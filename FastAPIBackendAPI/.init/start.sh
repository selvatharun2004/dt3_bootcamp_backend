#!/usr/bin/env bash
set -euo pipefail
WORKSPACE="/home/kavia/workspace/code-generation/dt3_bootcamp_backend/FastAPIBackendAPI"
cd "$WORKSPACE"
# Activate venv and start uvicorn in background, save PID to .init/uvicorn.pid and redirect logs
source "$WORKSPACE/.venv/bin/activate"
PORT=8000
if ss -ltn | awk '{print $4}' | grep -qE ":${PORT}$|:${PORT}\b"; then
  echo "start: port ${PORT} appears in use" >&2
  exit 3
fi
mkdir -p .init
LOG="$(mktemp /tmp/fastapi.XXXXXX.log)"
"$WORKSPACE/.venv/bin/uvicorn" main:app --host 0.0.0.0 --port "$PORT" >"$LOG" 2>&1 &
PID=$!
echo "$PID" > .init/uvicorn.pid
echo "$LOG" > .init/uvicorn.logpath
# Give caller control; do not wait so process runs in background
exit 0
