"""Tests for ``kgl pull --fail-on-feed-error``.

A failed feed update is logged and skipped so the other feeds still
run. With --fail-on-feed-error, pull still does all its work, but exits
with status 3 at the end when any feed failed to update. Delivery
failures don't count.
"""

from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from korgalore import PublicInboxError, RemoteError
from korgalore.cli import perform_pull, pull, update_all_feeds
from korgalore.pi_feed import PIFeed


def _make_feed(feed_key: str, status: int = PIFeed.STATUS_UPDATED, error: Optional[Exception] = None) -> MagicMock:
    """Create a mock feed that updates with ``status`` or raises ``error``."""
    feed = MagicMock()
    feed.feed_key = feed_key
    feed.feed_url = f'https://example.com/{feed_key}'
    if error is not None:
        feed.update_feed.side_effect = error
    else:
        feed.update_feed.return_value = status
    feed.get_latest_commits_for_delivery.return_value = [(0, f'{feed_key}-commit')]
    feed.STATUS_UPDATED = PIFeed.STATUS_UPDATED
    feed.STATUS_INITIALIZED = PIFeed.STATUS_INITIALIZED
    feed.STATUS_NOCHANGE = PIFeed.STATUS_NOCHANGE
    return feed


def _make_obj(feeds: Dict[str, MagicMock], with_deliveries: bool = True) -> Dict[str, Any]:
    """Build ctx.obj with one delivery per feed, all to the same target."""
    target = MagicMock()
    target.identifier = 'test-target'
    deliveries: Dict[str, Tuple[Any, Any, List[str], Any]] = {}
    if with_deliveries:
        deliveries = {f'd-{key}': (feed, target, [], None) for key, feed in feeds.items()}
    return {
        'config': {'deliveries': {d: {} for d in deliveries}},
        'feeds': feeds,
        'deliveries': deliveries,
        'targets': {},
        'bozofilter': set(),
        'hide_bar': True,
    }


def _make_ctx(obj: Dict[str, Any]) -> click.Context:
    ctx = click.Context(click.Command('test'))
    ctx.obj = obj
    return ctx


@pytest.fixture
def pull_env() -> Iterator[MagicMock]:
    """Stub out config mapping, locking and state; yield the deliver_commit mock.

    By default every delivery succeeds and returns a fresh message-id.
    """
    mock_deliver = MagicMock(side_effect=lambda dname, *args, **kwargs: f'<{dname}@example.org>')
    with (
        patch('korgalore.cli.map_deliveries'),
        patch('korgalore.cli.map_tracked_threads'),
        patch('korgalore.cli.lock_all_feeds'),
        patch('korgalore.cli.unlock_all_feeds'),
        patch('korgalore.cli.retry_all_failed_deliveries'),
        patch('korgalore.cli.update_tracked_thread_activity'),
        patch('korgalore.cli.close_requests_session'),
        patch('korgalore.cli.get_tracking_manifest'),
        patch('korgalore.cli.deliver_commit', mock_deliver),
    ):
        yield mock_deliver


class TestUpdateAllFeedsRecordsFailures:
    """update_all_feeds() keeps a list of failed feeds in ctx.obj."""

    def test_failed_feed_recorded_and_others_still_run(self) -> None:
        good_a = _make_feed('good-a')
        bad = _make_feed('bad', error=RemoteError('503 Service Unavailable'))
        good_b = _make_feed('good-b')
        ctx = _make_ctx(_make_obj({'good-a': good_a, 'bad': bad, 'good-b': good_b}))

        updated, initialized = update_all_feeds(ctx)

        assert updated == ['good-a', 'good-b']
        assert initialized == []
        assert ctx.obj['failed_feeds'] == ['bad']
        good_b.update_feed.assert_called_once()

    @pytest.mark.parametrize('error', [RemoteError('boom'), PublicInboxError('lei up failed')])
    def test_each_handled_error_type_counts(self, error: Exception) -> None:
        ctx = _make_ctx(_make_obj({'bad': _make_feed('bad', error=error)}))
        update_all_feeds(ctx)
        assert ctx.obj['failed_feeds'] == ['bad']

    def test_all_ok_gives_empty_list(self) -> None:
        ctx = _make_ctx(_make_obj({'good': _make_feed('good')}))
        update_all_feeds(ctx)
        assert ctx.obj['failed_feeds'] == []

    def test_list_replaced_on_every_call(self) -> None:
        """A feed that recovers must not stay in the list (the GUI case)."""
        flaky = _make_feed('flaky')
        flaky.update_feed.side_effect = [RemoteError('timeout'), PIFeed.STATUS_NOCHANGE]
        ctx = _make_ctx(_make_obj({'flaky': flaky}))

        update_all_feeds(ctx)
        assert ctx.obj['failed_feeds'] == ['flaky']
        update_all_feeds(ctx)
        assert ctx.obj['failed_feeds'] == []


