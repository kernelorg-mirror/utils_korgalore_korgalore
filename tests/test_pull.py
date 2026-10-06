"""Tests for ``kgl pull`` and the cli functions it is built from.

Covers feed failures (``--fail-on-feed-error``), delivery state set up for
freshly cloned feeds, the lore node cache and the deliver_commit
regressions. A failed feed update is logged and skipped so the other feeds
still run. With --fail-on-feed-error, pull still does all its work, but
exits with status 3 at the end when any feed failed to update. Delivery
failures don't count.
"""

from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from korgalore import GitError, PublicInboxError, RemoteError, StateError
from korgalore.cli import (
    SKIPPED_NOOP_COMMIT,
    deliver_commit,
    get_lore_node,
    perform_pull,
    pull,
    update_all_feeds,
)
from korgalore.lore_feed import LoreFeed
from korgalore.pi_feed import PIFeed
from tests.digest_helpers import make_ctx


def _make_feed(feed_key: str, status: int = PIFeed.STATUS_UPDATED, error: Optional[Exception] = None) -> MagicMock:
    """Create a mock feed that updates with ``status`` or raises ``error``."""
    feed = MagicMock(spec=LoreFeed)
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


def _make_obj(
    feeds: Dict[str, MagicMock], with_deliveries: bool = True, labels: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Build ctx.obj with one delivery per feed, all to the same target."""
    target = MagicMock()
    target.identifier = 'test-target'
    deliveries: Dict[str, Tuple[Any, Any, List[str], Any]] = {}
    if with_deliveries:
        deliveries = {f'd-{key}': (feed, target, labels or [], None) for key, feed in feeds.items()}
    return {
        'config': {'deliveries': {d: {} for d in deliveries}},
        'feeds': feeds,
        'deliveries': deliveries,
        'targets': {},
        'bozofilter': set(),
        'hide_bar': True,
    }


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

    @pytest.mark.parametrize(
        ('status', 'updated', 'initialized'),
        [
            pytest.param(PIFeed.STATUS_NOCHANGE, [], [], id='nochange-in-neither-list'),
            pytest.param(PIFeed.STATUS_INITIALIZED, [], ['f'], id='initialized-in-second-list-only'),
            pytest.param(PIFeed.STATUS_UPDATED, ['f'], [], id='updated-in-first-list-only'),
            pytest.param(
                PIFeed.STATUS_UPDATED | PIFeed.STATUS_INITIALIZED, ['f'], ['f'], id='both-flags-in-both-lists'
            ),
        ],
    )
    def test_status_decides_the_list(self, status: int, updated: List[str], initialized: List[str]) -> None:
        """update_all_feeds returns (updated_feeds, initialized_feeds)."""
        ctx = make_ctx(_make_obj({'f': _make_feed('f', status)}))

        assert update_all_feeds(ctx) == (updated, initialized)

    def test_failed_feed_recorded_and_others_still_run(self) -> None:
        good_a = _make_feed('good-a')
        bad = _make_feed('bad', error=RemoteError('503 Service Unavailable'))
        good_b = _make_feed('good-b')
        ctx = make_ctx(_make_obj({'good-a': good_a, 'bad': bad, 'good-b': good_b}))

        updated, initialized = update_all_feeds(ctx)

        assert updated == ['good-a', 'good-b']
        assert initialized == []
        assert ctx.obj['failed_feeds'] == ['bad']
        good_b.update_feed.assert_called_once()

    @pytest.mark.parametrize('error', [RemoteError('boom'), PublicInboxError('lei up failed')])
    def test_each_handled_error_type_counts(self, error: Exception) -> None:
        ctx = make_ctx(_make_obj({'bad': _make_feed('bad', error=error)}))
        update_all_feeds(ctx)
        assert ctx.obj['failed_feeds'] == ['bad']

    def test_all_ok_gives_empty_list(self) -> None:
        ctx = make_ctx(_make_obj({'good': _make_feed('good')}))
        update_all_feeds(ctx)
        assert ctx.obj['failed_feeds'] == []

    def test_list_replaced_on_every_call(self) -> None:
        """A feed that recovers must not stay in the list (the GUI case)."""
        flaky = _make_feed('flaky')
        flaky.update_feed.side_effect = [RemoteError('timeout'), PIFeed.STATUS_NOCHANGE]
        ctx = make_ctx(_make_obj({'flaky': flaky}))

        update_all_feeds(ctx)
        assert ctx.obj['failed_feeds'] == ['flaky']
        update_all_feeds(ctx)
        assert ctx.obj['failed_feeds'] == []


class TestPerformPull:
    """perform_pull() keeps its return value and resets the list each pull."""

    def test_return_value_unchanged(self, pull_env: MagicMock) -> None:
        """gui.py unpacks perform_pull() as a 2-tuple."""
        feeds = {'good': _make_feed('good'), 'bad': _make_feed('bad', error=RemoteError('boom'))}
        ctx = make_ctx(_make_obj(feeds))

        changes, msgids = perform_pull(ctx, no_update=False, force=False, delivery_name=None)

        assert changes == {'d-good': 1}
        assert msgids == {'<d-good@example.org>'}
        assert ctx.obj['failed_feeds'] == ['bad']

    def test_failure_recorded_on_early_return(self, pull_env: MagicMock) -> None:
        """No deliveries to run still leaves the failed feeds in ctx.obj."""
        feeds = {'bad': _make_feed('bad', error=RemoteError('boom'))}
        ctx = make_ctx(_make_obj(feeds, with_deliveries=False))

        assert perform_pull(ctx, no_update=False, force=False, delivery_name=None) == ({}, set())
        assert ctx.obj['failed_feeds'] == ['bad']

    def test_no_update_resets_list(self, pull_env: MagicMock) -> None:
        """--no-update updates nothing, so an old failure must not linger."""
        ctx = make_ctx(_make_obj({'good': _make_feed('good')}))
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


class TestDeliveryStateInitOnClone:
    """perform_pull initialises delivery state for newly cloned feeds.

    When update_all_feeds() reports newly initialised feeds, delivery state
    is set up immediately so the next pull cycle can deliver new commits
    without wasting a run.
    """

    @staticmethod
    def _pull(
        feeds: Dict[str, MagicMock], initialized: List[str], no_update: bool = False
    ) -> Tuple[Any, Dict[str, MagicMock]]:
        """Pull with every feed lacking delivery state; return update mock and feeds."""
        for feed in feeds.values():
            feed.load_delivery_info.side_effect = StateError('no state')
        ctx = make_ctx(_make_obj(feeds, labels=['label']))
        with patch('korgalore.cli.update_all_feeds', return_value=([], initialized)) as mock_update:
            perform_pull(ctx, no_update=no_update, force=False, delivery_name=None)
        return mock_update, feeds

    def test_initialized_feed_gets_delivery_state(self, pull_env: MagicMock) -> None:
        """Every delivery of a newly initialised feed gets its state saved."""
        feed = _make_feed('new-feed')
        feed.feed_key = 'new-feed'
        target = MagicMock()
        deliveries = {
            'delivery-a': (feed, target, ['label-a'], None),
            'delivery-b': (feed, target, ['label-b'], None),
        }
        obj = _make_obj({'new-feed': feed}, with_deliveries=False)
        obj['deliveries'] = deliveries
        obj['config'] = {'deliveries': {d: {} for d in deliveries}}
        feed.load_delivery_info.side_effect = StateError('no state')

        with patch('korgalore.cli.update_all_feeds', return_value=([], ['new-feed'])):
            perform_pull(make_ctx(obj), no_update=False, force=False, delivery_name=None)

        assert sorted(c.args[0] for c in feed.save_delivery_info.call_args_list) == ['delivery-a', 'delivery-b']

    def test_existing_state_not_reinitialised(self, pull_env: MagicMock) -> None:
        """If delivery state already exists, save_delivery_info is not called."""
        feed = _make_feed('new-feed')
        ctx = make_ctx(_make_obj({'new-feed': feed}))

        with patch('korgalore.cli.update_all_feeds', return_value=([], ['new-feed'])):
            perform_pull(ctx, no_update=False, force=False, delivery_name=None)

        feed.load_delivery_info.assert_called_once_with('d-new-feed')
        feed.save_delivery_info.assert_not_called()

    def test_no_init_when_no_update(self, pull_env: MagicMock) -> None:
        """With no_update=True, no initialisation is attempted."""
        mock_update, feeds = self._pull({'new-feed': _make_feed('new-feed')}, ['new-feed'], no_update=True)

        mock_update.assert_not_called()
        feeds['new-feed'].load_delivery_info.assert_not_called()
        feeds['new-feed'].save_delivery_info.assert_not_called()

    def test_unrelated_feed_not_initialized(self, pull_env: MagicMock) -> None:
        """Deliveries for non-initialized feeds are not touched."""
        _, feeds = self._pull(
            {'init-feed': _make_feed('init-feed'), 'other-feed': _make_feed('other-feed')}, ['init-feed']
        )

        feeds['init-feed'].save_delivery_info.assert_called_once_with('d-init-feed')
        feeds['other-feed'].load_delivery_info.assert_not_called()
        feeds['other-feed'].save_delivery_info.assert_not_called()


class TestGetLoreNode:
    """get_lore_node() caches one node per origin."""

    @staticmethod
    def _make_ctx() -> click.Context:
        """Create a Click context with an empty lore_nodes cache."""
        return make_ctx({'lore_nodes': dict()})

    def test_same_origin_returns_same_node(self) -> None:
        """Two URLs on the same host return the same cached node."""
        ctx = self._make_ctx()
        with patch('korgalore.cli.make_lore_node') as mock_make:
            mock_make.return_value = MagicMock()
            node1 = get_lore_node(ctx, 'https://lore.kernel.org/lkml')
            node2 = get_lore_node(ctx, 'https://lore.kernel.org/netdev')

        assert node1 is node2
        # Only one node should have been created
        mock_make.assert_called_once()

    def test_different_origins_return_different_nodes(self) -> None:
        """URLs on different hosts get separate nodes, e.g. subspace next to lore."""
        ctx = self._make_ctx()
        with patch('korgalore.cli.make_lore_node') as mock_make:
            mock_make.side_effect = [MagicMock(name='lore'), MagicMock(name='subspace')]
            node_lore = get_lore_node(ctx, 'https://lore.kernel.org/lkml')
            node_subspace = get_lore_node(ctx, 'https://subspace.kernel.org/_lists/helpdesk')

        assert node_lore is not node_subspace
        assert mock_make.call_count == 2
        assert len(ctx.obj['lore_nodes']) == 2

    def test_default_url_is_lore(self) -> None:
        """Default call creates a lore.kernel.org node."""
        ctx = self._make_ctx()
        with patch('korgalore.cli.make_lore_node') as mock_make:
            mock_make.return_value = MagicMock()
            get_lore_node(ctx)

        mock_make.assert_called_once_with(url='https://lore.kernel.org/all')

    def test_node_closed_on_context_close(self) -> None:
        """Created nodes are closed when the context tears down."""
        ctx = self._make_ctx()
        mock_node = MagicMock()
        with patch('korgalore.cli.make_lore_node', return_value=mock_node):
            get_lore_node(ctx, 'https://lore.kernel.org/lkml')

        mock_node.close.assert_not_called()
        ctx.close()
        mock_node.close.assert_called_once()


class TestDeliverBadObjectCommit:
    """Regression: deliver_commit must handle bad-object commits gracefully.

    When a commit in the failed delivery list is no longer available
    locally (bad object / missing packfile), deliver_commit must not
    crash the entire retry loop.  It should record the failure and
    move on.

    See: c4de9f25-0c60-49e4-925f-7749eba57264@app.fastmail.com
    """

    def test_bad_object_during_retry_records_failure(self) -> None:
        """deliver_commit marks a bad-object commit as failed, not crashed."""
        feed = MagicMock()
        feed.is_noop_commit.side_effect = GitError('Bad object: deadbeef')

        result = deliver_commit('test-delivery', MagicMock(), feed, 0, 'deadbeef' * 5, ['label'], was_failing=True)

        assert result is None
        feed.mark_failed_delivery.assert_called_once_with('test-delivery', 0, 'deadbeef' * 5)


class TestRetryNoopDoesNotRewindPointer:
    """Regression: retrying a noop commit must not rewind the delivery pointer.

    When a noop commit (rm/purge) is in the failed list and gets retried,
    deliver_commit must pass was_failing through so the entry is removed
    from the failed list. (That mark_successful_delivery then does not
    save_delivery_info for retried commits is tested in test_pi_feed.py.)

    See: e77298ce-1e3e-449f-9864-b4fcf77a00b4@app.fastmail.com
    """

    def test_noop_retry_passes_was_failing(self) -> None:
        """deliver_commit passes was_failing to mark_successful_delivery for noops."""
        feed = MagicMock()
        feed.is_noop_commit.return_value = True

        result = deliver_commit('test-delivery', MagicMock(), feed, 0, 'abc123', ['label'], was_failing=True)

        assert result == SKIPPED_NOOP_COMMIT
        feed.mark_successful_delivery.assert_called_once_with('test-delivery', 0, 'abc123', was_failing=True)
