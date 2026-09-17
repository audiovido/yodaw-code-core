#!/usr/bin/env bash
# Kodgar macOS repair: bring an existing machine back to a healthy,
# certified state without reinstalling anything.
#
# Idempotent steps:
#   1. ensure persistent Ollama LaunchAgent (local model server)
#   2. ensure persistent 9Router keeper LaunchAgent (auto-recovery)
#   3. wait until the 9Router daemon answers /api/health
#   4. register the local Ollama server with 9Router (idempotent)
#   5. certify a healthy route and persist primary + fallback chain
#   6. print a final doctor verdict
#
# Usage: bash scripts/repair_current_mac.sh

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCH_AGENTS_DIR"

echo "== Kodgar macOS repair =="

# --- 1. Ollama LaunchAgent (persistent local model server) ----------
if command -v ollama >/dev/null 2>&1; then
  OLLAMA_PATH="$(command -v ollama)"
  PLIST="$LAUNCH_AGENTS_DIR/com.kodgar.ollama.plist"
  if [ ! -f "$PLIST" ]; then
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key><string>com.kodgar.ollama</string>
    <key>ProgramArguments</key>
    <array>
      <string>${OLLAMA_PATH}</string>
      <string>serve</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/ollama_launchd.log</string>
    <key>StandardErrorPath</key><string>/tmp/ollama_launchd.log</string>
  </dict>
</plist>
EOF
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || \
      launchctl load "$PLIST" 2>/dev/null || true
    echo "ollama launchagent: installed"
  else
    echo "ollama launchagent: already installed"
  fi
else
  echo "warning: ollama not found; skipping local model server setup" >&2
fi

# --- 2. 9Router keeper LaunchAgent (automatic recovery) -------------
KEEPER_SRC="$REPO_DIR/scripts/9router-keeper.sh"
KEEPER_DST="$HOME/.kodgar/runtime/9router-keeper.sh"
KEEPER_PLIST="$LAUNCH_AGENTS_DIR/com.kodgar.9router-keeper.plist"
mkdir -p "$HOME/.kodgar/runtime" "$HOME/.kodgar/logs"
if [ -f "$KEEPER_SRC" ]; then
  mkdir -p "$(dirname "$KEEPER_DST")"
  cp "$KEEPER_SRC" "$KEEPER_DST"
  chmod +x "$KEEPER_DST"
fi
if [ -f "$KEEPER_DST" ] && [ ! -f "$KEEPER_PLIST" ]; then
  cat > "$KEEPER_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.kodgar.9router-keeper</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${KEEPER_DST}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${HOME}/.kodgar/logs/keeper.out.log</string>
  <key>StandardErrorPath</key><string>${HOME}/.kodgar/logs/keeper.err.log</string>
</dict>
</plist>
EOF
  launchctl bootstrap "gui/$(id -u)" "$KEEPER_PLIST" 2>/dev/null || \
    launchctl load "$KEEPER_PLIST" 2>/dev/null || true
  echo "9router keeper launchagent: installed"
else
  echo "9router keeper launchagent: already installed or keeper script missing"
fi

# --- 3. wait for the 9Router daemon ---------------------------------
echo "waiting for the 9Router daemon on 127.0.0.1:20128 ..."
DEADLINE=$((SECONDS + 180))
until curl -s -m 3 -o /dev/null http://127.0.0.1:20128/api/health; do
  if [ "$SECONDS" -ge "$DEADLINE" ]; then
    echo "error: 9Router daemon did not become healthy within 180s" >&2
    exit 1
  fi
  sleep 5
done
echo "daemon: healthy"

# --- 4+5. register local server + certify + persist -----------------
"$PYTHON" -m app.cli.main setup-9router \
  --register-local "Local Qwen:qwen:http://127.0.0.1:11434/v1" \
  --no-verify || echo "warning: local server registration reported an issue" >&2

"$PYTHON" -m app.cli.main kodgar || {
  echo "error: kodgar could not certify any route" >&2
  exit 1
}

# --- 6. verdict ------------------------------------------------------
"$PYTHON" -m app.cli.main kodgar-doctor || true
echo "repair finished."
