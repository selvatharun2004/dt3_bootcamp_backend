#!/usr/bin/env bash
set -euo pipefail
WORKSPACE="/home/kavia/workspace/code-generation/dt3_bootcamp_backend/FastAPIBackendAPI"
cd "$WORKSPACE"
# create workspace venv if missing (idempotent)
if [ ! -d "$WORKSPACE/.venv" ]; then
  python3 -m venv "$WORKSPACE/.venv"
fi
# activate venv and ensure pip is available
# shellcheck source=/dev/null
source "$WORKSPACE/.venv/bin/activate"
python -m ensurepip --upgrade >/dev/null 2>&1 || true
python -m pip install --upgrade pip --quiet
python -m pip install -r "$WORKSPACE/requirements.txt" --quiet
python - <<'PY'
import importlib,sys
pkgs=['fastapi','uvicorn','httpx','pytest','requests']
for p in pkgs:
    try:
        m=importlib.import_module(p)
        ver=getattr(m,'__version__',getattr(m,'VERSION','unknown'))
        print(f"{p}:{ver}")
    except Exception as e:
        print(f"{p}:IMPORT_FAILED:{e}", file=sys.stderr)
        raise
PY
exit 0
