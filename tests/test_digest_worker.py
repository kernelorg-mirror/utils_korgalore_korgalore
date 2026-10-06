"""Tests for the digest worker, which finishes slow digests outside kgl pull.

Most tests here use plain digests: without a summarizer in the context, a
schedule with a summarizer name only decides that the worker sends the
digest. TestSummarizedDigest gives the worker a fake summarizer.
"""

import mailbox
import textwrap
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from korgalore import ConfigurationError, StateError
from korgalore import cli as cli_mod
from korgalore.cli import (
    DIGEST_WORKER_LOCK,
    DIGEST_WORKER_LOG,
    DIGEST_WORKER_POKE,
    collect_digest,
    get_digest_worker_mode,
    get_xdg_data_dir,
    map_deliveries,
    poke_digest_worker,
    run_digest_worker,
    run_due_digests,
    spawn_digest_worker,
    summarize_digest_job,
)
from korgalore.digest import DigestJob, DigestSchedule, NoSummary, flocked
from korgalore.maildir_target import MaildirTarget
from korgalore.summarizer import FAILURES_MAX, SummaryRun
from tests.digest_helpers import (
    BOB,
    DAILY,
    DNAME,
    NOW,
    SLOW,
    FlakyTarget,
    InboxRepo,
    RecordingSummarizer,
    answered_repo,
    cache_of,
    collect,
    delivered,
    digest_text,
    job_of,
    make_ctx,
    make_raw,
    repo_with_msg,
    send,
    summarized_ctx,
    worker_ctx,
)


@pytest.fixture
def repo(repo: InboxRepo) -> InboxRepo:
    """One message, so every digest has something to send."""
    return repo_with_msg(repo)


