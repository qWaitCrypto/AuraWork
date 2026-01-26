# Aura Web (implementation)

This directory contains the Web surface implementation for AuraWork.

Constraints (per project rule):
- **Never modify** `aura/` (core runtime). This web surface is a separate layer that imports Aura runtime APIs.
- Only `web/` and `docs/` are modified by this implementation.

## Structure

- `web/backend/` — FastAPI server (WebSocket + small REST) that:
  - reads/writes the project’s `.aura/` stores via Aura runtime
  - streams events to clients
  - accepts chat input and approval decisions
- `web/frontend/` — React + TypeScript UI, based on `aura_full_client_prototype.html`

## Quick start (dev)

Backend (from repo root):
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r web/backend/requirements.txt
python web/backend/main.py --project /mnt/e/test
```

Note:
- `--project` must point to the **Aura project root** (directory containing `.aura/`). In this repo, `/mnt/e/aurawork/.aura` may be empty; your test runs live under `/mnt/e/test/.aura`.

Frontend (from repo root):
```bash
cd web/frontend
npm install
npm run dev
```

Then open the frontend dev server URL (printed by Vite).
