#!/usr/bin/env bash

# Debian stable lane: the old-dependency guardian, and the lane that sets our
# dependency floors. Trixie ships click 8.1.8 and click-log 0.3.2; korgalore
# declares `click>=8.1.7` and `click-log>=0.3.2` so that it installs here with
# nothing pulled from PyPI. Because _run.sh installs korgalore with --no-deps,
# pip leaves the distro copies alone and the suite genuinely runs against
# them, which is what keeps those floors honest.
#
# googleapiclient is packaged as python3-googleapi here, not
# python3-google-api-client as on Fedora.
#
# This is also the AppIndicator3 spelling that gtk_compat falls through to:
# Debian ships the namespace as AyatanaAppIndicator3, which is the reason
# that fallback loop exists at all.

set -eu

export DEBIAN_FRONTEND=noninteractive
apt-get -qq update >/dev/null
apt-get -qq install -y --no-install-recommends \
    git python3 python3-pip python3-venv \
    python3-click python3-click-log python3-requests \
    python3-google-auth python3-google-auth-oauthlib python3-google-auth-httplib2 \
    python3-googleapi \
    python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 \
    python3-pytest \
    >/dev/null

# Nothing to fill: apart from liblore, which no distro packages and which
# _run.sh installs from PyPI in every lane, trixie supplies korgalore's whole
# runtime stack. Keep it that way -- a PIP_FILL line appearing here means a
# third-party dependency has outgrown Debian stable again.

# shellcheck disable=SC1091
. /src/misc/distro/_run.sh
