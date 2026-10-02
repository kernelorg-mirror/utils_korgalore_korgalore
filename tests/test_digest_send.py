"""Tests for sending digests: state, scheduling, and the hooks in pull.

The send_digest tests use a real public-inbox style git repository and a
real maildir, so they check what ends up on disk, not which mocks were
called. Each "run" builds a new feed object, the same way every kgl run
does, so no cache can hide a bug.
"""

import functools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

import liblore
from korgalore import AuthenticationError, ConfigurationError, StateError
from korgalore import digest as digest_mod
from korgalore.cli import (
    digest_cmd,
    look_up_root_subjects,
    map_deliveries,
    perform_pull,
    render_digest_job,
    run_due_digests,
    send_digest,
)
from korgalore.digest import DigestJob, DigestSchedule
from korgalore.lei_feed import LeiFeed
from korgalore.lore_feed import LoreFeed
from korgalore.maildir_target import MaildirTarget
from korgalore.pi_feed import PIFeed
from tests.digest_helpers import (
    DAILY,
    DNAME,
    NOW,
    FlakyTarget,
    InboxRepo,
    collect,
    delivered,
    digest_text,
    job_of,
    make_ctx,
    make_raw,
    mkmsg,
    part_text,
    send,
    send_parts,
)

UTC = timezone.utc


