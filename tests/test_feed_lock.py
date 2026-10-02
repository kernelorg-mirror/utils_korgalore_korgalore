"""Tests for what happens when another process is using a feed.

kgl locks every feed it reads. When the timer's pull is running, a
command typed by hand finds the feeds busy. It should say so in one
line, not with a traceback, and leave no feed locked behind it.
"""

import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from korgalore import FeedLockedError, PublicInboxError
from korgalore.cli import digest_cmd, lock_all_feeds, pull
from korgalore.pi_feed import LOCKED_FEEDS, PIFeed
from tests.digest_helpers import make_ctx

# Holds the lock in another process, because a process never blocks on
# its own POSIX locks
_HOLD_LOCK = """
import sys
from fcntl import LOCK_EX, lockf
fh = open(sys.argv[1], 'w')
lockf(fh, LOCK_EX)
print('locked', flush=True)
sys.stdin.read()
"""

BUSY = FeedLockedError("Another kgl process is using feed 'git' (/x/git). Try again when it is done.")


@pytest.fixture
def held_lock(temp_feed_dir: Path) -> Iterator[None]:
    """Another process holds the lock of temp_feed_dir until the test ends."""
    holder = subprocess.Popen(
        [sys.executable, '-c', _HOLD_LOCK, str(temp_feed_dir / 'korgalore.lock')],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline() == 'locked\n'
        yield
    finally:
        holder.communicate('')


class TestFeedLock:
    """Tests for PIFeed.feed_lock() when the feed is busy."""

    @pytest.mark.usefixtures('held_lock')
    def test_busy_feed(self, mock_feed: PIFeed, temp_feed_dir: Path) -> None:
        """The error names the feed and says what to do."""
        with pytest.raises(FeedLockedError) as info:
            mock_feed.feed_lock()
        assert str(info.value) == (
            f"Another kgl process is using feed 'test-feed' ({temp_feed_dir}). Try again when it is done."
        )
        assert str(temp_feed_dir) not in LOCKED_FEEDS

    def test_still_a_public_inbox_error(self) -> None:
        """Code that catches PublicInboxError keeps working."""
        assert issubclass(FeedLockedError, PublicInboxError)


class TestLockAllFeeds:
    """Tests for lock_all_feeds()."""

    def test_all_locked(self) -> None:
        feeds = {'a': MagicMock(), 'b': MagicMock()}
        lock_all_feeds(make_ctx({'feeds': feeds}))
        for feed in feeds.values():
            feed.feed_lock.assert_called_once()
            feed.feed_unlock.assert_not_called()

    def test_busy_feed_releases_the_others(self) -> None:
        """The feeds locked before the busy one are unlocked again."""
        first, busy, last = MagicMock(), MagicMock(), MagicMock()
        busy.feed_lock.side_effect = BUSY
        with pytest.raises(FeedLockedError):
            lock_all_feeds(make_ctx({'feeds': {'first': first, 'busy': busy, 'last': last}}))
        first.feed_unlock.assert_called_once()
        busy.feed_unlock.assert_not_called()
        last.feed_lock.assert_not_called()
        last.feed_unlock.assert_not_called()

    def test_other_errors_pass_through(self) -> None:
        """Only a busy feed is handled here."""
        first, broken = MagicMock(), MagicMock()
        broken.feed_lock.side_effect = OSError('disk full')
        with pytest.raises(OSError):
            lock_all_feeds(make_ctx({'feeds': {'first': first, 'broken': broken}}))
        first.feed_unlock.assert_not_called()


class TestCommands:
    """kgl pull and kgl digest print one line for a busy feed."""

    @staticmethod
    def _check(result: Any, caplog: pytest.LogCaptureFixture) -> None:
        assert result.exit_code == 1
        # click.Abort, not the FeedLockedError itself
        assert isinstance(result.exception, SystemExit)
        assert 'Traceback' not in result.output
        assert caplog.messages[-1] == f'Error: {BUSY}'

    def test_digest(self, caplog: pytest.LogCaptureFixture) -> None:
        obj = {'config': {'deliveries': {'d': {'feed': 'git', 'target': 'local', 'mode': 'digest'}}}, 'targets': {}}
        with (
            patch('korgalore.cli.map_deliveries'),
            patch('korgalore.cli.lock_all_feeds', side_effect=BUSY),
            patch('korgalore.cli.run_digest_estimates') as estimate,
        ):
            result = CliRunner().invoke(digest_cmd, ['--estimate'], obj=obj)
        self._check(result, caplog)
        estimate.assert_not_called()

    def test_pull(self, caplog: pytest.LogCaptureFixture) -> None:
        with patch('korgalore.cli.perform_pull', side_effect=BUSY):
            result = CliRunner().invoke(pull, [], obj={})
        self._check(result, caplog)
