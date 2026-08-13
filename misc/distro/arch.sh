#!/usr/bin/env bash

# Arch lane: bleeding edge, the mirror image of the Debian lane. It ships the
# newest of everything -- click 8.3.x, pytest 9.x -- so it catches breakage
# from dependencies moving ahead of us rather than lagging behind, which is
# the failure mode no other lane in this matrix can see.
#
# Package names drop the "3" here: python-click, not python3-click.

set -eu

# The base image ships no local signing key, so archlinux-keyring's
# post-upgrade hook prints "error: command failed to execute correctly" and
# pacman still exits 0. Nothing is actually broken -- package signatures are
# verified against the distro keyring either way -- but an unexplained
# "error:" in a CI log is worth one line to remove.
pacman-key --init >/dev/null 2>&1

pacman -Syu --noconfirm --needed \
    git python python-pip \
    python-click python-click-log python-requests \
    python-google-auth python-google-auth-oauthlib python-google-auth-httplib2 \
    python-google-api-python-client \
    python-gobject gtk3 libappindicator-gtk3 \
    python-pytest \
    >/dev/null

# shellcheck disable=SC1091
. /src/misc/distro/_run.sh
