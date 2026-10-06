"""Tests for the GUI without GTK: config reload and network cancellation.

Config change detection and reload logic exercises _get_config_mtime,
_check_reload_config and the mtime update in _run_edit_config.

Cancelling in-flight lore requests when the network drops: run_sync() only
checks network availability before it starts, so a sync already in flight used
to keep retrying every configured origin against a network that was gone,
waiting out the read timeout each time. The network-changed callback now
cancels the active LoreNodes instead.
"""

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

import liblore
from tests.gui_helpers import make_app

if TYPE_CHECKING:
    from korgalore.gui import KorgaloreApp


def _bump_mtime(path: Path) -> float:
    """Push path's mtime 100 seconds into the future and return it."""
    future = time.time() + 100
    os.utime(path, (future, future))
    return future


class TestGetConfigMtime:
    """Tests for _get_config_mtime."""

    def test_returns_mtime_of_main_config(self, tmp_path: Path) -> None:
        # No conf.d directory at all: the main file's mtime, without raising
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')
        app = make_app(cfgpath=cfgpath)

        mtime = app._get_config_mtime()
        assert mtime == pytest.approx(cfgpath.stat().st_mtime)

    def test_returns_newest_across_conf_d(self, tmp_path: Path) -> None:
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')
        conf_d = tmp_path / 'conf.d'
        conf_d.mkdir()

        # Create two conf.d files with different mtimes
        (conf_d / 'a.toml').write_text('[targets]\n')
        f2 = conf_d / 'b.toml'
        f2.write_text('[feeds]\n')
        future = _bump_mtime(f2)

        app = make_app(cfgpath=cfgpath)

        assert app._get_config_mtime() == pytest.approx(future)

    def test_detects_conf_d_directory_change(self, tmp_path: Path) -> None:
        """Adding a file to conf.d changes the directory mtime."""
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')
        conf_d = tmp_path / 'conf.d'
        conf_d.mkdir()

        app = make_app(cfgpath=cfgpath)
        mtime_before = app._get_config_mtime()

        # Ensure wall-clock advances so the new file has a later mtime
        new_file = conf_d / 'new.toml'
        new_file.write_text('[targets]\n')
        _bump_mtime(new_file)
        _bump_mtime(conf_d)

        assert app._get_config_mtime() > mtime_before

    def test_missing_config_returns_zero(self, tmp_path: Path) -> None:
        app = make_app(cfgpath=tmp_path / 'does-not-exist.toml')

        assert app._get_config_mtime() == 0.0

    def test_ignores_non_toml_in_conf_d(self, tmp_path: Path) -> None:
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')
        conf_d = tmp_path / 'conf.d'
        conf_d.mkdir()

        # Non-toml file with a very high mtime should be ignored
        txt = conf_d / 'notes.txt'
        txt.write_text('hello')
        future = time.time() + 200
        os.utime(txt, (future, future))

        app = make_app(cfgpath=cfgpath)

        # mtime should not include the .txt file
        assert app._get_config_mtime() < future


