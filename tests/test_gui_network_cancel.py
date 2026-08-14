"""Tests for cancelling in-flight lore requests when the network drops.

run_sync() only checks network availability before it starts, so a sync
already in flight used to keep retrying every configured origin against a
network that was gone, waiting out the read timeout each time. The
network-changed callback now cancels the active LoreNodes instead.

These tests build a KorgaloreApp without GTK, the same way
test_gui_config_reload.py does.
"""

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

import liblore

if TYPE_CHECKING:
    from korgalore.gui import KorgaloreApp

import click


def _make_ctx(nodes: Dict[str, Any]) -> click.Context:
    """Build a minimal click.Context holding the given lore nodes."""
    ctx = click.Context(click.Command('test'))
    ctx.obj = {
        'config': {},
        'targets': {},
        'feeds': {},
        'deliveries': {},
        'lore_nodes': nodes,
    }
    return ctx


def _make_app(ctx: click.Context, sync_interval: int = 300) -> 'KorgaloreApp':
    """Construct a KorgaloreApp without GTK by stubbing __init__.

    Only the attributes the sync and network paths touch are set; the
    menu items and indicator are replaced with mocks.
    """
    from korgalore.gui import KorgaloreApp

    app = object.__new__(KorgaloreApp)
    app.ctx = ctx
    app.sync_interval = sync_interval
    app.is_syncing = False
    app.network_available = True
    app.next_sync_time = 0.0
    app.auth_needed_target = None
    app.ind = MagicMock()
    app.item_status = MagicMock()
    app.item_sync = MagicMock()
    app.item_next_sync = MagicMock()
    return app


def _mock_node() -> MagicMock:
    """A LoreNode stand-in that records cancel_active/shutdown calls."""
    return MagicMock(spec=liblore.LoreNode)


class TestNetworkLostCancelsSync:
    """The network-down callback cancels lore requests only when syncing."""

    def test_cancels_every_node_when_syncing(self) -> None:
        """Each cached node is cancelled, not shut down."""
        nodes = {'https://lore.kernel.org': _mock_node(), 'https://erol.kernel.org': _mock_node()}
        app = _make_app(_make_ctx(nodes))
        app.is_syncing = True

        with patch.object(app, 'update_status'):
            app._on_network_changed(MagicMock(), False)

        for node in nodes.values():
            node.cancel_active.assert_called_once_with()
            # shutdown() is terminal and reserved for quit(); the node has
            # to keep working once the network comes back.
            node.shutdown.assert_not_called()

    def test_does_not_cancel_when_idle(self) -> None:
        """With no sync in flight there is nothing to interrupt."""
        node = _mock_node()
        app = _make_app(_make_ctx({'https://lore.kernel.org': node}))
        app.is_syncing = False

        with patch.object(app, 'update_status'):
            app._on_network_changed(MagicMock(), False)

        node.cancel_active.assert_not_called()

    def test_network_restored_does_not_cancel(self) -> None:
        """Coming back online schedules a sync rather than cancelling one."""
        node = _mock_node()
        app = _make_app(_make_ctx({'https://lore.kernel.org': node}))
        app.network_available = False
        app.is_syncing = True

        with patch.object(app, 'update_status'):
            app._on_network_changed(MagicMock(), True)

        node.cancel_active.assert_not_called()
        assert app.next_sync_time == pytest.approx(time.time() + 10, abs=5)

    def test_still_reports_offline_status(self) -> None:
        """Cancelling does not replace the offline status update."""
        app = _make_app(_make_ctx({'https://lore.kernel.org': _mock_node()}))
        app.is_syncing = True

        with patch.object(app, 'update_status') as mock_status:
            app._on_network_changed(MagicMock(), False)

        mock_status.assert_called_once_with('Network unavailable', 'network-offline-symbolic')

    def test_failing_node_does_not_block_the_rest(self) -> None:
        """One uncooperative node must not strand the others mid-read."""
        bad = _mock_node()
        bad.cancel_active.side_effect = RuntimeError('boom')
        good = _mock_node()
        # dicts preserve insertion order, so the failing node comes first
        app = _make_app(_make_ctx({'bad': bad, 'good': good}))
        app.is_syncing = True

        with patch.object(app, 'update_status'):
            app._on_network_changed(MagicMock(), False)

        good.cancel_active.assert_called_once_with()

    def test_no_nodes_yet_is_harmless(self) -> None:
        """A sync can be flagged before any node has been created."""
        app = _make_app(_make_ctx({}))
        app.is_syncing = True

        with patch.object(app, 'update_status'):
            app._on_network_changed(MagicMock(), False)

        assert app.next_sync_time == 0.0


