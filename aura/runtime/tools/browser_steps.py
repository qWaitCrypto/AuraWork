from __future__ import annotations

import os
import shlex
from typing import Any


def _looks_like_agent_browser(token: str) -> bool:
    base = os.path.basename(token)
    return base in {"agent-browser", "agent-browser.exe"}


def _normalize_step(argv: list[str]) -> list[str]:
    if not argv:
        raise ValueError("Invalid 'steps' entry (empty command).")
    if _looks_like_agent_browser(argv[0]):
        argv = argv[1:]
    if not argv:
        raise ValueError("Invalid 'steps' entry (missing agent-browser subcommand).")
    return argv


def parse_browser_steps(raw: Any) -> list[list[str]]:
    """
    Parse `browser__run.steps` into argv arrays.

    Accepts:
    - string: 'open https://example.com'
    - list[str]: ['open', 'https://example.com']
    - object: {'command': 'open', 'args': ['https://example.com']}

    For convenience/compatibility, we also accept the user mistakenly including the
    `agent-browser` prefix (e.g. 'agent-browser open ...') and strip it.
    """

    if not isinstance(raw, list) or not raw:
        raise ValueError("Missing or invalid 'steps' (expected non-empty list).")

    out: list[list[str]] = []
    for item in raw:
        argv: list[str]
        if isinstance(item, str):
            argv = shlex.split(item)
        elif isinstance(item, list):
            if any(not isinstance(x, str) or not x for x in item):
                raise ValueError("Invalid 'steps' entry (expected list of non-empty strings).")
            argv = list(item)
        elif isinstance(item, dict):
            cmd = item.get("command")
            tail = item.get("args")
            if not isinstance(cmd, str) or not cmd.strip():
                raise ValueError("Invalid 'steps' entry (expected object with non-empty 'command').")
            if tail is None:
                argv = [cmd.strip()]
            else:
                if not isinstance(tail, list) or any(not isinstance(x, str) for x in tail):
                    raise ValueError("Invalid 'steps' entry 'args' (expected list of strings).")
                argv = [cmd.strip(), *[str(x) for x in tail if str(x)]]
        else:
            raise ValueError("Invalid 'steps' entry (expected string, list, or object).")

        out.append(_normalize_step(argv))

    return out

