#!/bin/bash
# YODAW Stage 8.13 live-provider smoke (detached run).
# One mission through the real Stage 8 runtime + real Ollama.
cd /Users/arminshokri/YODAW/yodaw-code-core || exit 1

export YODAW_DB_PATH=/tmp/yodaw_stage8_smoke.db
export YODAW_PORT=8845
export YODAW_LLM_STYLE=ollama
export YODAW_LLM_BASE_URL=http://127.0.0.1:11434
export YODAW_LLM_MODEL=qwen2.5-coder:7b
export YODAW_LLM_TIMEOUT_SECONDS=3600
export YODAW_LLM_KEEP_ALIVE=60m
export YODAW_PROVIDER_MAX_RETRIES=2
export YODAW_PROVIDER_BACKOFF_SECONDS=2
export YODAW_ENABLE_GITHUB=false
export YODAW_LOG_LEVEL=WARNING

rm -f "$YODAW_DB_PATH" "$YODAW_DB_PATH"-wal "$YODAW_DB_PATH"-shm

.venv/bin/python -m app.runtime \
  > /tmp/yodaw_stage8_runtime.log 2>&1 &
RUNTIME_PID=$!

for i in $(seq 1 30); do
  curl -s --max-time 2 http://127.0.0.1:8845/api/v1/health > /dev/null && break
  sleep 1
done

# Measure POST latency: must return QUEUED immediately.
START=$(date +%s)

curl -s --max-time 10 -X POST http://127.0.0.1:8845/api/v1/missions \
  -H "Content-Type: application/json" \
  -d '{"goal":"Refactor greet(name) to use an f-string without changing behavior.","capability":"repo-code","metadata":{"repo_path":"/tmp/yodaw_live_repo","commit_message":"stage8 live: use f-string greeting"}}' \
  > /tmp/yodaw_stage8_post.json

POST_LATENCY=$(( $(date +%s) - START ))

# Poll until terminal, measuring API responsiveness meanwhile.
POLL_START=$(date +%s)
TERMINAL=""
FINAL_JSON=""

while [ $(( $(date +%s) - POLL_START )) -lt 4000 ]; do
  BODY=$(curl -s --max-time 5 "http://127.0.0.1:8845/api/v1/missions" )
  ST=$(printf '%s' "$BODY" | .venv/bin/python -c "
import sys, json
try:
    missions = json.load(sys.stdin)
    m = missions[0] if missions else {}
    print(m.get('status',''), m.get('id',''))
except Exception:
    print('ERR', '')
")
  STATUS=$(echo "$ST" | cut -d' ' -f1)
  MID=$(echo "$ST" | cut -d' ' -f2)

  case "$STATUS" in
    PASS|FAIL|BLOCKED|CANCELLED)
      TERMINAL="$STATUS"
      curl -s --max-time 10 "http://127.0.0.1:8845/api/v1/missions/$MID" > /tmp/yodaw_stage8_mission.json
      break
      ;;
  esac

  sleep 15
done

END=$(date +%s)

curl -s --max-time 10 http://127.0.0.1:8845/api/v1/learning > /tmp/yodaw_stage8_learning.json 2>/dev/null

kill "$RUNTIME_PID" 2>/dev/null
wait "$RUNTIME_PID" 2>/dev/null

{
  echo "POST_LATENCY_SECONDS=$POST_LATENCY"
  echo "TERMINAL_STATUS=$TERMINAL"
  echo "MISSION_ID=$MID"
  echo "TOTAL_DURATION_SECONDS=$((END - START))"
  echo "DONE"
} > /tmp/yodaw_stage8_result.txt
