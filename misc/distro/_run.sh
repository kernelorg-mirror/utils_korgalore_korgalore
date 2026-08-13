#!/usr/bin/env bash

# Shared in-container runner, sourced by each distro/<lane>.sh after it has
# installed that distro's own packages. It builds a venv that can see those
# distro site-packages, layers liblore and korgalore on top with --no-deps
# (so pip never quietly upgrades a distro-provided dependency out from under
# the test), reports where every dependency came from, and runs the suite.
#
# A lane may set PIP_FILL to a space-separated list of requirements the distro
# does not package at all. Unlike the --no-deps installs below, these go in
# with their dependencies resolved normally -- but because the venv can see
# the distro's site-packages, pip treats already-satisfied requirements as
# installed and leaves the distro copies in place. AlmaLinux needs this for
# google-api-python-client, which EL10 does not ship anywhere.
#
# Unlike b4, korgalore has no optional third-party imports to degrade over:
# every runtime dependency is imported unconditionally, so a lane that cannot
# supply one has to fill it rather than skip it. The single genuinely optional
# piece is GTK, which gtk_compat.py gates behind HAS_GTK; the report below
# prints that flag so a lane installing the GObject stack can confirm it
# actually resolved, rather than silently falling back to the headless path.

set -eu

# The bind mount is read-only; copy to a writable tree so pytest and the
# editable install can write.
cp -r /src /build
cd /build

# The checkout carries liblore as a git submodule, for developers running
# straight from a source tree. Leaving it in place would let /build/liblore
# shadow the release installed below, which is the opposite of what this lane
# is meant to measure.
rm -rf /build/liblore

python3 -m venv --system-site-packages /venv
# shellcheck disable=SC1091
. /venv/bin/activate

# Requirements the distro does not package, resolved normally. Unquoted on
# purpose: this is a word-split list, and it is empty for most lanes.
if [ -n "${PIP_FILL:-}" ]; then
    # shellcheck disable=SC2086
    pip install -q $PIP_FILL
fi

# liblore is first-party and no distro packages it. --no-deps so it cannot
# drag in newer copies of click or requests and defeat the point of the lane.
pip install -q --no-deps liblore
# korgalore itself, editable and without deps -- everything is present now.
pip install -q --no-deps -e .

echo '=== dependency provenance (distro vs pip) ==='
python - <<'PY'
import importlib

# Import names, not package names: googleapiclient comes from
# google-api-python-client, and google.auth from google-auth.
for name in ('click', 'click_log', 'requests', 'google.auth',
             'google_auth_oauthlib', 'google_auth_httplib2', 'googleapiclient',
             'liblore', 'gi', 'pytest'):
    try:
        mod = importlib.import_module(name)
    except Exception as exc:  # report, don't fail
        print(f'  {name:22} absent  [{exc.__class__.__name__}]')
        continue
    ver = getattr(mod, '__version__', '?')
    path = getattr(mod, '__file__', '') or ''
    if path.startswith('/usr/'):
        origin = 'distro'
    elif '/venv/' in path:
        origin = 'pip'
    else:
        origin = '?'
    print(f'  {name:22} {str(ver):14} [{origin}]')

# The taskbar GUI is the one optional feature. HAS_GTK covers Gtk, GLib, Gio
# and an AppIndicator3 namespace under either of its two spellings, so this
# single flag says whether a lane's GObject packages were complete.
from korgalore.gtk_compat import HAS_GTK
print(f'  {"HAS_GTK":22} {HAS_GTK}')
PY

# The CLI is what users actually invoke, and it imports every target module,
# so it fails on a missing dependency that a bare `import korgalore` misses.
echo '=== kgl --version ==='
kgl --version

echo '=== pytest ==='
python -m pytest tests -q -p no:cacheprovider
