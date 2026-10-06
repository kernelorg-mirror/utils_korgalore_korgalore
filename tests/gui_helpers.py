"""Helpers for the GUI tests, which build a KorgaloreApp without GTK."""

from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional
from unittest.mock import MagicMock

from tests.digest_helpers import make_ctx

if TYPE_CHECKING:
    from korgalore.gui import KorgaloreApp


def make_app(
    config: Optional[Dict[str, Any]] = None,
    cfgpath: Optional[Path] = None,
    nodes: Optional[Dict[str, Any]] = None,
    **overrides: Any,
) -> 'KorgaloreApp':
    """Construct a KorgaloreApp without GTK by stubbing __init__.

    Only the attributes the config, sync and network paths touch are set; the
    menu items and indicator are replaced with mocks. config, cfgpath and the
    lore nodes go into ctx.obj (reach it as app.ctx.obj). When cfgpath is
    given, the config mtime is recorded as the real __init__ would. Any other
    keyword argument is set on the app last, e.g. sync_interval=60 or
    is_syncing=True.
    """
    from korgalore.gui import KorgaloreApp

    config = config if config is not None else {}
    obj: Dict[str, Any] = {
        'config': config,
        'targets': {},
        'feeds': {},
        'deliveries': {},
        'lore_nodes': nodes if nodes is not None else {},
    }
    if cfgpath is not None:
        obj['cfgpath'] = cfgpath

    app = object.__new__(KorgaloreApp)
    app.ctx = make_ctx(obj)
    app.sync_interval = config.get('gui', {}).get('sync_interval', 300)
    app.is_syncing = False
    app.network_available = True
    app.next_sync_time = 0.0
    app.auth_needed_target = None
    app.ind = MagicMock()
    app.item_status = MagicMock()
    app.item_sync = MagicMock()
    app.item_next_sync = MagicMock()
    if cfgpath is not None:
        app.cfgpath = cfgpath
        app._config_mtime = app._get_config_mtime()
    for name, value in overrides.items():
        setattr(app, name, value)
    return app
