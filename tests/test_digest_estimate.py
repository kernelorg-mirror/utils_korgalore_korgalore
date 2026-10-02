"""Tests for kgl digest --estimate, which reports without calling a model.

The numbers themselves are tested against real runs in test_summarizer.py.
These tests check which messages the estimate looks at, and that it
leaves no trace: no job, no saved state, no cached summaries.
"""

from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from korgalore.cli import digest_cmd, estimate_digest, run_digest_estimates, summarize_digest_job
from korgalore.digest import DigestSchedule
from korgalore.maildir_target import MaildirTarget
from korgalore.summarizer import SummaryCache, SummaryRun
from tests.digest_helpers import (
    BOB,
    DNAME,
    NOW,
    InboxRepo,
    RecordingSummarizer,
    cache_of,
    collect,
    job_of,
    send,
    summarized_ctx,
)

SUMMARIZED = DigestSchedule(summarizer='local')


@pytest.fixture
def repo(tmp_path: Path) -> InboxRepo:
    """a@x gets an answer; lonely@x does not."""
    repo = InboxRepo(tmp_path / 'lkml')
    repo.add_msg('a@x', NOW - timedelta(hours=3))
    repo.add_msg('r@x', NOW - timedelta(hours=2), sender=BOB, irt='a@x')
    repo.add_msg('lonely@x', NOW - timedelta(hours=1))
    return repo


def estimate(
    repo: InboxRepo,
    cache: SummaryCache,
    fake: Optional[RecordingSummarizer] = None,
    schedule: DigestSchedule = SUMMARIZED,
    **kwargs: Any,
) -> List[str]:
    kwargs.setdefault('now', NOW)
    return estimate_digest(DNAME, repo.feed(), schedule, fake, cache, **kwargs)


class TestEstimateDigest:
    def test_next_digest(self, repo: InboxRepo, cache: SummaryCache) -> None:
        lines = estimate(repo, cache, RecordingSummarizer())
        assert lines == [
            f'Digest {DNAME} (due now)',
            '  2 threads, 3 messages',
            '  Summarizer fake, model qwen3:32b, on this machine',
            '  No summary needed: 1, cached: 0, over max_summaries: 0',
            '  Model calls: 1 (0 build on an earlier summary)',
            lines[5],
        ]
        assert lines[5].startswith('  Input: ')

    def test_leaves_no_trace(self, repo: InboxRepo, cache: SummaryCache) -> None:
        fake = RecordingSummarizer()
        estimate(repo, cache, fake)

        assert fake.prompts == []
        assert not job_of(repo).exists()
        assert repo.feed().load_digest_sent(DNAME) is None
        assert not cache.path.exists() or not any(cache.path.iterdir())
        # The real digest still has everything
        assert collect(repo)
        assert len(job_of(repo).messages()) == 3

    def test_not_due_counts_up_to_now(self, repo: InboxRepo, cache: SummaryCache, maildir: MaildirTarget) -> None:
        send(repo, maildir)
        repo.add_msg('new@x', NOW + timedelta(hours=1))
        lines = estimate(repo, cache, RecordingSummarizer(), now=NOW + timedelta(hours=2))

        assert lines[0] == f'Digest {DNAME} (not due yet, counting up to now)'
        assert lines[1] == '  1 threads, 1 messages'

    def test_waiting_job_is_what_the_worker_sends(self, repo: InboxRepo, cache: SummaryCache) -> None:
        assert collect(repo)
        repo.add_msg('late@x', NOW + timedelta(hours=1))
        lines = estimate(repo, cache, RecordingSummarizer(), now=NOW + timedelta(hours=2))

        assert lines[0] == f'Digest {DNAME} (waiting for the digest worker)'
        # late@x goes into the next digest, not this one
        assert lines[1] == '  2 threads, 3 messages'

    def test_summarized_job(self, repo: InboxRepo, cache: SummaryCache) -> None:
        fake = RecordingSummarizer()
        assert collect(repo)
        summarize_digest_job(DNAME, SUMMARIZED, job_of(repo), SummaryRun(fake, cache), now=NOW)

        assert estimate(repo, cache, fake) == [f'Digest {DNAME}: already summarized, waiting to be sent']

    def test_worker_is_on_it(self, repo: InboxRepo, cache: SummaryCache) -> None:
        with job_of(repo).locked():
            lines = estimate(repo, cache, RecordingSummarizer())
        assert lines == [f'Digest {DNAME}: the digest worker is working on it now']

    def test_cached_summaries_cost_nothing(self, repo: InboxRepo, cache: SummaryCache) -> None:
        cache.store('a@x', 'qwen3:32b', 'from before', ['a@x', 'r@x'], NOW)
        lines = estimate(repo, cache, RecordingSummarizer())

        assert '  No summary needed: 1, cached: 1, over max_summaries: 0' in lines
        assert '  Model calls: 0 (0 build on an earlier summary)' in lines
        assert not any(line.startswith('  Input:') for line in lines)

    def test_max_summaries(self, repo: InboxRepo, cache: SummaryCache) -> None:
        repo.add_msg('b@x', NOW - timedelta(minutes=30))
        repo.add_msg('rb@x', NOW - timedelta(minutes=20), sender=BOB, irt='b@x')
        schedule = DigestSchedule(summarizer='local', max_summaries=1)
        lines = estimate(repo, cache, RecordingSummarizer(), schedule=schedule)

        assert '  No summary needed: 1, cached: 0, over max_summaries: 1' in lines
        assert '  Model calls: 1 (0 build on an earlier summary)' in lines

    def test_remote_summarizer_stands_out(self, repo: InboxRepo, cache: SummaryCache) -> None:
        fake = RecordingSummarizer()
        fake.is_local = False
        assert '  Summarizer fake, model qwen3:32b, NOT on this machine' in estimate(repo, cache, fake)

    def test_cut_prompts(self, repo: InboxRepo, cache: SummaryCache) -> None:
        fake = RecordingSummarizer()
        fake.max_input_chars = 100
        lines = estimate(repo, cache, fake)
        assert lines[-1].startswith('  Cut to max_input_chars (100): 1 prompts, ')

    def test_plain_digest(self, repo: InboxRepo, cache: SummaryCache) -> None:
        lines = estimate(repo, cache, None, schedule=DigestSchedule())
        assert lines == [f'Digest {DNAME} (due now)', '  2 threads, 3 messages', '  Plain digest, nothing to summarize']


