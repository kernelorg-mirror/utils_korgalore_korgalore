"""Tests for keeping enough lore history for digests.

Lore clones are shallow, and a normal fetch moves the cut to one week
back. A digest that was last sent longer ago than that asks the fetch to
keep its history. The real-git tests use shallow mirror clones over
file://, with absolute dates, because the test commits have made-up
dates.
"""

import json
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from korgalore import RemoteError, StateError
from korgalore.cli import digest_history_needs, send_digest, update_all_feeds
from korgalore.lei_feed import LeiFeed
from korgalore.lore_feed import HISTORY_MAX, LoreFeed
from korgalore.maildir_target import MaildirTarget
from korgalore.pi_feed import PIFeed
from tests.digest_helpers import DAILY, DNAME, NOW, UTC, InboxRepo, ShallowCopy, digest_text, make_ctx


def digest_body(msg: EmailMessage) -> str:
    """The plain text of a digest, with the line wrapping undone."""
    return ' '.join(digest_text(msg).split())


def write_info(feed_dir: Path, info: Dict[str, Any]) -> LoreFeed:
    feed_dir.mkdir(parents=True, exist_ok=True)
    (feed_dir / f'korgalore.{DNAME}.info').write_text(json.dumps(info))
    return LoreFeed('lkml', feed_dir, 'https://lore.kernel.org/lkml')


class TestShallowSince:
    def test_no_digest_uses_the_default(self) -> None:
        assert LoreFeed.shallow_since(None, NOW) is None

    def test_recent_digest_uses_the_default(self) -> None:
        assert LoreFeed.shallow_since(NOW - timedelta(days=3), NOW) is None

    def test_old_digest_keeps_its_history(self) -> None:
        assert LoreFeed.shallow_since(NOW - timedelta(days=10), NOW) == '2026-09-21 09:00:00 +0000'

    def test_very_old_digest_is_capped(self) -> None:
        assert HISTORY_MAX == timedelta(days=30)
        assert LoreFeed.shallow_since(NOW - timedelta(days=90), NOW) == '2026-09-01 09:00:00 +0000'


class TestFetchEpoch:
    """The git commands a fetch runs, with and without a digest's needs."""

    @staticmethod
    def fetch(tmp_path: Path, results: List[Any], keep: Optional[datetime] = None) -> List[List[str]]:
        feed = LoreFeed('lkml', tmp_path / 'lkml', 'https://lore.kernel.org/lkml', lore_node=MagicMock(origins=[]))
        with patch('korgalore.lore_feed.run_git_command', side_effect=results) as git:
            try:
                feed.fetch_epoch(0, keep, now=NOW)
            finally:
                calls: List[List[str]] = [c.args[1] for c in git.call_args_list]
        return calls

    def test_default_fetch(self, tmp_path: Path) -> None:
        calls = self.fetch(tmp_path, [(0, b'', b'')])
        assert calls == [['fetch', 'origin', '--shallow-since=1.week.ago', '--update-shallow']]

    def test_default_falls_back_to_depth_one(self, tmp_path: Path) -> None:
        calls = self.fetch(tmp_path, [(128, b'', b'no commits'), (0, b'', b'')])
        assert calls[1] == ['fetch', 'origin', '--depth=1', '--update-shallow']

    def test_digest_keeps_history(self, tmp_path: Path) -> None:
        calls = self.fetch(tmp_path, [(0, b'', b'')], keep=NOW - timedelta(days=10))
        assert calls == [['fetch', 'origin', '--shallow-since=2026-09-21 09:00:00 +0000', '--update-shallow']]

    def test_digest_fallback_never_cuts_history(self, tmp_path: Path) -> None:
        """--depth=1 would throw away what the digest needs, so do a plain fetch."""
        calls = self.fetch(tmp_path, [(128, b'', b'no commits'), (0, b'', b'')], keep=NOW - timedelta(days=10))
        assert calls[1] == ['fetch', 'origin']

    def test_fetch_failure(self, tmp_path: Path) -> None:
        with pytest.raises(RemoteError):
            self.fetch(tmp_path, [(128, b'', b'no'), (128, b'', b'still no')], keep=NOW - timedelta(days=10))

    def test_update_feed_passes_it_on(self, tmp_path: Path) -> None:
        feed = LoreFeed('lkml', tmp_path / 'lkml', 'https://lore.kernel.org/lkml', lore_node=MagicMock())
        keep = NOW - timedelta(days=10)
        with (
            patch.object(feed, 'load_feed_state', return_value={'epochs': {'0': {}}}),
            patch.object(feed, 'load_epochs_info', return_value=[(0, '/lkml/git/0.git', 'old')]),
            patch.object(feed, 'get_manifest_epochs', return_value=[(0, '/lkml/git/0.git', 'new')]),
            patch.object(feed, 'feed_updated', return_value=True),
            patch.object(feed, 'save_feed_state'),
            patch.object(feed, 'fetch_epoch') as fetch_epoch,
        ):
            assert feed.update_feed(keep_history_since=keep) == PIFeed.STATUS_UPDATED
        fetch_epoch.assert_called_once_with(0, keep)


