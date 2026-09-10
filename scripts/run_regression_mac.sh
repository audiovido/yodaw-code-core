#!/bin/bash
# YODAW Mac regression runner.
#
# Safe wrapper around the full pytest suite for macOS, where two
# environment problems can produce false failures:
#   1. `python` may not exist (only `python3`), while some tests
#      and subprocesses invoke `python`.
#   2. A low open-file limit (e.g. `ulimit -n 256`) exhausts
#      file descriptors: "Too many open files" /
#      "sqlite3.OperationalError: unable to open database file".
#
# What this script does:
#   - requires Python 3.12 on PATH, fails clearly otherwise
#   - creates a temporary dir with a `python` shim -> python3.12
#     (removed on exit; PATH-only change, no system changes)
#   - raises the open-file limit to 4096 when permitted
#   - sets PYTHONDONTWRITEBYTECODE=1 (no .pyc writes, no repo mutation)
#   - runs pytest with any extra args passed through ($@)
#   - prints the exact environment used
#   - preserves the pytest exit code (never hardcodes counts)
#
# Usage:
#   ./scripts/run_regression_mac.sh [pytest args...]
#   ./scripts/run_regression_mac.sh -x -q
#
# No sudo. No permanent shell/system changes. No repo mutation.

set -u

fail() {
  echo "run_regression_mac: ERROR: $1" >&2
  exit "${2:-2}"
}

# --- 1. Require Python 3.12 ---------------------------------------------
if ! command -v python3.12 >/dev/null 2>&1; then
  fail "python3.12 not found on PATH. Install Python 3.12 and retry."
fi
PY312="$(command -v python3.12)"
PY312_VERSION="$("$PY312" --version 2>&1)" || fail "could not run $PY312 --version"

# --- 2. Temporary `python` shim (PATH-only, cleaned up on exit) ---------
SHIM_DIR="$(mktemp -d "${TMPDIR:-/tmp}/yodaw-py-shim.XXXXXX")" \
  || fail "could not create temporary shim directory."
cleanup() {
  rm -rf "$SHIM_DIR"
}
trap cleanup EXIT INT TERM
ln -s "$PY312" "$SHIM_DIR/python" \
  || fail "could not create python shim in $SHIM_DIR."

export PATH="$SHIM_DIR:$PATH"
SHIM_TARGET="$(command -v python)" || fail "'python' still not resolvable after shim install."
SHIM_VERSION="$(python --version 2>&1)" || fail "shimmed 'python --version' failed."

# --- 3. Open-file limit --------------------------------------------------
NOFILES_BEFORE="$(ulimit -n 2>/dev/null || echo unknown)"
if ulimit -n 4096 2>/dev/null; then
  NOFILES_AFTER="$(ulimit -n)"
  NOFILES_NOTE="raised to 4096"
else
  NOFILES_AFTER="$NOFILES_BEFORE"
  NOFILES_NOTE="raise to 4096 NOT permitted; continuing with $NOFILES_BEFORE (low limits can cause false 'Too many open files' failures)"
fi

# --- 4. Read-only test posture -------------------------------------------
export PYTHONDONTWRITEBYTECODE=1

# --- 5. Report exact environment ------------------------------------------
echo "=== YODAW Mac regression environment ==="
echo "python3.12: $PY312 ($PY312_VERSION)"
echo "python shim: $SHIM_DIR/python -> $PY312"
echo "python resolves to: $SHIM_TARGET ($SHIM_VERSION)"
echo "open-file limit before: $NOFILES_BEFORE"
echo "open-file limit now: $NOFILES_AFTER ($NOFILES_NOTE)"
echo "PYTHONDONTWRITEBYTECODE=$PYTHONDONTWRITEBYTECODE"
echo "working directory: $PWD"
if [ "$#" -gt 0 ]; then
  echo "pytest args: $*"
else
  echo "pytest args: <none> (full suite per pytest.ini)"
fi
echo "========================================="

# --- 6. Run pytest; its exit code is authoritative ------------------------
"$PY312" -m pytest "$@"
PYTEST_EXIT=$?

echo "========================================="
echo "pytest exit code: $PYTEST_EXIT"
echo "NOTE: pytest exit code is authoritative; do not judge by test counts."
echo "NOTE: if failures mention 'Too many open files' or sqlite 'unable to open database file', retry with a clean environment (ulimit -n 4096) before treating them as code regressions."

exit "$PYTEST_EXIT"
