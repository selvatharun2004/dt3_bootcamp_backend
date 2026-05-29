#!/usr/bin/env bash
set -euo pipefail
WORKSPACE="/home/kavia/workspace/code-generation/dt3_bootcamp_backend/FastAPIBackendAPI"
mkdir -p "$WORKSPACE/tests" && cd "$WORKSPACE"
cat > "$WORKSPACE/main.py" <<'PY'
from fastapi import FastAPI
app = FastAPI()
@app.get("/")
async def read_root():
    return {"status":"ok"}
PY
cat > "$WORKSPACE/.env" <<'ENV'
# Local dev env - change DATABASE_URL as needed
DATABASE_URL=sqlite:///./dev.db
SECRET_KEY=devsecret
ENV
cat > "$WORKSPACE/requirements.txt" <<'RQ'
fastapi~=0.100
uvicorn[standard]~=0.23
httpx~=0.24
pytest~=7.4
requests~=2.31
RQ
cat > "$WORKSPACE/start.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
WORKSPACE="/home/kavia/workspace/code-generation/dt3_bootcamp_backend/FastAPIBackendAPI"
# Activate workspace-local venv; assume .venv exists or will be created by environment step
if [ -f "$WORKSPACE/.venv/bin/activate" ]; then
  # shellcheck disable=SC1090
  source "$WORKSPACE/.venv/bin/activate"
  exec "$WORKSPACE/.venv/bin/uvicorn" main:app --host 0.0.0.0 --port 8000
else
  echo "ERROR: venv not found at $WORKSPACE/.venv. Create it with: python3 -m venv $WORKSPACE/.venv" >&2
  exit 2
fi
SH
chmod +x "$WORKSPACE/start.sh"
cat > "$WORKSPACE/.gitignore" <<'GI'
.venv
.env
dev.db
__pycache__
*.pyc
GI
cat > "$WORKSPACE/README.dev.md" <<'RD'
Local dev: edit .env to change DATABASE_URL or secrets. Create a venv with:
  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
Run the dev server with:
  ./start.sh
RD
exit 0