class TestHistoryStart:
    """How far back one digest delivery needs the feed history."""

    def test_no_state(self, tmp_path: Path) -> None:
        feed = LoreFeed('lkml', tmp_path / 'lkml', 'https://lore.kernel.org/lkml')
        assert feed.get_digest_history_start(DNAME) is None

    def test_older_of_pointer_and_last_sent(self, tmp_path: Path) -> None:
        # The pointer is a message from before the digest was sent
        feed = write_info(
            tmp_path / 'lkml',
            {
                'epochs': {'0': {'last': 'abc', 'commit_date': '2026-09-01 06:00:00 +0000'}},
                'digest': {'last_sent': '2026-09-01T07:00:00+00:00'},
            },
        )
        assert feed.get_digest_history_start(DNAME) == datetime(2026, 8, 31, 6, 0, tzinfo=UTC)

    def test_highest_epoch_pointer(self, tmp_path: Path) -> None:
        feed = write_info(
            tmp_path / 'lkml',
            {
                'epochs': {
                    '0': {'last': 'a', 'commit_date': '2026-01-01 00:00:00 +0000'},
                    '1': {'last': 'b', 'commit_date': '2026-09-20 00:00:00 +0000'},
                },
                'digest': {'last_sent': '2026-09-21T07:00:00+00:00'},
            },
        )
        assert feed.get_digest_history_start(DNAME) == datetime(2026, 9, 19, 0, 0, tzinfo=UTC)

    def test_empty_feed_has_only_last_sent(self, tmp_path: Path) -> None:
        feed = write_info(tmp_path / 'lkml', {'epochs': {}, 'digest': {'last_sent': '2026-09-21T07:00:00+00:00'}})
        assert feed.get_digest_history_start(DNAME) == datetime(2026, 9, 20, 7, 0, tzinfo=UTC)

    def test_bad_commit_date(self, tmp_path: Path) -> None:
        feed = write_info(tmp_path / 'lkml', {'epochs': {'0': {'last': 'a', 'commit_date': 'yesterday'}}})
        with pytest.raises(StateError):
            feed.get_digest_history_start(DNAME)


def lore_mock(feed_key: str) -> MagicMock:
    feed = MagicMock(spec=LoreFeed)
    feed.feed_key = feed_key
    feed.STATUS_UPDATED = PIFeed.STATUS_UPDATED
    feed.STATUS_INITIALIZED = PIFeed.STATUS_INITIALIZED
    feed.update_feed.return_value = PIFeed.STATUS_NOCHANGE
    return feed


class TestHistoryNeeds:
    def test_oldest_digest_per_feed_wins(self) -> None:
        lkml, netdev = lore_mock('lkml'), lore_mock('netdev')
        lkml.get_digest_history_start.side_effect = lambda d: {
            'a': NOW - timedelta(days=9),
            'b': NOW - timedelta(days=20),
        }[d]
        netdev.get_digest_history_start.return_value = None
        obj = {
            'deliveries': {
                'a': (lkml, None, [], None),
                'b': (lkml, None, [], None),
                'c': (netdev, None, [], None),
                'msgs': (lkml, None, [], None),
            },
            'digest_schedules': {'a': DAILY, 'b': DAILY, 'c': DAILY},
        }
        assert digest_history_needs(make_ctx(obj)) == {'lkml': NOW - timedelta(days=20)}
        # Message deliveries never ask for history
        assert [c.args[0] for c in lkml.get_digest_history_start.call_args_list] == ['a', 'b']

    def test_lei_feeds_are_skipped(self) -> None:
        lei = MagicMock(spec=LeiFeed)
        obj = {'deliveries': {'a': (lei, None, [], None)}, 'digest_schedules': {'a': DAILY}}
        assert digest_history_needs(make_ctx(obj)) == {}
        lei.get_digest_history_start.assert_not_called()

    def test_bad_state_is_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        lkml = lore_mock('lkml')
        lkml.get_digest_history_start.side_effect = StateError('Bad commit_date')
        obj = {'deliveries': {'a': (lkml, None, [], None)}, 'digest_schedules': {'a': DAILY}}
        assert digest_history_needs(make_ctx(obj)) == {}
        assert 'Bad commit_date' in caplog.text

    def test_update_all_feeds_passes_needs(self) -> None:
        lkml, netdev = lore_mock('lkml'), lore_mock('netdev')
        lkml.get_digest_history_start.return_value = NOW - timedelta(days=20)
        obj = {
            'feeds': {'lkml': lkml, 'netdev': netdev},
            'deliveries': {'a': (lkml, None, [], None), 'b': (netdev, None, [], None)},
            'digest_schedules': {'a': DAILY},
            'hide_bar': True,
        }
        update_all_feeds(make_ctx(obj))
        lkml.update_feed.assert_called_once_with(keep_history_since=NOW - timedelta(days=20))
        netdev.update_feed.assert_called_once_with()


