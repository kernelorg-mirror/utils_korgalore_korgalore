#!/usr/bin/env bash

# AlmaLinux 10 lane: the enterprise-Linux case, and the one lane that cannot
# be satisfied from distro packages alone. EL10 does not package
# google-api-python-client anywhere -- not base, AppStream, EPEL or CRB -- and
# korgalore imports googleapiclient unconditionally from gmail_target, so it
# is filled in from PyPI via PIP_FILL. Everything else comes from the distro,
# including click 8.1.7, which is exactly our declared floor -- EL10 and
# Debian trixie together are what that floor is pinned to.
#
# It also ships pytest 7.4.3, the oldest runner in the matrix, so this lane
# doubles as the check that the suite does not depend on newer pytest
# behaviour.
#
# EPEL and CRB are enabled because several of the google-auth packages live
# there rather than in AppStream.

set -eu

dnf -q -y install \
    "https://dl.fedoraproject.org/pub/epel/epel-release-latest-10.noarch.rpm" \
    >/dev/null 2>&1 || true
dnf -q -y install dnf-plugins-core >/dev/null 2>&1 || true
dnf config-manager --set-enabled crb >/dev/null 2>&1 || true

dnf -q -y install \
    git python3 python3-pip \
    python3-click python3-click-log python3-requests \
    python3-google-auth python3-google-auth-oauthlib python3-google-auth-httplib2 \
    python3-gobject gtk3 libappindicator-gtk3 gobject-introspection \
    python3-pytest \
    >/dev/null

# google-api-python-client is not packaged for EL10 at all; see the header.
# It resolves its own dependencies normally, but the venv sees the distro
# site-packages, so pip keeps the distro google-auth rather than pulling a
# newer one.
PIP_FILL='google-api-python-client'
export PIP_FILL

# shellcheck disable=SC1091
. /src/misc/distro/_run.sh
