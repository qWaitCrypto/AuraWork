from __future__ import annotations

import argparse
from pathlib import Path

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