class TestRealHistory:
    """The four-week vacation, with real shallow clones."""

    @pytest.fixture
    def upstream(self, tmp_path: Path) -> InboxRepo:
        return InboxRepo(tmp_path / 'upstream')

    @staticmethod
    def first_digest(upstream: InboxRepo, tmp_path: Path, days_ago: int) -> ShallowCopy:
        """A feed whose last digest went out days_ago days before NOW."""
        sent = NOW - timedelta(days=days_ago)
        upstream.add_msg('older@x', sent - timedelta(hours=3))
        upstream.add_msg('before@x', sent - timedelta(hours=1))
        local = ShallowCopy(upstream, tmp_path / 'lkml', since=sent - timedelta(days=5))
        maildir = MaildirTarget('local', str(tmp_path / 'first'))
        assert send_digest(DNAME, local.feed(), maildir, [], None, DAILY, now=sent)
        return local

    @staticmethod
    def fetch_for_digest(local: ShallowCopy) -> None:
        feed = local.feed()
        feed.fetch_epoch(0, feed.get_digest_history_start(DNAME), now=NOW)

    @staticmethod
    def next_digest(local: ShallowCopy, tmp_path: Path) -> str:
        maildir = MaildirTarget('local', str(tmp_path / 'next'))
        parts = send_digest(DNAME, local.feed(), maildir, [], None, DAILY, now=NOW)
        assert len(parts) == 1
        return digest_body(parts[0])

    def test_vacation_loses_nothing(self, tmp_path: Path, upstream: InboxRepo) -> None:
        local = self.first_digest(upstream, tmp_path, days_ago=28)
        upstream.add_msg('vacation@x', NOW - timedelta(days=20))
        upstream.add_msg('recent@x', NOW - timedelta(days=3))

        self.fetch_for_digest(local)

        text = self.next_digest(local, tmp_path)
        assert 'vacation@x' in text
        assert 'recent@x' in text
        assert 'Some messages are missing' not in text

    def test_lost_history_is_fetched_back(self, tmp_path: Path, upstream: InboxRepo) -> None:
        """A feed that already lost history (older korgalore) gets it back."""
        local = self.first_digest(upstream, tmp_path, days_ago=28)
        upstream.add_msg('vacation@x', NOW - timedelta(days=20))
        upstream.add_msg('recent@x', NOW - timedelta(days=3))
        local.fetch(since=NOW - timedelta(days=7))
        feed = local.feed()
        assert feed.find_history_gap(DNAME, feed.get_latest_commits_for_delivery(DNAME)) is not None

        self.fetch_for_digest(local)

        text = self.next_digest(local, tmp_path)
        assert 'vacation@x' in text
        assert 'Some messages are missing' not in text

    def test_very_long_vacation_is_capped(self, tmp_path: Path, upstream: InboxRepo) -> None:
        """Past HISTORY_MAX, older messages stay out and the digest says so."""
        local = self.first_digest(upstream, tmp_path, days_ago=45)
        upstream.add_msg('too-old@x', NOW - timedelta(days=40))
        upstream.add_msg('vacation@x', NOW - timedelta(days=20))
        upstream.add_msg('recent@x', NOW - timedelta(days=3))
        local.fetch(since=NOW - timedelta(days=7))

        self.fetch_for_digest(local)

        text = self.next_digest(local, tmp_path)
        assert 'too-old@x' not in text
        assert 'vacation@x' in text
        assert 'Some messages are missing. Korgalore only has messages from 2026-09-11' in text