class TestRunEstimates:
    def test_prints_each_digest(
        self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake = RecordingSummarizer()
        ctx = summarized_ctx(tmp_path, repo, maildir, fake)
        run_digest_estimates(ctx, [DNAME], now=NOW)

        out = capsys.readouterr().out
        assert out.startswith(f'Digest {DNAME} (')
        assert '  Model calls: 1 ' in out
        assert fake.prompts == []
        assert not cache_of(ctx).path.exists() or not any(cache_of(ctx).path.iterdir())

    def test_bozofilter(
        self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ctx = summarized_ctx(tmp_path, repo, maildir, RecordingSummarizer())
        ctx.obj['bozofilter'] = {'bob@x'}
        run_digest_estimates(ctx, [DNAME], now=NOW)
        # Bob's answer is left out, as in the real digest
        assert '  2 threads, 2 messages' in capsys.readouterr().out

    def test_failure_moves_on(
        self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget, caplog: pytest.LogCaptureFixture
    ) -> None:
        ctx = summarized_ctx(tmp_path, repo, maildir, RecordingSummarizer())
        ctx.obj['deliveries']['other'] = ctx.obj['deliveries'][DNAME]
        ctx.obj['digest_schedules']['other'] = SUMMARIZED
        with patch('korgalore.cli.estimate_digest', side_effect=[OSError('disk on fire'), ['fine']]) as est:
            run_digest_estimates(ctx, [DNAME, 'other'], now=NOW)
        assert est.call_count == 2
        assert 'Could not estimate digest lkml-digest: disk on fire' in caplog.text


class TestEstimateCommand:
    @pytest.fixture
    def env(self) -> Iterator[Dict[str, MagicMock]]:
        mocks = {name: MagicMock() for name in ('map', 'lock', 'unlock', 'update', 'estimate', 'due', 'work')}
        with (
            patch('korgalore.cli.map_deliveries', mocks['map']),
            patch('korgalore.cli.lock_all_feeds', mocks['lock']),
            patch('korgalore.cli.unlock_all_feeds', mocks['unlock']),
            patch('korgalore.cli.update_all_feeds', mocks['update']),
            patch('korgalore.cli.run_digest_estimates', mocks['estimate']),
            patch('korgalore.cli.run_due_digests', mocks['due']),
            patch('korgalore.cli.run_digest_worker', mocks['work']),
        ):
            yield mocks

    @staticmethod
    def _invoke(*args: str) -> Any:
        obj = {'config': {'deliveries': {DNAME: {'feed': 'lkml', 'target': 'local', 'mode': 'digest'}}}, 'targets': {}}
        return CliRunner().invoke(digest_cmd, list(args), obj=obj)

    def test_estimate_sends_nothing(self, env: Dict[str, MagicMock]) -> None:
        result = self._invoke('--estimate')
        assert result.exit_code == 0, result.output
        assert env['estimate'].call_args.args[1] == [DNAME]
        env['due'].assert_not_called()
        # Reading the feed needs the lock, like a real digest
        env['lock'].assert_called_once()
        env['unlock'].assert_called_once()
        env['update'].assert_called_once()

    def test_no_update(self, env: Dict[str, MagicMock]) -> None:
        assert self._invoke('--estimate', '--no-update').exit_code == 0
        env['update'].assert_not_called()
        env['estimate'].assert_called_once()

    @pytest.mark.parametrize('other', ['--force', '--work'])
    def test_estimate_only_reports(self, env: Dict[str, MagicMock], other: str) -> None:
        result = self._invoke('--estimate', other)
        assert result.exit_code == 2
        assert 'only reports' in result.output
        env['map'].assert_not_called()
        env['estimate'].assert_not_called()
        env['due'].assert_not_called()
        env['work'].assert_not_called()

    def test_estimate_is_listed(self) -> None:
        assert '--estimate' in CliRunner().invoke(digest_cmd, ['--help']).output
