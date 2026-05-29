#!/usr/bin/env bash
set -euo pipefail
WORKSPACE="/home/kavia/workspace/code-generation/dt3_bootcamp_backend/FastAPIBackendAPI"
cd "$WORKSPACE"
source "$WORKSPACE/.venv/bin/activate"
PORT=8000
# fail fast if port in use
if ss -ltn | awk '{print $4}' | grep -qE ":${PORT}$|:${PORT}\b"; then
  echo "validation: port ${PORT} appears in use" >&2
  exit 3
fi
LOG=$(mktemp /tmp/fastapi.XXXXXX.log)
"$WORKSPACE/.venv/bin/uvicorn" main:app --host 0.0.0.0 --port "$PORT" >"$LOG" 2>&1 &
PID=$!
trap 'kill "$PID" >/dev/null 2>&1 || true; wait "$PID" 2>/dev/null || true' EXIT
OK=1
for i in {1..20}; do
  if curl --silent --max-time 2 http://127.0.0.1:${PORT}/ | grep -q '"status":"ok"'; then OK=0; break; fi
  sleep 0.5
done
if [ $OK -eq 0 ]; then
  kill "$PID" >/dev/null 2>&1 || true; wait "$PID" 2>/dev/null || true; rm -f "$LOG" || true; echo "validation: ok"; exit 0
else
  echo "validation: failed - no expected response" >&2
  echo "--- server log (head) ---" >&2
  sed -n '1,200p' "$LOG" >&2 || true
  # preserve log path for debugging
  echo "server log preserved at: $LOG" >&2
  exit 2
fi