class TestHistoryGap:
    """A pointer older than the shallow cut means missing messages.

    Each test starts with an older commit before the pointer: a pointer
    with no parent looks like a rebased feed to korgalore.
    """

    @pytest.fixture
    def upstream(self, tmp_path: Path) -> InboxRepo:
        return InboxRepo(tmp_path / 'upstream')

    def test_gap_is_found(self, tmp_path: Path, upstream: InboxRepo) -> None:
        # Four weeks of vacation: the cut moves past the pointer
        upstream.add_msg('older@x', NOW - timedelta(days=29))
        pointer = upstream.add_msg('before@x', NOW - timedelta(days=28))
        local = ShallowCopy(upstream, tmp_path / 'lkml', since=NOW - timedelta(days=35))
        local.feed().save_delivery_info(DNAME, 0, pointer, digest_sent=NOW - timedelta(days=28))
        upstream.add_msg('lost@x', NOW - timedelta(days=20))
        after = NOW - timedelta(days=3)
        upstream.add_msg('kept@x', after)
        local.fetch(since=NOW - timedelta(days=7))

        feed = local.feed()
        commits = feed.get_latest_commits_for_delivery(DNAME)
        assert len(commits) == 1  # lost@x is hidden by the cut
        assert feed.find_history_gap(DNAME, commits) == after

    def test_cut_right_after_pointer_is_no_gap(self, tmp_path: Path, upstream: InboxRepo) -> None:
        """The first commit after the cut can be the one right after the pointer."""
        upstream.add_msg('older@x', NOW - timedelta(days=10, hours=1))
        pointer = upstream.add_msg('before@x', NOW - timedelta(days=10))
        local = ShallowCopy(upstream, tmp_path / 'lkml', since=NOW - timedelta(days=11))
        local.feed().save_delivery_info(DNAME, 0, pointer, digest_sent=NOW - timedelta(days=10))
        upstream.add_msg('next@x', NOW - timedelta(days=3))
        local.fetch(since=NOW - timedelta(days=7))

        feed = local.feed()
        assert (local.gitdir / 'shallow').exists()
        commits = feed.get_latest_commits_for_delivery(DNAME)
        assert len(commits) == 1
        assert feed.find_history_gap(DNAME, commits) is None

    def test_pointer_inside_history_is_no_gap(self, tmp_path: Path, upstream: InboxRepo) -> None:
        upstream.add_msg('old@x', NOW - timedelta(days=6))
        pointer = upstream.add_msg('before@x', NOW - timedelta(days=2))
        local = ShallowCopy(upstream, tmp_path / 'lkml', since=NOW - timedelta(days=7))
        local.feed().save_delivery_info(DNAME, 0, pointer, digest_sent=NOW - timedelta(days=2))
        upstream.add_msg('next@x', NOW - timedelta(days=1))
        local.fetch(since=NOW - timedelta(days=7))

        feed = local.feed()
        commits = feed.get_latest_commits_for_delivery(DNAME)
        assert feed.find_history_gap(DNAME, commits) is None

    def test_full_clone_is_no_gap(self, repo: InboxRepo) -> None:
        pointer = repo.add_msg('a@x', NOW - timedelta(days=30))
        repo.feed().save_delivery_info(DNAME, 0, pointer, digest_sent=NOW - timedelta(days=30))
        repo.add_msg('b@x', NOW - timedelta(days=1))
        feed = repo.feed()
        assert feed.find_history_gap(DNAME, feed.get_latest_commits_for_delivery(DNAME)) is None

    def test_digest_says_messages_are_missing(self, tmp_path: Path, upstream: InboxRepo) -> None:
        upstream.add_msg('older@x', NOW - timedelta(days=29))
        upstream.add_msg('before@x', NOW - timedelta(days=28, hours=1))
        local = ShallowCopy(upstream, tmp_path / 'lkml', since=NOW - timedelta(days=35))
        maildir = MaildirTarget('local', str(tmp_path / 'mail'))
        sent = NOW - timedelta(days=28)
        send_digest(DNAME, local.feed(), maildir, [], None, DAILY, now=sent)
        upstream.add_msg('lost@x', NOW - timedelta(days=20))
        upstream.add_msg('kept@x', NOW - timedelta(days=3))
        local.fetch(since=NOW - timedelta(days=7))

        parts = send_digest(DNAME, local.feed(), maildir, [], None, DAILY, now=NOW)

        assert len(parts) == 1
        text = digest_body(parts[0])
        assert 'Some messages are missing.' in text
        assert 'kept@x' in text
        assert 'lost@x' not in text

    def test_gap_with_no_new_threads_is_still_sent(self, tmp_path: Path, upstream: InboxRepo) -> None:
        """The only news is that something is missing, and that is news."""
        upstream.add_msg('older@x', NOW - timedelta(days=29))
        upstream.add_msg('before@x', NOW - timedelta(days=28, hours=1))
        local = ShallowCopy(upstream, tmp_path / 'lkml', since=NOW - timedelta(days=35))
        maildir = MaildirTarget('local', str(tmp_path / 'mail'))
        send_digest(DNAME, local.feed(), maildir, [], None, DAILY, now=NOW - timedelta(days=28))
        upstream.add_msg('lost@x', NOW - timedelta(days=20))
        # The only commit after the cut is a deletion, so there are no threads
        upstream.add(b'', NOW - timedelta(days=3), filename='d')
        local.fetch(since=NOW - timedelta(days=7))

        parts = send_digest(DNAME, local.feed(), maildir, [], None, DAILY, now=NOW)

        assert len(parts) == 1
        assert 'Some messages are missing.' in digest_text(parts[0])
