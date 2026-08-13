#!/usr/bin/env bash

# Fedora 44 lane: the complete case. Fedora packages every one of korgalore's
# runtime dependencies, including click-log, which most distros skip, so
# nothing has to be filled in from PyPI here.
#
# It also carries the full GTK 3 stack under the original AppIndicator3
# namespace spelling, which makes this the lane that exercises the first
# branch of gtk_compat's namespace search. See debian-stable.sh for the
# Ayatana spelling.
#
# gobject-introspection is listed explicitly: python3-gobject does not pull it
# in, and without it Gtk-3.0.typelib cannot resolve its xlib-2.0 dependency.
# The import then fails and HAS_GTK comes out False, which would leave this
# lane green while testing none of the GUI path.

set -eu

dnf -q -y install \
    git python3 python3-pip \
    python3-click python3-click-log python3-requests \
    python3-google-auth python3-google-auth-oauthlib python3-google-auth-httplib2 \
    python3-google-api-client \
    python3-gobject gtk3 libappindicator-gtk3 gobject-introspection \
    python3-pytest \
    >/dev/null

# shellcheck disable=SC1091
. /src/misc/distro/_run.sh
