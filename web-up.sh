#!/usr/bin/env bash
set -euo pipefail

# One-command launcher for Aura web UI + backend.
# - Starts backend (FastAPI/uvicorn) and frontend (Vite)
# - Prints URLs
# - Ctrl+C stops both

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$ROOT_DIR/web/frontend"
BACKEND_DIR="$ROOT_DIR/web/backend"

BACKEND_HOST="${AURA_WEB_HOST:-127.0.0.1}"
BACKEND_PORT="${AURA_WEB_PORT:-8000}"
FRONTEND_PORT="${AURA_WEB_FRONTEND_PORT:-5173}"

REPLAY_CAP="${AURA_WEB_WS_REPLAY_CAP:-1000}"

have() { command -v "$1" >/dev/null 2>&1; }

if ! have node; then
  echo "ERROR: node is required" >&2
  exit 1
fi

if ! have npm; then
  echo "ERROR: npm is required" >&2
  exit 1
fi

if ! have python; then
  echo "ERROR: python is required" >&2
  exit 1
fi

cleanup() {
  set +e
  if [[ -n "${FRONT_PID:-}" ]]; then kill "$FRONT_PID" 2>/dev/null; fi
  if [[ -n "${BACK_PID:-}" ]]; then kill "$BACK_PID" 2>/dev/null; fi
}
trap cleanup EXIT INT TERM

echo "==> Starting backend (FastAPI)"
(
  cd "$BACKEND_DIR"
  export AURA_WEB_WS_REPLAY_CAP="$REPLAY_CAP"
  # Ensure backend deps exist for python import.
  python -c "import fastapi, uvicorn" >/dev/null 2>&1 || {
    echo "ERROR: backend deps missing. Install: pip install -r $BACKEND_DIR/requirements.txt" >&2
    exit 1
  }

  # Run the app from code without relying on repo-root being a Python package.
  # We add web/backend to sys.path and import aura_web.server from there.
  python - <<'PY'
from __future__ import annotations

from pathlib import Path
import sys
import uvicorn

repo_root = Path.cwd().parents[1]
backend_root = repo_root / "web" / "backend"

# Add both repo root (for `aura.*`) and backend root (for `aura_web.*`).
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(backend_root))

from aura_web.server import build_app  # noqa: E402

app = build_app(project_root=repo_root)
uvicorn.run(app, host="${AURA_WEB_HOST:-127.0.0.1}", port=int("${AURA_WEB_PORT:-8000}"), log_level="info")
PY
) &
BACK_PID=$!

# Small wait to let backend bind.
sleep 0.8

echo "==> Starting frontend (Vite)"
(
  cd "$FRONTEND_DIR"
  npm install
  # Force Vite to bind to a predictable host/port for copy-paste URL.
  npm run dev -- --host 127.0.0.1 --port "$FRONTEND_PORT"
) &
FRONT_PID=$!

cat <<EOF

Aura web is starting.

Frontend:  http://127.0.0.1:${FRONTEND_PORT}
Backend:   http://${BACKEND_HOST}:${BACKEND_PORT}

Notes:
- If you only want frontend, run: (cd web/frontend && npm run dev)
- Replay cap is set via AURA_WEB_WS_REPLAY_CAP=${REPLAY_CAP}

Press Ctrl+C to stop.
EOF

wait
