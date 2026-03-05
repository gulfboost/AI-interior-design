#!/bin/sh
# Backend startup: run arq worker in background, then start uvicorn in foreground.
cd /app/api
uv run arq app.workers.main.WorkerSettings &
exec uv run uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