class TestSyncRescheduleAfterCancel:
    """run_sync() must not push back a sooner sync scheduled by a callback."""

    def _run_sync_with(self, app: 'KorgaloreApp', pull_side_effect: Any) -> None:
        """Run app.run_sync() with everything but the pull stubbed out."""
        with (
            patch('korgalore.gui.GLib'),
            patch('korgalore.gui.refresh_subfolder_templates'),
            patch('korgalore.gui.perform_pull', side_effect=pull_side_effect) as mock_pull,
            patch.object(app, '_check_reload_config'),
            patch.object(app, 'update_status'),
        ):
            app.run_sync()
            assert mock_pull.called, 'run_sync() returned before pulling'

    def test_network_flap_keeps_the_sooner_resync(self) -> None:
        """A down/up flap mid-sync leaves the 10-second resync in place."""
        node = _mock_node()
        app = _make_app(_make_ctx({'https://lore.kernel.org': node}), sync_interval=300)

        def _flap(*args: Any, **kwargs: Any) -> None:
            # The network drops and returns while the pull is in flight.
            app._on_network_changed(MagicMock(), False)
            app._on_network_changed(MagicMock(), True)
            raise liblore.OperationCancelledError('Request cancelled')

        self._run_sync_with(app, _flap)

        node.cancel_active.assert_called_once_with()
        # Without the guard in run_sync()'s finally block, this would have
        # been pushed out to now + 300 and the user would sit offline for
        # five minutes after reconnecting.
        assert app.next_sync_time == pytest.approx(time.time() + 10, abs=5)
        assert app.is_syncing is False

    def test_successful_sync_resets_to_full_interval(self) -> None:
        """The ordinary case still schedules one full interval ahead."""
        app = _make_app(_make_ctx({}), sync_interval=300)

        self._run_sync_with(app, lambda *a, **kw: (None, []))

        assert app.next_sync_time == pytest.approx(time.time() + 300, abs=5)

    def test_cancelled_sync_without_flap_uses_full_interval(self) -> None:
        """A cancellation with no callback rescheduling falls back to normal."""
        app = _make_app(_make_ctx({}), sync_interval=300)

        def _cancelled(*args: Any, **kwargs: Any) -> None:
            raise liblore.OperationCancelledError('Node is shut down')

        self._run_sync_with(app, _cancelled)

        assert app.next_sync_time == pytest.approx(time.time() + 300, abs=5)

    def test_cancellation_is_not_an_error(self) -> None:
        """A cancelled sync leaves no error status on the indicator."""
        app = _make_app(_make_ctx({}))
        statuses: List[str] = []

        def _cancelled(*args: Any, **kwargs: Any) -> None:
            raise liblore.OperationCancelledError('Request cancelled')

        with (
            patch('korgalore.gui.GLib'),
            patch('korgalore.gui.refresh_subfolder_templates'),
            patch('korgalore.gui.perform_pull', side_effect=_cancelled),
            patch.object(app, '_check_reload_config'),
            patch.object(app, 'update_status', side_effect=lambda s, i=None: statuses.append(s)),
        ):
            app.run_sync()

        assert not any(s.startswith('Error:') for s in statuses)


class TestQuitStillShutsDown:
    """quit() keeps using the terminal shutdown(), not cancel_active()."""

    def test_shutdown_used_on_quit(self, tmp_path: Path) -> None:
        node = _mock_node()
        app = _make_app(_make_ctx({'https://lore.kernel.org': node}))
        app.stop_event = MagicMock()
        app.cfgpath = tmp_path / 'korgalore.toml'

        with patch('korgalore.gui.Gtk'):
            app.quit()

        node.shutdown.assert_called_once_with()
        node.cancel_active.assert_not_called()