class TestCheckReloadConfig:
    """Tests for _check_reload_config."""

    @patch('korgalore.gui.load_config')
    @patch('korgalore.gui.validate_config_file')
    def test_no_reload_when_unchanged(self, mock_validate: MagicMock, mock_load: MagicMock, tmp_path: Path) -> None:
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')
        app = make_app({'gui': {'sync_interval': 300}}, cfgpath)

        app._check_reload_config()

        mock_validate.assert_not_called()
        mock_load.assert_not_called()

    @patch('korgalore.gui.load_config')
    @patch('korgalore.gui.validate_config_file', return_value=(True, ''))
    def test_reloads_when_mtime_changes(self, mock_validate: MagicMock, mock_load: MagicMock, tmp_path: Path) -> None:
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')
        new_config = {'gui': {'sync_interval': 600}, 'targets': {}}
        mock_load.return_value = new_config

        app = make_app({'gui': {'sync_interval': 300}}, cfgpath)
        ctx = app.ctx
        # Cached instances from the old config must be dropped
        ctx.obj['targets'] = {'t1': MagicMock()}
        ctx.obj['feeds'] = {'f1': MagicMock()}
        ctx.obj['deliveries'] = {'d1': MagicMock()}

        # Simulate external modification
        _bump_mtime(cfgpath)

        app._check_reload_config()

        mock_validate.assert_called_once_with(cfgpath)
        mock_load.assert_called_once_with(cfgpath)
        assert ctx.obj['config'] is new_config
        assert ctx.obj['targets'] == {}
        assert ctx.obj['feeds'] == {}
        assert ctx.obj['deliveries'] == {}
        assert app.sync_interval == 600

    @pytest.mark.parametrize(
        'validate_result',
        [(True, ''), (False, 'syntax error')],
        ids=['valid-config', 'validation-failure'],
    )
    @patch('korgalore.gui.load_config', return_value={'gui': {}})
    @patch('korgalore.gui.validate_config_file')
    def test_updates_stored_mtime(
        self, mock_validate: MagicMock, mock_load: MagicMock, tmp_path: Path, validate_result: tuple[bool, str]
    ) -> None:
        """Mtime is recorded even on failure, to avoid retrying every cycle."""
        mock_validate.return_value = validate_result
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')

        app = make_app(cfgpath=cfgpath)
        old_mtime = app._config_mtime

        future = _bump_mtime(cfgpath)

        app._check_reload_config()

        assert app._config_mtime > old_mtime
        assert app._config_mtime == pytest.approx(future)
        # Second call should neither reload nor retry
        mock_validate.reset_mock()
        app._check_reload_config()
        mock_validate.assert_not_called()

    @patch('korgalore.gui.load_config')
    @patch('korgalore.gui.validate_config_file', return_value=(False, 'syntax error'))
    def test_keeps_old_config_on_validation_failure(
        self, mock_validate: MagicMock, mock_load: MagicMock, tmp_path: Path
    ) -> None:
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')
        original_config: dict[str, Any] = {'gui': {'sync_interval': 300}}

        app = make_app(original_config, cfgpath)

        _bump_mtime(cfgpath)

        app._check_reload_config()

        mock_validate.assert_called_once()
        mock_load.assert_not_called()
        # Original config should be preserved
        assert app.ctx.obj['config'] is original_config


class TestEditConfigMtimeUpdate:
    """Test that _run_edit_config updates _config_mtime after reload."""

    @patch('korgalore.gui.load_config')
    @patch('korgalore.gui.validate_config_file', return_value=(True, ''))
    @patch('subprocess.Popen')
    def test_edit_config_updates_mtime(
        self, mock_popen: MagicMock, mock_validate: MagicMock, mock_load: MagicMock, tmp_path: Path
    ) -> None:
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')
        mock_load.return_value = {'gui': {'sync_interval': 300}}
        mock_popen.return_value = MagicMock()

        app = make_app({'gui': {}}, cfgpath)

        # Patch get_xdg_config_dir so _run_edit_config uses our temp path
        with patch('korgalore.gui.get_xdg_config_dir', return_value=tmp_path):
            old_mtime = app._config_mtime
            # Bump file mtime so there is something newer to record
            _bump_mtime(cfgpath)

            app._run_edit_config()

        assert app._config_mtime > old_mtime
        # Subsequent _check_reload_config should not trigger a reload
        with patch('korgalore.gui.validate_config_file') as v:
            app._check_reload_config()
            v.assert_not_called()


def _mock_node() -> MagicMock:
    """A LoreNode stand-in that records cancel_active/shutdown calls."""
    return MagicMock(spec=liblore.LoreNode)


