"""Optional GTK 3 / AppIndicator imports for the taskbar GUI.

PyGObject builds the ``gi.repository`` namespaces at runtime from typelib
files, so no static type information exists for them and no stub package
covers them. Importing the names directly and rebinding them to ``None`` on
failure -- which is what ``gui.py`` used to do inline -- left every
``Gtk.*`` use looking to a type checker like an attribute access on
``None``, which accounted for over a hundred of the diagnostics ci.sh
reports.

Declaring the namespaces as ``Any`` up front and populating them through
importlib gives the checkers one consistent type for each name, so the GUI
code reads naturally without a type: ignore on every line.

Callers must still gate on :data:`HAS_GTK` before touching any of these
names: they are ``None`` when GTK is unavailable, exactly as before.
"""

import importlib
import logging
from typing import Any

logger = logging.getLogger('korgalore.gui')

# The GUI needs all four namespaces, so they are populated together or not at
# all. Annotated Any rather than left bare so the checkers do not infer None.
Gtk: Any = None
GLib: Any = None
Gio: Any = None
AppIndicator3: Any = None

# AppIndicator3 is called AyatanaAppIndicator3 on some systems (e.g. Debian).
_INDICATOR_NAMESPACES = (('AppIndicator3', '0.1'), ('AyatanaAppIndicator3', '0.1'))


def _load() -> tuple[Any, Any, Any, Any] | None:
    """Import the GTK namespaces, or return None if any of them is missing.

    require_version() raises ValueError for a namespace that is absent or
    only available at another version, and import_module() raises ImportError
    when PyGObject itself is not installed.
    """
    try:
        gi = importlib.import_module('gi')
        gi.require_version('Gtk', '3.0')
        gtk = importlib.import_module('gi.repository.Gtk')
        glib = importlib.import_module('gi.repository.GLib')
        gio = importlib.import_module('gi.repository.Gio')
    except (ImportError, ValueError) as e:
        logger.debug('GTK unavailable, taskbar GUI disabled: %s', e)
        return None

    for name, version in _INDICATOR_NAMESPACES:
        try:
            gi.require_version(name, version)
            indicator = importlib.import_module(f'gi.repository.{name}')
        except (ImportError, ValueError):
            continue
        return gtk, glib, gio, indicator

    logger.debug('No AppIndicator3 namespace found, taskbar GUI disabled')
    return None


_loaded = _load()
#: True when every namespace above was imported successfully.
HAS_GTK = _loaded is not None
if _loaded is not None:
    Gtk, GLib, Gio, AppIndicator3 = _loaded
