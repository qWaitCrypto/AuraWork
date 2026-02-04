from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure repo root is on sys.path so `import aura` works when running this file directly.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aura_web.server import run


def main() -> int:
    parser = argparse.ArgumentParser(prog="aura-web", description="Aura web surface (FastAPI + WebSocket).")
    parser.add_argument(
        "--project",
        default=".",
        help="Aura project root (directory containing .aura). Default: current directory.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    run(project_root=Path(args.project), host=str(args.host), port=int(args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

