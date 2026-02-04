from __future__ import annotations

import os
import re
import socket
import tempfile
import hashlib
from pathlib import Path

from .project import RuntimePaths


def agent_browser_session_for_aura_session(aura_session_id: str) -> str:
    """
    Return an agent-browser session name derived from the Aura session ID.

    Keep it filesystem-safe because agent-browser uses the session name for
    socket/pid/port filenames.
    """

    raw = str(aura_session_id or "").strip()
    if not raw:
        return "aura_default"

    # agent-browser uses Unix domain sockets on non-Windows platforms. Those
    # sockets have a small maximum path length (~100 bytes). Therefore, keep the
    # session name short and deterministic to avoid "socket path too long".
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    safe = re.sub(r"[^a-zA-Z0-9]+", "_", raw).strip("_")
    prefix = safe[:8] if safe else ""
    if prefix:
        return f"aura_{prefix}_{digest}"
    return f"aura_{digest}"


def agent_browser_socket_dir_for_project(project_root: Path) -> Path:
    """
    Directory used by Aura to store agent-browser-related state for this project.

    Note: agent-browser itself uses its own socket directory (see
    `agent_browser_daemon_socket_dir()`), which should live on a filesystem that
    supports Unix domain sockets (not e.g. WSL /mnt/* mounts).
    """

    paths = RuntimePaths.for_project(project_root)
    return paths.state_dir / "agent-browser"


def agent_browser_stream_port_file(project_root: Path, *, aura_session_id: str) -> Path:
    session = agent_browser_session_for_aura_session(aura_session_id)
    return agent_browser_socket_dir_for_project(project_root) / f"{session}.aura_stream_port"


def agent_browser_daemon_stream_file(project_root: Path, *, aura_session_id: str) -> Path:
    session = agent_browser_session_for_aura_session(aura_session_id)
    return agent_browser_daemon_socket_dir() / f"{session}.stream"


def agent_browser_daemon_socket_dir() -> Path:
    """
    Resolve the socket directory used by agent-browser daemon, matching its own
    default behavior:
      1) AGENT_BROWSER_SOCKET_DIR (explicit override)
      2) XDG_RUNTIME_DIR/agent-browser
      3) ~/.agent-browser
      4) <tmp>/agent-browser
    """

    override = os.environ.get("AGENT_BROWSER_SOCKET_DIR")
    if override and override.strip():
        return Path(override).expanduser().resolve()

    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and xdg.strip():
        return (Path(xdg).expanduser().resolve() / "agent-browser").resolve()

    home = Path.home()
    if str(home).strip():
        return (home / ".agent-browser").resolve()

    return (Path(tempfile.gettempdir()) / "agent-browser").resolve()


def allocate_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def ensure_agent_browser_stream_port(project_root: Path, *, aura_session_id: str) -> int:
    port_file = agent_browser_stream_port_file(project_root, aura_session_id=aura_session_id)
    port_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        raw = port_file.read_text(encoding="utf-8").strip()
        port = int(raw)
        if 1 <= port <= 65535:
            return port
    except Exception:
        pass

    port = allocate_loopback_port()
    port_file.write_text(f"{port}\n", encoding="utf-8")
    return port
