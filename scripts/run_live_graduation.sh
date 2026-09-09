#!/bin/bash
# YODAW Stage 7.7 live-model graduation mission (detached run).
cd /Users/arminshokri/YODAW/yodaw-code-core || exit 1

export YODAW_LLM_STYLE=ollama
export YODAW_LLM_BASE_URL=http://127.0.0.1:11434
export YODAW_LLM_MODEL=qwen2.5-coder:7b
export YODAW_LLM_TIMEOUT_SECONDS=3600
export YODAW_LLM_KEEP_ALIVE=60m
export YODAW_ENABLE_GITHUB=false

.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8844 \
  > /tmp/yodaw_uvicorn.log 2>&1 &
SERVER_PID=$!

for i in $(seq 1 30); do
  curl -s --max-time 2 http://127.0.0.1:8844/api/v1/health > /dev/null && break
  sleep 1
done

START=$(date +%s)

curl -s --max-time 3900 -X POST http://127.0.0.1:8844/api/v1/missions \
  -H "Content-Type: application/json" \
  -d '{"goal":"Refactor greet(name) to use an f-string without changing behavior.","capability":"repo-code","metadata":{"repo_path":"/tmp/yodaw_live_repo","commit_message":"use f-string greeting"}}' \
  > /tmp/yodaw_live_mission.json
CURL_EXIT=$?

END=$(date +%s)

curl -s --max-time 10 http://127.0.0.1:8844/api/v1/learning \
  > /tmp/yodaw_live_learning.json

kill "$SERVER_PID" 2>/dev/null

echo "LIVE_MISSION_DURATION_SECONDS=$((END - START))" > /tmp/yodaw_live_result.txt
echo "CURL_EXIT=$CURL_EXIT" >> /tmp/yodaw_live_result.txt
echo "DONE" >> /tmp/yodaw_live_result.txt
