#!/usr/bin/env bash
# Kodgar/YODAW macOS installer.
#
# Wraps scripts/bootstrap.py with macOS-specific checks:
#   1. verifies macOS + Apple Silicon/Intel detection
#   2. verifies Python 3.12 (Xcode CLT / Homebrew / python.org)
#   3. runs the zero-touch bootstrap (venv, deps, wrapper, 9Router stage)
#   4. optionally installs a LaunchAgent so `yodaw serve` starts at login
#
# Usage:
#   scripts/install_macos.sh                 # standard install
#   scripts/install_macos.sh --with-launchagent
#   scripts/install_macos.sh --install-dir /opt/yodaw --skip-9router

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${HOME}/.yodaw"
WITH_LAUNCHAGENT=0
BOOTSTRAP_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --with-launchagent)
            WITH_LAUNCHAGENT=1
            shift
            ;;
        --install-dir)
            INSTALL_DIR="$2"
            BOOTSTRAP_ARGS+=(--install-dir "$2")
            shift 2
            ;;
        *)
            BOOTSTRAP_ARGS+=("$1")
            shift
            ;;
    esac
done

echo "== Kodgar/YODAW macOS installer =="

# 1. Platform check -----------------------------------------------------
if [ "$(uname -s)" != "Darwin" ]; then
    echo "error: this installer is for macOS (detected: $(uname -s))" >&2
    exit 1
fi
ARCH="$(uname -m)"
echo "architecture: ${ARCH} ($([ "$ARCH" = "arm64" ] && echo "Apple Silicon" || echo "Intel"))"

# 2. Python 3.12 detection ----------------------------------------------
PY=""
for candidate in python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
            PY="$(command -v "$candidate")"
            break
        fi
    fi
done
if [ -z "$PY" ]; then
    cat >&2 <<'EOF'
error: Python 3.12 is required but was not found.
Install it with one of:
  xcode-select --install        # (may only provide a newer python3)
  brew install python@3.12
or grab the installer from https://www.python.org/downloads/
EOF
    exit 1
fi
echo "python: $PY ($("$PY" --version))"

# 3. Bootstrap ----------------------------------------------------------
echo "bootstrapping into ${INSTALL_DIR} ..."
"$PY" "$SCRIPT_DIR/bootstrap.py" \
    --install-dir "$INSTALL_DIR" \
    "${BOOTSTRAP_ARGS[@]+"${BOOTSTRAP_ARGS[@]}"}"

echo "install:   ${INSTALL_DIR}"
echo "launcher:  ${INSTALL_DIR}/bin/yodaw"

# 4. Optional LaunchAgent ----------------------------------------------
if [ "$WITH_LAUNCHAGENT" -eq 1 ]; then
    PLIST_DIR="${HOME}/Library/LaunchAgents"
    PLIST="${PLIST_DIR}/com.kodgar.yodaw.plist"
    mkdir -p "$PLIST_DIR"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key><string>com.kodgar.yodaw</string>
    <key>ProgramArguments</key>
    <array>
      <string>${INSTALL_DIR}/bin/yodaw</string>
      <string>serve</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><false/>
    <key>StandardOutPath</key><string>${INSTALL_DIR}/var/log/launchagent.log</string>
    <key>StandardErrorPath</key><string>${INSTALL_DIR}/var/log/launchagent.log</string>
  </dict>
</plist>
EOF
    echo "launchagent: installed at ${PLIST} (starts yodaw serve at login)"
    echo "load it now with: launchctl load \"${PLIST}\""
fi

echo "done. try: ${INSTALL_DIR}/bin/yodaw kodgar-doctor"
