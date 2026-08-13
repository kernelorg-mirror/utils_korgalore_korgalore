#!/usr/bin/env sh

set -eu

# Run an import smoke check and pytest under every interpreter version in the
# supported range (see `requires-python` in pyproject.toml). This is not part
# of ci.sh because materialising a venv per version is slow; invoke this
# before releases and after changes that could depend on version-specific
# runtime behaviour (import-time annotations, stdlib APIs, etc.).
#
# The linters and type checkers are deliberately not repeated here. ci.sh runs
# them once, and they are configured against a fixed target version
# (target-version/python_version in pyproject.toml), so re-running them per
# interpreter would report the same thing four times.
#
# Missing interpreters are pulled automatically from uv's managed Python
# cache (python-build-standalone), so no system packages or sudo are needed.
#
# Override the version list: PYTHONS="3.11 3.14" ./ci-matrix.sh
#
# After the interpreter sweep it runs one "floor" lane: the project resolved
# to the *minimum* versions our metadata allows (uv --resolution
# lowest-direct), then the suite. This is what catches a dependency floor that
# is declared but does not actually work -- our `liblore>=0.9.0` and
# `click>=8.3.0` bounds are claims about what we support, and every other lane
# resolves the newest compatible release instead, so nothing else tests them.
# It runs on the lowest supported interpreter, where the old dependency
# releases are likeliest to still publish wheels. Override or skip it:
# FLOOR_PY=3.12 ./ci-matrix.sh  (or FLOOR_PY= ./ci-matrix.sh to skip)

# ${VAR-default} rather than ${VAR:-default}: the latter also substitutes for
# an explicitly empty value, which would make "PYTHONS= ./ci-matrix.sh" run
# the full sweep instead of skipping it.
PYTHONS="${PYTHONS-3.11 3.12 3.13 3.14}"

# Install any requested interpreters that are missing. This is an explicit
# step because a dev may have set UV_PYTHON_DOWNLOADS=manual globally to
# prevent `uv sync` from reaching out to the network mid-workflow; running
# it once up front scopes the network access to this script.
if [ -n "$PYTHONS" ]; then
    # shellcheck disable=SC2086
    uv python install $PYTHONS
fi

# Collect failures so the run reports a complete matrix instead of bailing on
# the first broken interpreter.
failed=""

for py in $PYTHONS; do
    printf '\n=== Python %s ===\n' "$py"
    # Each version gets its own project environment so switching interpreters
    # does not thrash the default .venv and each sync is incremental.
    UV_PROJECT_ENVIRONMENT=".venv-$py"
    export UV_PROJECT_ENVIRONMENT
    # No --all-extras: the gui extra needs PyGObject, which builds against
    # cairo and girepository headers. See the same note in ci.sh.
    if ! uv sync --all-groups --python "$py"; then
        failed="$failed $py(sync)"
        continue
    fi
    if ! uv run python -c 'import korgalore.cli, sys; print("import korgalore.cli OK on", sys.version.split()[0])'; then
        failed="$failed $py(import)"
        continue
    fi
    # The CLI is the entry point users actually hit, so make sure Click can
    # build the command tree under this interpreter, not just that the
    # package imports.
    if ! uv run kgl --version; then
        failed="$failed $py(cli)"
        continue
    fi
    if ! uv run pytest --durations=20; then
        failed="$failed $py(pytest)"
        continue
    fi
done

# Floor lane: install the project at the minimum versions our metadata allows,
# then run the suite. Dependency groups (pytest, mypy, ...) carry no lower
# bounds, so `uv sync --resolution lowest-direct` would drag them to
# unbuildable ancient releases; installing just the project with
# `uv pip install` keeps the flooring to korgalore's own runtime dependencies,
# and a current test runner is layered on afterwards.
FLOOR_PY="${FLOOR_PY-3.11}"
if [ -n "$FLOOR_PY" ]; then
    printf '\n=== Floors (lowest-direct) on Python %s ===\n' "$FLOOR_PY"
    # --python targets the venv explicitly, so the loop's UV_PROJECT_ENVIRONMENT
    # must not leak in and redirect these commands.
    unset UV_PROJECT_ENVIRONMENT
    uv python install "$FLOOR_PY"
    floorenv='.venv-floor'
    rm -rf "$floorenv"
    if ! uv venv "$floorenv" --python "$FLOOR_PY"; then
        failed="$failed floors(venv)"
    elif ! uv pip install --python "$floorenv" --resolution lowest-direct .; then
        failed="$failed floors(install)"
    elif ! uv pip install --python "$floorenv" pytest; then
        failed="$failed floors(pytest-install)"
    elif ! "$floorenv/bin/python" -c 'import korgalore.cli, sys; print("import korgalore.cli OK on", sys.version.split()[0])'; then
        failed="$failed floors(import)"
    elif ! "$floorenv/bin/kgl" --version; then
        failed="$failed floors(cli)"
    elif ! "$floorenv/bin/python" -m pytest --durations=20; then
        failed="$failed floors(pytest)"
    fi

    # Report what the floors actually resolved to. A green lane says the
    # declared minimums install and pass; this says which versions that was,
    # so a bound that has drifted above what PyPI still offers is visible.
    printf '\n--- resolved floors ---\n'
    uv pip list --python "$floorenv" 2>/dev/null | grep -Ei \
        'click|google|liblore|requests' || true
fi

if [ -n "$failed" ]; then
    printf '\nFAILURES:%s\n' "$failed"
    exit 1
fi

if [ -n "$PYTHONS" ]; then
    printf '\nAll interpreters passed: %s\n' "$PYTHONS"
fi
if [ -n "$FLOOR_PY" ]; then
    printf 'Floor lane passed on Python %s\n' "$FLOOR_PY"
fi