class TestPerformPull:
    """perform_pull() keeps its return value and resets the list each pull."""

    def test_return_value_unchanged(self, pull_env: MagicMock) -> None:
        """gui.py unpacks perform_pull() as a 2-tuple."""
        feeds = {'good': _make_feed('good'), 'bad': _make_feed('bad', error=RemoteError('boom'))}
        ctx = _make_ctx(_make_obj(feeds))

        result = perform_pull(ctx, no_update=False, force=False, delivery_name=None)

        assert isinstance(result, tuple) and len(result) == 2
        changes, msgids = result
        assert changes == {'d-good': 1}
        assert msgids == {'<d-good@example.org>'}
        assert ctx.obj['failed_feeds'] == ['bad']

    def test_failure_recorded_on_early_return(self, pull_env: MagicMock) -> None:
        """No deliveries to run still leaves the failed feeds in ctx.obj."""
        feeds = {'bad': _make_feed('bad', error=RemoteError('boom'))}
        ctx = _make_ctx(_make_obj(feeds, with_deliveries=False))

        assert perform_pull(ctx, no_update=False, force=False, delivery_name=None) == ({}, set())
        assert ctx.obj['failed_feeds'] == ['bad']

    def test_repeated_pulls_on_same_ctx(self, pull_env: MagicMock) -> None:
        """A feed that failed once and then worked is not reported again."""
        flaky = _make_feed('flaky')
        flaky.update_feed.side_effect = [RemoteError('timeout'), PIFeed.STATUS_UPDATED]
        ctx = _make_ctx(_make_obj({'flaky': flaky}))

        perform_pull(ctx, no_update=False, force=False, delivery_name=None)
        assert ctx.obj['failed_feeds'] == ['flaky']
        perform_pull(ctx, no_update=False, force=False, delivery_name=None)
        assert ctx.obj['failed_feeds'] == []

    def test_no_update_resets_list(self, pull_env: MagicMock) -> None:
        """--no-update updates nothing, so an old failure must not linger."""
        ctx = _make_ctx(_make_obj({'good': _make_feed('good')}))
        ctx.obj['failed_feeds'] = ['stale']

        perform_pull(ctx, no_update=True, force=False, delivery_name=None)

        assert ctx.obj['failed_feeds'] == []


class TestPullExitStatus:
    """The exit status of the pull command itself."""

    @staticmethod
    def _invoke(obj: Dict[str, Any], *args: str) -> Any:
        return CliRunner().invoke(pull, list(args), obj=obj)

    def test_feed_failure_exits_3(self, pull_env: MagicMock) -> None:
        feeds = {
            'good-a': _make_feed('good-a'),
            'bad': _make_feed('bad', error=RemoteError('503 Service Unavailable')),
            'good-b': _make_feed('good-b'),
        }
        result = self._invoke(_make_obj(feeds), '--fail-on-feed-error')

        assert result.exit_code == 3, result.output
        assert 'Feeds that failed to update:' in result.output
        assert '  bad' in result.output
        # The other feeds and their deliveries still ran
        delivered = sorted(c.args[0] for c in pull_env.call_args_list)
        assert delivered == ['d-good-a', 'd-good-b']

    def test_feed_failure_without_flag_exits_0(self, pull_env: MagicMock) -> None:
        feeds = {'good': _make_feed('good'), 'bad': _make_feed('bad', error=RemoteError('boom'))}
        result = self._invoke(_make_obj(feeds))

        assert result.exit_code == 0, result.output
        assert 'Feeds that failed to update:' not in result.output

    def test_all_ok_exits_0(self, pull_env: MagicMock) -> None:
        result = self._invoke(_make_obj({'good': _make_feed('good')}), '--fail-on-feed-error')
        assert result.exit_code == 0, result.output

    def test_delivery_failure_does_not_count(self, pull_env: MagicMock) -> None:
        """A failed delivery doesn't change what the feed archive holds."""
        pull_env.side_effect = None
        pull_env.return_value = None  # deliver_commit() returns None on failure
        result = self._invoke(_make_obj({'good': _make_feed('good')}), '--fail-on-feed-error')

        pull_env.assert_called_once()
        assert result.exit_code == 0, result.output

    def test_no_update_exits_0(self, pull_env: MagicMock) -> None:
        bad = _make_feed('bad', error=RemoteError('boom'))
        result = self._invoke(_make_obj({'bad': bad}), '--no-update', '--fail-on-feed-error')

        assert result.exit_code == 0, result.output
        bad.update_feed.assert_not_called()

    def test_help_says_it_does_not_stop_early(self) -> None:
        result = CliRunner().invoke(pull, ['--help'])
        assert result.exit_code == 0
        assert '--fail-on-feed-error' in result.output
        assert 'still run' in result.output
