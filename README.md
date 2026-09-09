# YODAW Code Core

Run locally:

    source .venv/bin/activate
    uvicorn app.main:app --host 127.0.0.1 --port 8844

Health:

    curl http://127.0.0.1:8844/api/v1/health

Mission:

    curl -X POST \
      http://127.0.0.1:8844/api/v1/missions \
      -H "Content-Type: application/json" \
      -d '{"goal":"Test YODAW Code Core","capability":"code"}'

API docs:

    http://127.0.0.1:8844/docs
