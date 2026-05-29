#!/usr/bin/env bash
set -euo pipefail
WORKSPACE="/home/kavia/workspace/code-generation/dt3_bootcamp_backend/FastAPIBackendAPI"
cd "$WORKSPACE"
source "$WORKSPACE/.venv/bin/activate"
# Run pytest, fail-fast
pytest -q --maxfail=1 || exit $?
exit 0
