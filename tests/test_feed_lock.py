"""Tests for what happens when another process is using a feed.

kgl locks every feed it reads. When the timer's pull is running, a
command typed by hand finds the feeds busy. It should say so in one
line, not with a traceback, and leave no feed locked behind it.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from korgalore import FeedLockedError
from korgalore.cli import digest_cmd, lock_all_feeds, pull
from tests.digest_helpers import make_ctx

BUSY = FeedLockedError("Another kgl process is using feed 'git' (/x/git). Try again when it is done.")


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