class TestNetworkLostCancelsSync:
    """The network-down callback cancels lore requests only when syncing."""

    @pytest.mark.parametrize(
        'names',
        [('https://lore.kernel.org', 'https://erol.kernel.org'), ()],
        ids=['two-nodes', 'no-nodes-yet'],
    )
    def test_cancels_every_node_when_syncing(self, names: tuple[str, ...]) -> None:
        """Each cached node is cancelled, not shut down; no nodes is harmless."""
        nodes = {name: _mock_node() for name in names}
        app = make_app(nodes=nodes, is_syncing=True)

        with patch.object(app, 'update_status'):
            app._on_network_changed(MagicMock(), False)

        for node in nodes.values():
            node.cancel_active.assert_called_once_with()
            # shutdown() is terminal and reserved for quit(); the node has
            # to keep working once the network comes back.
            node.shutdown.assert_not_called()
        assert app.next_sync_time == 0.0

    def test_does_not_cancel_when_idle(self) -> None:
        """With no sync in flight there is nothing to interrupt."""
        node = _mock_node()
        app = make_app(nodes={'https://lore.kernel.org': node}, is_syncing=False)

        with patch.object(app, 'update_status'):
            app._on_network_changed(MagicMock(), False)

        node.cancel_active.assert_not_called()

    def test_network_restored_does_not_cancel(self) -> None:
        """Coming back online schedules a sync rather than cancelling one."""
        node = _mock_node()
        app = make_app(nodes={'https://lore.kernel.org': node}, network_available=False, is_syncing=True)

        with patch.object(app, 'update_status'):
            app._on_network_changed(MagicMock(), True)

        node.cancel_active.assert_not_called()
        assert app.next_sync_time == pytest.approx(time.time() + 10, abs=5)

    def test_still_reports_offline_status(self) -> None:
        """Cancelling does not replace the offline status update."""
        app = make_app(nodes={'https://lore.kernel.org': _mock_node()}, is_syncing=True)

        with patch.object(app, 'update_status') as mock_status:
            app._on_network_changed(MagicMock(), False)

        mock_status.assert_called_once_with('Network unavailable', 'network-offline-symbolic')

    def test_failing_node_does_not_block_the_rest(self) -> None:
        """One uncooperative node must not strand the others mid-read."""
        bad = _mock_node()
        bad.cancel_active.side_effect = RuntimeError('boom')
        good = _mock_node()
        # dicts preserve insertion order, so the failing node comes first
        app = make_app(nodes={'bad': bad, 'good': good}, is_syncing=True)

        with patch.object(app, 'update_status'):
            app._on_network_changed(MagicMock(), False)

        good.cancel_active.assert_called_once_with()


class TestSyncRescheduleAfterCancel:
    """run_sync() must not push back a sooner sync scheduled by a callback."""

    def _run_sync_with(self, app: 'KorgaloreApp', pull_side_effect: Any) -> list[str]:
        """Run app.run_sync() with everything but the pull stubbed out.

        Returns the status texts passed to update_status().
        """
        statuses: list[str] = []
        with (
            patch('korgalore.gui.GLib'),
            patch('korgalore.gui.refresh_subfolder_templates'),
            patch('korgalore.gui.perform_pull', side_effect=pull_side_effect) as mock_pull,
            patch.object(app, '_check_reload_config'),
            patch.object(app, 'update_status', side_effect=lambda status, icon=None: statuses.append(status)),
        ):
            app.run_sync()
            assert mock_pull.called, 'run_sync() returned before pulling'
        return statuses

    def test_network_flap_keeps_the_sooner_resync(self) -> None:
        """A down/up flap mid-sync leaves the 10-second resync in place."""
        node = _mock_node()
        app = make_app(nodes={'https://lore.kernel.org': node}, sync_interval=300)

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
        app = make_app(sync_interval=300)

        self._run_sync_with(app, lambda *a, **kw: (None, []))

        assert app.next_sync_time == pytest.approx(time.time() + 300, abs=5)

    def test_cancelled_sync_without_flap_is_not_an_error(self) -> None:
        """A cancellation with no callback rescheduling falls back to the
        normal interval and leaves no error status on the indicator."""
        app = make_app(sync_interval=300)

        def _cancelled(*args: Any, **kwargs: Any) -> None:
            raise liblore.OperationCancelledError('Node is shut down')

        statuses = self._run_sync_with(app, _cancelled)

        assert app.next_sync_time == pytest.approx(time.time() + 300, abs=5)
        assert not any(s.startswith('Error:') for s in statuses)


class TestQuitStillShutsDown:
    """quit() keeps using the terminal shutdown(), not cancel_active()."""

    def test_shutdown_used_on_quit(self, tmp_path: Path) -> None:
        node = _mock_node()
        app = make_app(
            nodes={'https://lore.kernel.org': node}, stop_event=MagicMock(), cfgpath=tmp_path / 'korgalore.toml'
        )

        with patch('korgalore.gui.Gtk'):
            app.quit()

        node.shutdown.assert_called_once_with()
        node.cancel_active.assert_not_called()
