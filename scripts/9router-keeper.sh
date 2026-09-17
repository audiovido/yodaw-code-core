#!/usr/bin/env bash
# Kodgar 9Router keeper (long-lived supervisor).
#
# Runs FOREVER under launchd (KeepAlive=true), waking every 30s to
# guarantee exactly one healthy local daemon:
#
#   1. port listening           -> healthy, sleep
#   2. launcher booting (<150s) -> give it time, sleep
#   3. launcher wedged (old)    -> kill it, spawn fresh
#   4. no launcher at all       -> spawn fresh
#
# Spawned daemons mirror the production launcher env
# (app/llm/ninerouter.py start_daemon): PORT, TRAY_MODE=1, DATA_DIR,
# cwd=data_dir. The keeper process must stay alive (it does - it is
# the launchd job) because children of an exiting launchd job are
# reaped by launchd.
set -u
export PATH="/usr/local/bin:/opt/homebrew/bin:/Users/arminshokri/.npm-global/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PORT="20128"
NR="/Users/arminshokri/.npm-global/bin/9router"
DATA_DIR="$HOME/.9router"
LOG="$HOME/.kodgar/logs/9router.out.log"
ERR="$HOME/.kodgar/logs/9router.err.log"
LOCK="$HOME/.kodgar/runtime/9router-start.lock"
SPAWN_MARKER="$HOME/.kodgar/runtime/last-spawn.epoch"
BOOT_GRACE_SECONDS=150

while true; do
  now=$(date +%s)

  if lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    sleep 30
    continue
  fi

  if ! mkdir "$LOCK" 2>/dev/null; then
    sleep 5
    continue
  fi

  # Re-check under the lock.
  if lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    rmdir "$LOCK" >/dev/null 2>&1 || true
    sleep 30
    continue
  fi

  launcher_pids=$(pgrep -f "npm-global/bin/9router" 2>/dev/null || true)
  if [ -n "$launcher_pids" ]; then
    last_spawn=0
    [ -f "$SPAWN_MARKER" ] && last_spawn=$(cat "$SPAWN_MARKER" 2>/dev/null || echo 0)
    age=$(( now - last_spawn ))
    if [ "$age" -lt "$BOOT_GRACE_SECONDS" ]; then
      # A launcher exists and was started recently: it is booting
      # (binding can take a couple of minutes on a cold catalog).
      rmdir "$LOCK" >/dev/null 2>&1 || true
      sleep 10
      continue
    fi
    # Launcher exists but is old and never bound the port: wedged.
    for pid in $launcher_pids; do
      kill -9 "$pid" 2>/dev/null || true
    done
    sleep 1
  fi

  echo "$now" > "$SPAWN_MARKER" 2>/dev/null || true
  cd "$DATA_DIR" 2>/dev/null || true
  DATA_DIR="$DATA_DIR" PORT="$PORT" TRAY_MODE=1 nohup "$NR" \
    --port "$PORT" --host 127.0.0.1 --no-browser --skip-update \
    >>"$LOG" 2>>"$ERR" </dev/null &
  cd / 2>/dev/null || true

  rmdir "$LOCK" >/dev/null 2>&1 || true
  sleep 30
done