class TestJobLock:
    def test_second_holder_is_refused(self, repo: InboxRepo) -> None:
        # flock works between open files, so this conflicts even in one process
        with job_of(repo).locked() as first, job_of(repo).locked() as second:
            assert first
            assert not second
        with job_of(repo).locked() as again:
            assert again

    def test_send_leaves_a_locked_job_alone(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        assert collect(repo)
        with job_of(repo).locked():
            assert send(repo, maildir) is None
        assert delivered(maildir) == []
        assert job_of(repo).exists()


class TestQueueForWorker:
    def test_pull_only_collects(self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget) -> None:
        ctx = worker_ctx(tmp_path, repo, maildir)
        with patch('korgalore.cli.spawn_digest_worker') as spawn:
            assert run_due_digests(ctx, [DNAME], force=True) == {}

        assert delivered(maildir) == []
        assert job_of(repo).load()['stage'] == 'collected'
        spawn.assert_called_once_with(ctx.obj['data_dir'], None)

    def test_waiting_job_is_not_collected_again(self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget) -> None:
        ctx = worker_ctx(tmp_path, repo, maildir)
        with patch('korgalore.cli.spawn_digest_worker') as spawn:
            run_due_digests(ctx, [DNAME], force=True)
            first = job_of(repo).load()
            repo.add_msg('b@x', NOW - timedelta(hours=1))
            run_due_digests(ctx, [DNAME], force=True)

        assert job_of(repo).load() == first
        # The worker may have died, so it is started again
        assert spawn.call_count == 2

    def test_job_held_by_worker_still_needs_it(self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget) -> None:
        ctx = worker_ctx(tmp_path, repo, maildir)
        with job_of(repo).locked(), patch('korgalore.cli.spawn_digest_worker') as spawn:
            run_due_digests(ctx, [DNAME], force=True)
        assert not job_of(repo).exists()
        spawn.assert_called_once()

    def test_nothing_due_starts_no_worker(self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget) -> None:
        ctx = worker_ctx(tmp_path, repo, maildir)
        assert collect(repo)
        send(repo, maildir)
        with patch('korgalore.cli.spawn_digest_worker') as spawn:
            run_due_digests(ctx, [DNAME])
        spawn.assert_not_called()

    def test_external_worker_is_not_spawned(self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget) -> None:
        ctx = worker_ctx(tmp_path, repo, maildir)
        ctx.obj['digest_worker'] = 'external'
        with patch('korgalore.cli.spawn_digest_worker') as spawn:
            run_due_digests(ctx, [DNAME], force=True)
        spawn.assert_not_called()
        assert job_of(repo).exists()
        # A worker that the user runs is told to look again too
        assert (ctx.obj['data_dir'] / DIGEST_WORKER_POKE).exists()


class TestWorker:
    # A plain digest job is finished too: it is left behind when an inline send failed half-way
    @pytest.mark.parametrize('schedule', [SLOW, DAILY], ids=['summarized', 'plain'])
    def test_finishes_the_job(
        self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget, schedule: DigestSchedule
    ) -> None:
        assert collect(repo)
        ctx = worker_ctx(tmp_path, repo, maildir, schedule=schedule)

        sent = run_digest_worker(ctx, [DNAME])

        [msg] = delivered(maildir)
        assert sent == {DNAME: [str(msg['Message-ID'])]}
        assert not job_of(repo).exists()
        assert repo.feed().load_digest_sent(DNAME) == NOW

    def test_no_job_sends_nothing(self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget) -> None:
        ctx = worker_ctx(tmp_path, repo, maildir)
        assert run_digest_worker(ctx, [DNAME]) == {}
        assert delivered(maildir) == []

    def test_waits_for_a_job_that_pull_is_collecting(
        self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget
    ) -> None:
        assert collect(repo)
        ctx = worker_ctx(tmp_path, repo, maildir)
        done = threading.Event()

        def work() -> None:
            run_digest_worker(ctx, [DNAME])
            done.set()

        worker = threading.Thread(target=work)
        # kgl pull holds the job lock while it collects
        with job_of(repo).locked():
            worker.start()
            assert not done.wait(0.1)
            assert delivered(maildir) == []
        worker.join(timeout=30)
        assert done.is_set()
        assert len(delivered(maildir)) == 1
        assert not job_of(repo).exists()

    def test_poke_makes_the_worker_look_again(self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget) -> None:
        ctx = worker_ctx(tmp_path, repo, maildir)
        real_exists = DigestJob.exists
        looked: List[int] = []

        def exists(job: DigestJob) -> bool:
            if looked:
                return real_exists(job)
            looked.append(1)
            # kgl pull collects a job right after the worker looked
            assert collect(repo)
            poke_digest_worker(ctx.obj['data_dir'])
            return False

        with patch.object(DigestJob, 'exists', exists):
            sent = run_digest_worker(ctx, [DNAME])

        assert DNAME in sent
        assert len(delivered(maildir)) == 1
        assert not (ctx.obj['data_dir'] / DIGEST_WORKER_POKE).exists()

    def test_stale_poke_is_consumed(self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget) -> None:
        ctx = worker_ctx(tmp_path, repo, maildir)
        poke_digest_worker(ctx.obj['data_dir'])
        assert run_digest_worker(ctx, [DNAME]) == {}
        assert not (ctx.obj['data_dir'] / DIGEST_WORKER_POKE).exists()

    def test_only_one_worker_runs(self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget) -> None:
        assert collect(repo)
        ctx = worker_ctx(tmp_path, repo, maildir)
        with flocked(ctx.obj['data_dir'] / DIGEST_WORKER_LOCK):
            assert run_digest_worker(ctx, [DNAME]) == {}
        assert job_of(repo).exists()

    def test_failing_target_is_tried_once(self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget) -> None:
        assert collect(repo)
        target = FlakyTarget(maildir, fail_on=list(range(1, 10)))
        ctx = worker_ctx(tmp_path, repo, target)

        assert run_digest_worker(ctx, [DNAME]) == {}

        # The job stays for the next worker, but this one does not loop on it
        assert target.calls == 1
        assert job_of(repo).load()['stage'] == 'rendered'

    def test_failure_does_not_stop_others(self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget) -> None:
        other = InboxRepo(tmp_path / 'netdev')
        other.add_msg('n@x', NOW - timedelta(hours=2))
        other_feed = other.feed()
        assert collect(repo)
        other_job = DigestJob(other_feed.get_digest_job_dir('netdev-digest'))
        assert collect_digest('netdev-digest', other_feed, DAILY, other_job, now=NOW)
        ctx = worker_ctx(tmp_path, repo, FlakyTarget(maildir, fail_on=[1]))
        good = MaildirTarget('good', str(tmp_path / 'good'))
        ctx.obj['deliveries']['netdev-digest'] = (other_feed, good, [], None)
        ctx.obj['digest_schedules']['netdev-digest'] = SLOW

        sent = run_digest_worker(ctx, [DNAME, 'netdev-digest'])

        assert list(sent) == ['netdev-digest']
        assert len(delivered(good)) == 1
        assert job_of(repo).exists()


class TestSummarizedDigest:
    @pytest.fixture
    def answered(self, tmp_path: Path) -> InboxRepo:
        return answered_repo(InboxRepo(tmp_path / 'lkml'))

    def test_digest_has_summaries(self, tmp_path: Path, answered: InboxRepo, maildir: MaildirTarget) -> None:
        fake = RecordingSummarizer()
        ctx = summarized_ctx(tmp_path, answered, maildir, fake)
        assert collect(answered)

        sent = run_digest_worker(ctx, [DNAME])

        assert len(sent[DNAME]) == 1
        (msg,) = delivered(maildir)
        assert msg['X-Korgalore-Digest-Model'] == 'qwen3:32b'
        text = digest_text(msg).replace('\r\n', '\n')
        assert 'Summary (machine-generated):\n    - summary 1' in text
        # lonely@x needs no summary, and says nothing about one
        assert text.count('summary 1') == 1
        assert 'Summary unavailable' not in text
        assert len(fake.prompts) == 1
        assert cache_of(ctx).find('a@x', ['a@x', 'r@x'], 'qwen3:32b') is not None

    def test_summaries_survive_in_the_job(self, tmp_path: Path, answered: InboxRepo, maildir: MaildirTarget) -> None:
        """A worker stopped after summarizing does not ask again."""
        ctx = summarized_ctx(tmp_path, answered, maildir, RecordingSummarizer())
        assert collect(answered)
        job = job_of(answered)
        summarize_digest_job(DNAME, SLOW, job, SummaryRun(RecordingSummarizer(), cache_of(ctx)))
        assert job.load()['stage'] == DigestJob.SUMMARIZED
        # Even an empty cache does not matter now
        cache_of(ctx).prune(datetime.now(timezone.utc), max_age=timedelta(0))

        run_digest_worker(ctx, [DNAME])

        assert ctx.obj['summarizers']['local'].prompts == []
        assert 'summary 1' in digest_text(delivered(maildir)[0])

    def test_dead_summarizer_still_sends(self, tmp_path: Path, answered: InboxRepo, maildir: MaildirTarget) -> None:
        ctx = summarized_ctx(tmp_path, answered, maildir, RecordingSummarizer(fail=True))
        assert collect(answered)

        run_digest_worker(ctx, [DNAME])

        (msg,) = delivered(maildir)
        assert msg['X-Korgalore-Digest-Model'] == 'qwen3:32b'
        assert digest_text(msg).count('Summary unavailable.') == 1
        assert not job_of(answered).exists()

    def test_max_summaries(self, tmp_path: Path, answered: InboxRepo, maildir: MaildirTarget) -> None:
        answered.add_msg('r2@x', NOW - timedelta(minutes=30), sender=BOB, irt='lonely@x')
        fake = RecordingSummarizer()
        ctx = summarized_ctx(tmp_path, answered, maildir, fake)
        ctx.obj['digest_schedules'][DNAME] = DigestSchedule(summarizer='local', max_summaries=1)
        assert collect(answered)

        run_digest_worker(ctx, [DNAME])

        assert len(fake.prompts) == 1
        assert 'reached its max_summaries limit' in digest_text(delivered(maildir)[0])

    def test_summary_instructions(self, tmp_path: Path, answered: InboxRepo, maildir: MaildirTarget) -> None:
        fake = RecordingSummarizer()
        ctx = summarized_ctx(tmp_path, answered, maildir, fake)
        ctx.obj['digest_schedules'][DNAME] = DigestSchedule(
            summarizer='local', summary_instructions='Tell me if anyone sounds upset.'
        )
        assert collect(answered)

        run_digest_worker(ctx, [DNAME])

        assert fake.instructions == ['Tell me if anyone sounds upset.']
        assert cache_of(ctx).latest('a@x', fake.model, 'Tell me if anyone sounds upset.') is not None

    def test_failures_count_across_digests(self, tmp_path: Path, answered: InboxRepo, maildir: MaildirTarget) -> None:
        """One run gives up on a dead summarizer, even across digests."""
        other = InboxRepo(tmp_path / 'netdev')
        other.add_many(
            [
                (make_raw(f'{kind}{n}@x', f'[PATCH] n{n}@x', **extra), when, 'm')
                for n in range(FAILURES_MAX)
                for kind, when, extra in (
                    ('n', NOW - timedelta(hours=2), {}),
                    ('nr', NOW - timedelta(hours=1), {'sender': BOB, 'irt': f'n{n}@x'}),
                )
            ]
        )
        other_feed = other.feed()
        assert collect_digest(
            'netdev-digest', other_feed, DAILY, DigestJob(other_feed.get_digest_job_dir('netdev-digest')), now=NOW
        )
        assert collect(answered)
        fake = RecordingSummarizer(fail=True)
        ctx = summarized_ctx(tmp_path, answered, maildir, fake)
        ctx.obj['deliveries']['netdev-digest'] = (other_feed, maildir, [], None)
        ctx.obj['digest_schedules']['netdev-digest'] = SLOW

        run_digest_worker(ctx, ['netdev-digest', DNAME])

        # netdev used up the failures, so lkml never asked
        assert len(fake.prompts) == FAILURES_MAX
        assert len(delivered(maildir)) == 2

    def test_old_summaries_are_pruned(self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget) -> None:
        ctx = summarized_ctx(tmp_path, repo, maildir, RecordingSummarizer())
        cache = cache_of(ctx)
        cache.store('old@x', 'qwen3:32b', 'old news', ['old@x'], datetime.now(timezone.utc) - timedelta(days=31))
        cache.store('new@x', 'qwen3:32b', 'fresh', ['new@x'], datetime.now(timezone.utc))

        run_digest_worker(ctx, [DNAME])

        assert cache.latest('old@x', 'qwen3:32b') is None
        assert cache.latest('new@x', 'qwen3:32b') is not None


class TestJobSummaries:
    def test_round_trip(self, repo: InboxRepo) -> None:
        assert collect(repo)
        job = job_of(repo)
        job.write_summaries('m', {'a@x': 'text', 'b@x': NoSummary.BUDGET, 'c@x': NoSummary.NOT_NEEDED})

        state = job.load()
        assert state['stage'] == DigestJob.SUMMARIZED
        assert state['model'] == 'm'
        assert job.summaries(state) == {'a@x': 'text', 'b@x': NoSummary.BUDGET, 'c@x': NoSummary.NOT_NEEDED}

    @pytest.mark.parametrize('state', [{'no_summary': {'a@x': 'tired'}}, {'summaries': ['a@x']}])
    def test_bad_summaries(self, repo: InboxRepo, state: Dict[str, Any]) -> None:
        with pytest.raises(StateError, match='Bad summaries'):
            job_of(repo).summaries(state)


class TestSpawn:
    def test_spawns_in_its_own_session(self, tmp_path: Path) -> None:
        with patch('korgalore.cli.subprocess.Popen') as popen:
            assert spawn_digest_worker(tmp_path, tmp_path / 'kgl.toml')
        cmd = popen.call_args.args[0]
        assert cmd[1:] == [
            '-m',
            'korgalore',
            '--cfgfile',
            str(tmp_path / 'kgl.toml'),
            '--logfile',
            str(tmp_path / DIGEST_WORKER_LOG),
            'digest',
            '--work',
        ]
        assert popen.call_args.kwargs['start_new_session'] is True
        cli_mod._digest_workers.clear()

    def test_not_spawned_while_one_runs(self, tmp_path: Path) -> None:
        with flocked(tmp_path / DIGEST_WORKER_LOCK), patch('korgalore.cli.subprocess.Popen') as popen:
            assert not spawn_digest_worker(tmp_path, None)
        popen.assert_not_called()
        # The running worker is told to look again before it exits
        assert (tmp_path / DIGEST_WORKER_POKE).exists()

    def test_finished_workers_are_reaped(self, tmp_path: Path) -> None:
        done = MagicMock()
        done.poll.return_value = 0
        cli_mod._digest_workers.append(done)
        with patch('korgalore.cli.subprocess.Popen') as popen:
            spawn_digest_worker(tmp_path, None)
        assert cli_mod._digest_workers == [popen.return_value]
        cli_mod._digest_workers.clear()

    def test_real_worker_sends_the_digest(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Start "python -m korgalore digest --work" for real and wait for it."""
        monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'share'))
        data_dir = get_xdg_data_dir()
        repo = InboxRepo(data_dir / 'lkml')
        repo.add_msg('a@x', NOW - timedelta(hours=3))
        assert collect(repo)
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text(
            textwrap.dedent(f"""
                [targets.local]
                type = 'maildir'
                path = '{tmp_path / 'mail'}'

                [deliveries.{DNAME}]
                feed = 'https://lore.kernel.org/lkml'
                target = 'local'
                mode = 'digest'
            """)
        )

        assert spawn_digest_worker(data_dir, cfgpath)
        proc = cli_mod._digest_workers.pop()
        assert proc.wait(timeout=120) == 0

        [msg] = mailbox.Maildir(str(tmp_path / 'mail'), create=False)
        assert 'lkml' in str(msg['Subject'])
        assert not job_of(repo).exists()
        assert 'Sent digest' in (data_dir / DIGEST_WORKER_LOG).read_text()


class TestWorkerConfig:
    @pytest.mark.parametrize(
        ('config', 'expected'),
        [
            pytest.param({}, 'spawn', id='default'),
            pytest.param({'digests': {'worker': 'external'}}, 'external', id='external'),
            pytest.param({'digests': {'worker': 'thread'}}, None, id='bad-value'),
        ],
    )
    def test_mode(self, config: Dict[str, Any], expected: Optional[str]) -> None:
        if expected is None:
            with pytest.raises(ConfigurationError, match='worker'):
                get_digest_worker_mode(config)
        else:
            assert get_digest_worker_mode(config) == expected

    def test_mapped_with_digests(self) -> None:
        ctx = make_ctx({'config': {'targets': {}, 'digests': {'worker': 'external'}}, 'targets': {}, 'feeds': {}})
        with (
            patch('korgalore.cli.get_feed_for_delivery', return_value=MagicMock(feed_key='lkml')),
            patch('korgalore.cli.get_target', return_value=MagicMock(identifier='local')),
        ):
            map_deliveries(ctx, {DNAME: {'feed': 'lkml', 'target': 'local', 'mode': 'digest'}})
        assert ctx.obj['digest_worker'] == 'external'
