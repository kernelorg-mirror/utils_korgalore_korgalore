#!/usr/bin/env sh

set -eu

# Run the test suite inside real distro containers, installing korgalore's
# third-party dependencies from each distro's *own* package manager. This
# catches "the distro ships a different dependency than we declare" bugs that
# ci-matrix.sh cannot see: every uv-resolved lane there gets the newest
# compatible release, and even its floor lane only tests the bounds we wrote
# down. A distro ships whatever it froze, which may sit below our declared
# minimum -- Debian trixie's click 8.1.8 against our `click>=8.3.0` is exactly
# that case.
#
# This is a heavy, network-dependent, pre-release check -- it pulls OS images
# and hits distro mirrors -- so it is deliberately separate from both ci.sh
# (fast local gate) and ci-matrix.sh (interpreter sweep).
#
# Requires rootless podman. Override the lane list or the runtime:
#   DISTROS="debian-stable arch" ./distro-matrix.sh
#   PODMAN=docker ./distro-matrix.sh
#
# Each lane runs misc/distro/<lane>.sh inside its container; that recipe
# installs the distro packages, then hands off to the shared
# misc/distro/_run.sh, which builds a venv over the distro site-packages,
# adds liblore and korgalore with --no-deps, prints a provenance report, and
# runs pytest.
#
# IMPORTANT: a green matrix does not mean every lane tested an old
# dependency. Read the per-lane "dependency provenance" report to see which
# libraries came from the distro and which pip had to supply. AlmaLinux, for
# instance, packages no google-api-python-client at all and pulls it from
# PyPI, so that lane says nothing about EL's copy of it -- there isn't one.

PODMAN="${PODMAN:-podman}"

# ${VAR-default} rather than ${VAR:-default}, so "DISTROS= ./distro-matrix.sh"
# is an explicit no-op instead of silently running everything.
DISTROS="${DISTROS-fedora44 alma10 debian-stable arch}"

if ! command -v "$PODMAN" >/dev/null 2>&1; then
    printf 'error: %s not found; install podman (rootless) or set PODMAN=\n' "$PODMAN" >&2
    exit 2
fi

# Base image per lane. Pinned to the major versions we care about; bump
# deliberately, since a newer image may ship newer dependencies and quietly
# change what the lane covers.
image_for() {
    case "$1" in
        fedora44)      echo 'registry.fedoraproject.org/fedora:44' ;;
        alma10)        echo 'quay.io/almalinuxorg/almalinux:10' ;;
        debian-stable) echo 'docker.io/debian:stable-slim' ;;
        arch)          echo 'docker.io/archlinux:latest' ;;
        *) printf 'error: unknown distro lane: %s\n' "$1" >&2; return 1 ;;
    esac
}

# The source tree is bind-mounted read-only; --security-opt label=disable
# avoids relabelling the host checkout under SELinux (rootless podman would
# otherwise fail to read it, or rewrite its labels with :z/:Z).
run_lane() {
    _d="$1"
    _img=$(image_for "$_d") || return 1
    printf '\n============================ %s (%s) ============================\n' \
        "$_d" "$_img"
    "$PODMAN" run --rm --security-opt label=disable \
        -v "$PWD:/src:ro" "$_img" bash "/src/misc/distro/$_d.sh"
}

# Collect failures so the run reports the whole matrix instead of bailing on
# the first red lane.
failed=""
for d in $DISTROS; do
    if ! run_lane "$d"; then
        failed="$failed $d"
    fi
done

if [ -n "$failed" ]; then
    printf '\nFAILURES:%s\n' "$failed"
    exit 1
fi

if [ -n "$DISTROS" ]; then
    printf '\nAll distro lanes passed: %s\n' "$DISTROS"
fi