class TestSendDigest:
    def test_first_run_backfills_one_period(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        repo.add_msg('old@x', NOW - timedelta(days=3))
        repo.add_msg('recent@x', NOW - timedelta(hours=12))
        repo.add_msg('fresh@x', NOW - timedelta(hours=1))

        digest = send(repo, maildir)

        assert digest is not None
        [msg] = delivered(maildir)
        assert msg['Message-ID'] == digest['Message-ID']
        assert msg.get_content_type() == 'multipart/alternative'
        text = digest_text(msg)
        assert 'recent@x' in text
        assert 'fresh@x' in text
        assert 'old@x' not in text
        # Links go to the feed's own archive
        html = part_text(msg, 'html')
        assert 'https://lore.kernel.org/lkml/fresh@x/' in html

        feed = repo.feed()
        assert feed.load_digest_sent(DNAME) == NOW
        assert feed.load_delivery_info(DNAME)['epochs']['0']['last'] == repo.head

    def test_not_due_sends_nothing(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=1))
        send(repo, maildir)
        repo.add_msg('b@x', NOW + timedelta(minutes=30))

        assert send(repo, maildir, now=NOW + timedelta(hours=1)) is None
        assert len(delivered(maildir)) == 1
        assert repo.feed().load_digest_sent(DNAME) == NOW

    def test_next_digest_has_only_new_messages(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=1))
        send(repo, maildir)
        tomorrow = NOW + timedelta(days=1)
        repo.add_msg('b@x', tomorrow - timedelta(hours=2))

        digest = send(repo, maildir, now=tomorrow)

        assert digest is not None
        text = digest_text(digest)
        assert 'b@x' in text
        assert 'a@x' not in text
        assert repo.feed().load_digest_sent(DNAME) == tomorrow

    def test_reply_to_old_thread_is_an_update(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        repo.add_msg('root@x', NOW - timedelta(hours=2), subject='[PATCH] mm: fix it')
        send(repo, maildir)
        tomorrow = NOW + timedelta(days=1)
        repo.add_msg('reply@x', tomorrow - timedelta(hours=2), subject='Re: [PATCH] mm: fix it', irt='root@x')

        text = digest_text(send(repo, maildir, now=tomorrow))

        assert '1 continuing' in text
        assert 'mm: fix it' in text
        # A continuing thread links to its root, where the whole thread is
        assert 'https://lore.kernel.org/lkml/root@x/' in text

    def test_empty_period_is_skipped_but_recorded(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=1))
        send(repo, maildir)
        tomorrow = NOW + timedelta(days=1)

        assert send(repo, maildir, now=tomorrow) is None
        assert len(delivered(maildir)) == 1
        # Recorded as sent, so we don't check again until the next slot
        assert repo.feed().load_digest_sent(DNAME) == tomorrow

    def test_send_empty(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        repo.add_msg('a@x', NOW - timedelta(days=3))
        digest = send(repo, maildir, schedule=DigestSchedule(send_empty=True))
        assert digest is not None
        assert len(delivered(maildir)) == 1

    def test_empty_feed(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        assert send(repo, maildir) is None
        assert repo.feed().load_digest_sent(DNAME) == NOW
        assert not repo.feed().has_delivery_pointer(DNAME)

    def test_empty_feed_send_empty_sends_once(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        sched = DigestSchedule(send_empty=True)
        assert send(repo, maildir, schedule=sched) is not None
        assert send(repo, maildir, now=NOW + timedelta(minutes=10), schedule=sched) is None
        assert len(delivered(maildir)) == 1

    def test_feed_gets_first_messages(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        send(repo, maildir)  # empty, no pointer saved
        tomorrow = NOW + timedelta(days=1)
        repo.add_msg('a@x', tomorrow - timedelta(hours=1))
        # Sent before the last digest, so it was in that (empty) period
        repo.add_msg('old@x', NOW - timedelta(hours=1))

        text = digest_text(send(repo, maildir, now=tomorrow))

        assert 'a@x' in text
        assert 'old@x' not in text
        assert repo.feed().has_delivery_pointer(DNAME)

    def test_force_when_not_due(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=1))
        send(repo, maildir)
        later = NOW + timedelta(hours=1)
        repo.add_msg('b@x', later - timedelta(minutes=5))

        digest = send(repo, maildir, now=later, force=True)

        assert digest is not None
        assert 'b@x' in digest_text(digest)
        assert repo.feed().load_digest_sent(DNAME) == later

    def test_force_sends_empty_digest(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=1))
        send(repo, maildir)
        assert send(repo, maildir, now=NOW + timedelta(hours=1), force=True) is not None
        assert len(delivered(maildir)) == 2

    def test_bozofilter(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        repo.add_msg('nice@x', NOW - timedelta(hours=2))
        repo.add_msg('bozo@x', NOW - timedelta(hours=1), sender='Bozo <bozo@example.com>')

        text = digest_text(send(repo, maildir, bozofilter={'bozo@example.com'}))

        assert 'nice@x' in text
        assert 'bozo@x' not in text

    def test_only_bozos_is_empty(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        repo.add_msg('bozo@x', NOW - timedelta(hours=1), sender='Bozo <bozo@example.com>')
        assert send(repo, maildir, bozofilter={'bozo@example.com'}) is None
        assert delivered(maildir) == []

    def test_deleted_message_is_skipped(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=2))
        # public-inbox records a removal as a commit with a 'd' file
        last = repo.add(make_raw('a@x', 'gone'), NOW - timedelta(hours=1), filename='d')

        digest = send(repo, maildir)

        assert digest is not None
        # The pointer still moves past the deletion
        assert repo.feed().load_delivery_info(DNAME)['epochs']['0']['last'] == last

    def test_target_failure_keeps_state(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=1))
        send(repo, maildir)
        tomorrow = NOW + timedelta(days=1)
        repo.add_msg('b@x', tomorrow - timedelta(hours=1))
        broken = MagicMock()
        broken.import_message.side_effect = RuntimeError('server said no')

        with pytest.raises(RuntimeError):
            send(repo, broken, now=tomorrow)
        assert repo.feed().load_digest_sent(DNAME) == NOW

        # The next run sends the same messages
        digest = send(repo, maildir, now=tomorrow + timedelta(minutes=10))
        assert digest is not None
        assert 'b@x' in digest_text(digest)

    def test_target_gets_labels_and_names(self, repo: InboxRepo) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=1))
        target = MagicMock()

        parts = send_digest(DNAME, repo.feed(), target, ['digests'], 'lists/lkml', DAILY, now=NOW)

        assert len(parts) == 1
        digest = parts[0]
        target.connect.assert_called_once()
        raw = target.import_message.call_args.args[0]
        assert raw == digest.as_bytes()
        assert target.import_message.call_args.kwargs == {
            'labels': ['digests'],
            'feed_name': 'lkml',
            'delivery_name': DNAME,
            'subfolder': 'lists/lkml',
        }

    def test_message_mode_state_survives(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        """Turning a message delivery into a digest keeps working."""
        repo.add_msg('a@x', NOW - timedelta(hours=1))
        feed = repo.feed()
        feed.save_delivery_info(DNAME)  # what a message delivery leaves behind
        assert feed.load_digest_sent(DNAME) is None

        assert send(repo, maildir) is not None


def _target(identifier: str) -> MagicMock:
    target = MagicMock()
    target.identifier = identifier
    return target


class TestRunDueDigests:
    @staticmethod
    def _obj(targets: Dict[str, MagicMock]) -> Dict[str, Any]:
        return {
            'deliveries': {name: (MagicMock(feed_key=name), t, [], None) for name, t in targets.items()},
            'digest_schedules': {name: DAILY for name in targets},
            'bozofilter': {'bozo@example.com'},
        }

    def test_failure_does_not_stop_others(self) -> None:
        obj = self._obj({'bad': _target('t1'), 'good': _target('t2')})
        good_digest = {'Subject': 'lkml digest', 'Message-ID': '<d@x>'}

        def fake_send(dname: str, *args: Any, **kwargs: Any) -> Any:
            if dname == 'bad':
                raise RuntimeError('boom')
            return [good_digest]

        with patch('korgalore.cli.send_digest', side_effect=fake_send) as mock_send:
            sent = run_due_digests(make_ctx(obj), ['bad', 'good'], force=True)

        assert sent == {'good': ['<d@x>']}
        # Every target used gets disconnected, even after a failure
        for _, target, _, _ in obj['deliveries'].values():
            target.disconnect.assert_called_once()
        assert mock_send.call_args.kwargs == {'force': True}
        assert mock_send.call_args.args[6] == {'bozo@example.com'}

    def test_not_due_is_not_reported(self) -> None:
        obj = self._obj({'quiet': _target('t1')})
        with patch('korgalore.cli.send_digest', return_value=[]):
            assert run_due_digests(make_ctx(obj), ['quiet']) == {}

    def test_auth_error_goes_to_caller(self) -> None:
        obj = self._obj({'gmail': _target('t1')})
        error = AuthenticationError('expired', target_id='t1')
        with patch('korgalore.cli.send_digest', side_effect=error), pytest.raises(AuthenticationError):
            run_due_digests(make_ctx(obj), ['gmail'])


class TestMapDeliveries:
    @staticmethod
    def _map(details: Dict[str, Any]) -> click.Context:
        ctx = make_ctx({'config': {'targets': {}}, 'targets': {}, 'feeds': {}})
        with (
            patch('korgalore.cli.get_feed_for_delivery', return_value=MagicMock(feed_key='lkml')),
            patch('korgalore.cli.get_target', return_value=_target('local')),
        ):
            map_deliveries(ctx, {DNAME: {'feed': 'lkml', 'target': 'local', **details}})
        return ctx

    def test_message_is_default(self) -> None:
        assert self._map({}).obj['digest_schedules'] == {}

    def test_digest(self) -> None:
        ctx = self._map({'mode': 'digest', 'schedule': 'weekly'})
        assert ctx.obj['digest_schedules'] == {DNAME: DigestSchedule(schedule='weekly')}
        assert DNAME in ctx.obj['deliveries']

    def test_bad_mode(self) -> None:
        with pytest.raises(ConfigurationError, match='mode'):
            self._map({'mode': 'digests'})

    def test_digest_keys_need_digest_mode(self) -> None:
        # A typo in mode shouldn't silently deliver every message
        with pytest.raises(ConfigurationError, match='send_at'):
            self._map({'send_at': '07:00'})


def _pull_obj() -> Dict[str, Any]:
    feed = MagicMock(feed_key='lkml')
    feed.update_feed.return_value = PIFeed.STATUS_UPDATED
    feed.STATUS_UPDATED = PIFeed.STATUS_UPDATED
    feed.STATUS_INITIALIZED = PIFeed.STATUS_INITIALIZED
    feed.STATUS_NOCHANGE = PIFeed.STATUS_NOCHANGE
    feed.get_latest_commits_for_delivery.return_value = [(0, 'abc')]
    target = _target('local')
    return {
        'config': {'deliveries': {'lkml-all': {}, DNAME: {'mode': 'digest'}}},
        'feeds': {'lkml': feed},
        'deliveries': {'lkml-all': (feed, target, [], None), DNAME: (feed, target, [], None)},
        'digest_schedules': {DNAME: DAILY},
        'targets': {},
        'bozofilter': set(),
        'hide_bar': True,
    }


@pytest.fixture
def pull_env() -> Iterator[Tuple[MagicMock, MagicMock]]:
    """Stub out mapping, locking and tracking; yield (deliver_commit, run_due_digests)."""
    deliver = MagicMock(return_value='<m@x>')
    digests = MagicMock(return_value={DNAME: ['<digest-1@x>', '<digest-2@x>']})
    with (
        patch('korgalore.cli.map_deliveries'),
        patch('korgalore.cli.map_tracked_threads'),
        patch('korgalore.cli.lock_all_feeds'),
        patch('korgalore.cli.unlock_all_feeds') as unlock,
        patch('korgalore.cli.retry_all_failed_deliveries'),
        patch('korgalore.cli.update_tracked_thread_activity'),
        patch('korgalore.cli.close_requests_session'),
        patch('korgalore.cli.deliver_commit', deliver),
        patch('korgalore.cli.run_due_digests', digests),
    ):
        digests.unlock = unlock
        yield deliver, digests


class TestPullHook:
    def test_digest_not_delivered_per_message(self, pull_env: Tuple[MagicMock, MagicMock]) -> None:
        deliver, digests = pull_env
        changes, msgids = perform_pull(make_ctx(_pull_obj()), no_update=False, force=False, delivery_name=None)

        assert [c.args[0] for c in deliver.call_args_list] == ['lkml-all']
        assert digests.call_args.args[1] == [DNAME]
        # A digest in two parts counts as two delivered messages
        assert changes == {'lkml-all': 1, DNAME: 2}
        assert msgids == {'<m@x>', '<digest-1@x>', '<digest-2@x>'}

    def test_force_still_skips_digest_messages(self, pull_env: Tuple[MagicMock, MagicMock]) -> None:
        deliver, _ = pull_env
        perform_pull(make_ctx(_pull_obj()), no_update=True, force=True, delivery_name=None)
        assert [c.args[0] for c in deliver.call_args_list] == ['lkml-all']

    def test_digests_checked_without_updates(self, pull_env: Tuple[MagicMock, MagicMock]) -> None:
        # A digest can be due on a quiet day, when no feed has news
        deliver, digests = pull_env
        obj = _pull_obj()
        obj['feeds']['lkml'].update_feed.return_value = PIFeed.STATUS_NOCHANGE

        changes, _ = perform_pull(make_ctx(obj), no_update=False, force=False, delivery_name=None)

        deliver.assert_not_called()
        digests.assert_called_once()
        assert changes == {DNAME: 2}

    def test_auth_error_unlocks(self, pull_env: Tuple[MagicMock, MagicMock]) -> None:
        _, digests = pull_env
        digests.side_effect = AuthenticationError('expired', target_id='local')
        with pytest.raises(AuthenticationError):
            perform_pull(make_ctx(_pull_obj()), no_update=True, force=False, delivery_name=None)
        digests.unlock.assert_called_once()


class TestDigestCommand:
    @staticmethod
    def _obj() -> Dict[str, Any]:
        return {
            'config': {
                'deliveries': {
                    'lkml-all': {'feed': 'lkml', 'target': 'local'},
                    DNAME: {'feed': 'lkml', 'target': 'local', 'mode': 'digest'},
                    'netdev-digest': {'feed': 'netdev', 'target': 'local', 'mode': 'digest'},
                }
            },
            'targets': {},
            'hide_bar': True,
        }

    @pytest.fixture
    def env(self) -> Iterator[Dict[str, MagicMock]]:
        def fake_update(ctx: click.Context, **kwargs: Any) -> Tuple[List[str], List[str]]:
            ctx.obj['failed_feeds'] = ['netdev']
            return [], []

        mocks = {
            'map': MagicMock(),
            'update': MagicMock(side_effect=fake_update),
            'run': MagicMock(return_value={}),
            'unlock': MagicMock(),
        }
        with (
            patch('korgalore.cli.map_deliveries', mocks['map']),
            patch('korgalore.cli.lock_all_feeds'),
            patch('korgalore.cli.unlock_all_feeds', mocks['unlock']),
            patch('korgalore.cli.update_all_feeds', mocks['update']),
            patch('korgalore.cli.run_due_digests', mocks['run']),
            patch('korgalore.cli.close_requests_session'),
        ):
            yield mocks

    def _invoke(self, *args: str) -> Any:
        return CliRunner().invoke(digest_cmd, list(args), obj=self._obj())

    def test_runs_only_digests(self, env: Dict[str, MagicMock]) -> None:
        result = self._invoke()
        assert result.exit_code == 0, result.output
        assert list(env['map'].call_args.args[1]) == [DNAME, 'netdev-digest']
        assert env['run'].call_args.args[1] == [DNAME, 'netdev-digest']
        assert env['run'].call_args.kwargs == {'force': False}
        env['update'].assert_called_once()
        env['unlock'].assert_called_once()

    def test_named_and_forced(self, env: Dict[str, MagicMock]) -> None:
        result = self._invoke('--force', '--no-update', DNAME)
        assert result.exit_code == 0, result.output
        assert env['run'].call_args.args[1] == [DNAME]
        assert env['run'].call_args.kwargs == {'force': True}
        env['update'].assert_not_called()

    def test_unknown_delivery(self, env: Dict[str, MagicMock]) -> None:
        result = self._invoke('nope')
        assert result.exit_code != 0
        env['run'].assert_not_called()

    def test_message_delivery_refused(self, env: Dict[str, MagicMock]) -> None:
        result = self._invoke('lkml-all')
        assert result.exit_code != 0
        env['run'].assert_not_called()

    def test_fail_on_feed_error(self, env: Dict[str, MagicMock]) -> None:
        assert self._invoke().exit_code == 0
        result = self._invoke('--fail-on-feed-error')
        assert result.exit_code == 3
        # The digests were still sent
        assert env['run'].call_count == 2

    def test_unlocks_on_error(self, env: Dict[str, MagicMock]) -> None:
        env['run'].side_effect = AuthenticationError('expired', target_id='local')
        result = self._invoke()
        assert result.exit_code != 0
        env['unlock'].assert_called_once()

    def test_no_digests_configured(self, env: Dict[str, MagicMock]) -> None:
        obj = self._obj()
        del obj['config']['deliveries'][DNAME]
        del obj['config']['deliveries']['netdev-digest']
        result = CliRunner().invoke(digest_cmd, [], obj=obj)
        assert result.exit_code == 0
        env['map'].assert_not_called()


@pytest.fixture
def small_parts() -> Iterator[None]:
    """Split digests into parts of 4000 bytes, so a few threads are enough."""
    small = functools.partial(digest_mod.render_digest_parts, max_size=4000)
    with patch('korgalore.cli.render_digest_parts', small):
        yield


@pytest.mark.usefixtures('small_parts')
class TestSplitDelivery:
    @staticmethod
    def add_threads(repo: InboxRepo, count: int) -> None:
        for n in range(count):
            repo.add_msg(f't{n}@x', NOW - timedelta(hours=8) + timedelta(minutes=n))

    def test_parts_are_sent_in_order(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        self.add_threads(repo, 30)
        parts = send_parts(repo, maildir)

        assert len(parts) > 1
        subjects = [str(msg['Subject']) for msg in delivered(maildir)]
        assert subjects == [str(msg['Subject']) for msg in parts]
        total = len(parts)
        assert [str(msg['X-Korgalore-Digest-Part']) for msg in parts] == [f'{n}/{total}' for n in range(1, total + 1)]
        assert repo.feed().load_digest_sent(DNAME) == NOW
        assert not job_of(repo).path.exists()

    def test_failed_part_is_resumed(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        self.add_threads(repo, 30)
        flaky = FlakyTarget(maildir, fail_on=[3])

        with pytest.raises(RuntimeError):
            send_parts(repo, flaky)
        assert len(delivered(maildir)) == 2
        # State is not saved until the last part is in
        assert repo.feed().load_digest_sent(DNAME) is None
        assert job_of(repo).exists()

        # A new message arrives before the next run: it waits for the next digest
        repo.add_msg('late@x', NOW + timedelta(minutes=30))
        rest = send_parts(repo, maildir, now=NOW + timedelta(hours=1))

        everything = delivered(maildir)
        total = int(str(everything[0]['X-Korgalore-Digest-Part']).split('/')[1])
        assert len(rest) == total - 2
        # Every part arrived exactly once, and all of them are one digest
        numbers = sorted(int(str(msg['X-Korgalore-Digest-Part']).split('/')[0]) for msg in everything)
        assert numbers == list(range(1, total + 1))
        assert len({msg['Message-ID'] for msg in everything}) == total
        assert not any('late@x' in digest_text(msg) for msg in everything)
        # The digest period ends when it was rendered, not when the last part went out
        assert repo.feed().load_digest_sent(DNAME) == NOW
        assert not job_of(repo).path.exists()

        tomorrow = NOW + timedelta(days=1)
        nxt = send_parts(repo, maildir, now=tomorrow)
        assert len(nxt) == 1
        assert 'late@x' in digest_text(nxt[0])
        assert 't0@x' not in digest_text(nxt[0])

    def test_resume_comes_before_schedule(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        """Left-over parts go out on the next run, even when no digest is due."""
        self.add_threads(repo, 30)
        with pytest.raises(RuntimeError):
            send_parts(repo, FlakyTarget(maildir, fail_on=[1]))
        assert delivered(maildir) == []
        assert len(send_parts(repo, maildir, now=NOW + timedelta(minutes=5))) > 1

    def test_unfinished_job_is_thrown_away(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        """Without a job file, the job was never finished, so start again."""
        self.add_threads(repo, 3)
        job = job_of(repo)
        job.parts_dir.mkdir(parents=True)
        (job.parts_dir / '0001.eml').write_bytes(b'Subject: half-written\n\n')

        (digest,) = send_parts(repo, maildir)

        assert 'half-written' not in str(digest['Subject'])
        assert [str(msg['Subject']) for msg in delivered(maildir)] == [str(digest['Subject'])]

    def test_job_already_delivered(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        """We stopped after saving state but before removing the job."""
        self.add_threads(repo, 3)
        send_parts(repo, maildir)
        job = job_of(repo)
        job.create([], {'period_end': NOW.isoformat(), 'pointer': None})
        job.write_parts([])

        assert send_parts(repo, maildir, now=NOW + timedelta(minutes=5)) == []
        assert not job.path.exists()
        assert len(delivered(maildir)) == 1

    def test_bad_job_file(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        job = job_of(repo)
        job.path.mkdir()
        (job.path / DigestJob.JOB_FILE).write_text('{not json')
        with pytest.raises(StateError):
            send_parts(repo, maildir)

    def test_unknown_stage(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        job = job_of(repo)
        job.path.mkdir()
        (job.path / DigestJob.JOB_FILE).write_text(json.dumps({'stage': 'summarizing', 'period_end': NOW.isoformat()}))
        with pytest.raises(StateError, match='summarizing'):
            send_parts(repo, maildir)


class TestJobStages:
    """Each stage of a digest job, and resuming between them."""

    def test_collect_copies_messages_and_pointer(self, repo: InboxRepo) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=3))
        top = repo.add_msg('b@x', NOW - timedelta(hours=2))

        assert collect(repo)

        job = job_of(repo)
        state = job.load()
        assert state['stage'] == DigestJob.COLLECTED
        assert len(job.messages()) == 2
        assert state['pointer']['entry']['last'] == top
        assert state['pointer']['entry']['msgid'] == '<b@x>'
        assert state['period_end'] == NOW.isoformat()
        # Nothing is saved until the digest is delivered
        assert repo.feed().load_digest_sent(DNAME) is None

    def test_empty_period_makes_no_job(self, repo: InboxRepo) -> None:
        repo.add_msg('old@x', NOW - timedelta(days=3))

        assert not collect(repo)

        assert not job_of(repo).path.exists()
        assert repo.feed().load_digest_sent(DNAME) == NOW

    def test_later_stages_read_no_git(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        """After collect, the feed repositories are not needed at all.

        This is what lets a slow stage run without holding the feed lock,
        while kgl pull fetches and rewrites the feed.
        """
        top = repo.add_msg('a@x', NOW - timedelta(hours=3))
        assert collect(repo)
        repo.feed_dir.joinpath('git').rename(repo.feed_dir / 'git-away')

        digest = send(repo, maildir, now=NOW + timedelta(minutes=5))

        assert digest is not None
        assert 'a@x' in digest_text(digest)
        repo.feed_dir.joinpath('git-away').rename(repo.feed_dir / 'git')
        feed = repo.feed()
        assert feed.load_digest_sent(DNAME) == NOW
        assert feed.get_delivery_info_for_epoch(DNAME, 0)['last'] == top
        assert not job_of(repo).path.exists()

    def test_collected_job_keeps_its_period(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        """Messages that arrive after collect wait for the next digest."""
        repo.add_msg('a@x', NOW - timedelta(hours=3))
        assert collect(repo)
        repo.add_msg('late@x', NOW + timedelta(minutes=30))

        digest = send(repo, maildir, now=NOW + timedelta(hours=1))

        assert digest is not None
        assert 'a@x' in digest_text(digest)
        assert 'late@x' not in digest_text(digest)
        nxt = send(repo, maildir, now=NOW + timedelta(days=1))
        assert nxt is not None
        assert 'late@x' in digest_text(nxt)
        assert 'a@x' not in digest_text(nxt)

    def test_render_drops_the_messages(self, repo: InboxRepo) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=3))
        assert collect(repo)
        job = job_of(repo)

        render_digest_job(DNAME, repo.feed(), DAILY, job, now=NOW)

        assert job.load()['stage'] == DigestJob.RENDERED
        assert len(job.pending()) == 1
        assert not job.messages_dir.exists()

    def test_unfinished_render_is_done_again(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        """A render that stopped half way left parts, but the job is still collected."""
        repo.add_msg('a@x', NOW - timedelta(hours=3))
        assert collect(repo)
        job = job_of(repo)
        job.parts_dir.mkdir()
        (job.parts_dir / '0007.eml').write_bytes(b'Subject: half-written\n\n')

        digest = send(repo, maildir, now=NOW + timedelta(minutes=5))

        assert digest is not None
        assert [str(msg['Subject']) for msg in delivered(maildir)] == [str(digest['Subject'])]

    def test_bozofilter_is_applied_at_collect(self, repo: InboxRepo) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=3))
        repo.add_msg('b@x', NOW - timedelta(hours=2), sender='Bozo <bozo@example.com>')

        assert collect(repo, bozofilter={'bozo@example.com'})

        messages = job_of(repo).messages()
        assert len(messages) == 1
        assert b'bozo@example.com' not in messages[0]


def add_review(repo: InboxRepo, cover: str, subject: str, when: datetime = NOW - timedelta(hours=2)) -> None:
    """A review of patch 2 of a series whose cover letter is older than the period."""
    msgid = f'review-{cover}'
    repo.add(mkmsg(msgid, f'Re: {subject}', refs=[cover, f'p2-{cover}'], irt=f'p2-{cover}').as_bytes(), when)


class TestRootLookup:
    """The collect stage looks up the cover letters of continuing series."""

    def test_digest_is_named_after_the_cover(
        self, repo: InboxRepo, maildir: MaildirTarget, no_archive_lookups: MagicMock
    ) -> None:
        add_review(repo, 'cover@x', '[PATCH v3 2/7] mm: use the tail pointer')
        no_archive_lookups.side_effect = None
        no_archive_lookups.return_value = make_raw('cover@x', '[PATCH v3 0/7] mm: frobnicate the widgets')

        assert collect(repo)
        assert job_of(repo).load()['root_subjects'] == {'cover@x': '[PATCH v3 0/7] mm: frobnicate the widgets'}
        no_archive_lookups.assert_called_once_with('cover@x')
        digest = send(repo, maildir)

        text = digest_text(digest)
        assert '\n[PATCH v3 0/7] mm: frobnicate the widgets\n' in text
        assert 'on 2/7' in text

    def test_first_failure_stops_the_lookups(
        self, repo: InboxRepo, maildir: MaildirTarget, no_archive_lookups: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        add_review(repo, 'one@x', '[PATCH 2/3] mm: first series')
        add_review(repo, 'two@x', '[PATCH 2/3] net: second series')
        no_archive_lookups.side_effect = liblore.RemoteError('503 Service Unavailable')

        digest = send(repo, maildir)

        assert no_archive_lookups.call_count == 1
        assert 'cannot look up' in caplog.text
        # The digest still goes out, with the names it had before
        text = digest_text(digest)
        assert '\n[PATCH 2/3] mm: first series\n' in text
        assert '\n[PATCH 2/3] net: second series\n' in text

    def test_root_that_is_a_reply_is_left_out(self, repo: InboxRepo, no_archive_lookups: MagicMock) -> None:
        add_review(repo, 'cover@x', '[PATCH v8 16/23] dma-buf: routing')
        no_archive_lookups.side_effect = None
        no_archive_lookups.return_value = make_raw('cover@x', 'Re: [PATCH v8 16/23] dma-buf: routing')

        assert collect(repo)

        no_archive_lookups.assert_called_once_with('cover@x')
        assert job_of(repo).load()['root_subjects'] == {}

    def test_only_patch_titles_are_looked_up(self, repo: InboxRepo) -> None:
        """A thread already named after its cover letter needs no lookup (the fixture fails on any)."""
        add_review(repo, 'cover@x', '[PATCH v3 0/7] mm: frobnicate the widgets')
        repo.add_msg('new@x', NOW - timedelta(hours=1), subject='[PATCH 2/2] mm: a new series')

        assert collect(repo)

        assert job_of(repo).load()['root_subjects'] == {}

    def test_lei_feeds_do_not_look_up(self) -> None:
        feed = MagicMock(spec=LeiFeed)
        raw = mkmsg('review@x', 'Re: [PATCH 2/3] mm: a patch', refs=['cover@x', 'p2@x']).as_bytes()

        assert look_up_root_subjects(DNAME, feed, [raw]) == {}

    def test_job_without_root_subjects(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        """A job collected by an older version still renders."""
        repo.add_msg('a@x', NOW - timedelta(hours=3))
        assert collect(repo)
        job = job_of(repo)
        state = job.load()
        del state['root_subjects']
        (job.path / DigestJob.JOB_FILE).write_text(json.dumps(state))

        digest = send(repo, maildir, now=NOW + timedelta(minutes=5))

        assert 'a@x' in digest_text(digest)

    @pytest.mark.parametrize('bad', [{'cover@x': 7}, ['cover@x']], ids=['not-a-subject', 'not-a-map'])
    def test_bad_root_subjects(self, repo: InboxRepo, maildir: MaildirTarget, bad: Any) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=3))
        assert collect(repo)
        job = job_of(repo)
        state = job.load()
        state['root_subjects'] = bad
        (job.path / DigestJob.JOB_FILE).write_text(json.dumps(state))

        with pytest.raises(StateError, match='root_subjects'):
            send_parts(repo, maildir, now=NOW + timedelta(minutes=5))

    def test_lore_feed_fetches_through_its_node(self, tmp_path: Path, real_get_message: Any) -> None:
        node = MagicMock()
        node.get_message_by_msgid.return_value = b'Subject: hi\n\n'
        feed = LoreFeed('lkml', tmp_path, 'https://lore.kernel.org/lkml', lore_node=node)

        assert real_get_message(feed, 'cover@x') == b'Subject: hi\n\n'
        node.get_message_by_msgid.assert_called_once_with('cover@x')
