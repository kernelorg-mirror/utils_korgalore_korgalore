#!/usr/bin/env bash

# Debian stable lane: the old-dependency guardian. Trixie ships click 8.1.8
# and click-log 0.3.2, both below korgalore's declared `click>=8.3.0` and
# `click-log>=0.4.0` floors. Because _run.sh installs korgalore with
# --no-deps, pip leaves those older copies alone and the suite actually runs
# against them -- so this lane answers whether those floors describe a real
# requirement or merely the version that happened to be current when they
# were written.
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

# Trixie's click 8.1.8 is genuinely too old: cli.py calls
# click.progressbar(hidden=...), which did not exist before 8.3.0, so four
# tests fail outright against the distro copy. Filling click from PyPI keeps
# the rest of Debian's stack under test rather than losing the whole lane to
# one dependency. click-log is deliberately NOT filled -- trixie's 0.3.2 sits
# below our declared >=0.4.0, and leaving it in place is what tests whether
# that floor describes a real requirement.
PIP_FILL='click>=8.3.0'
export PIP_FILL

# shellcheck disable=SC1091
. /src/misc/distro/_run.sh
