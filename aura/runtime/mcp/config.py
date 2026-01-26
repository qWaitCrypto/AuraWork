from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MCP_CONFIG_FILENAME = "mcp.json"


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """
    Minimal MCP server config (v0.3).

    Router mode uses a small set of local tools to list/call MCP tools.
    For now we support only stdio servers (spawned subprocess).
    """

    name: str
    enabled: bool
    command: str
    args: list[str]
    env: dict[str, str]
    cwd: str | None
    timeout_s: float


@dataclass(frozen=True, slots=True)
class McpConfig:
    servers: dict[str, McpServerConfig]
    source: str | None = None


def _as_dict(v: Any) -> dict[str, Any]:
    if isinstance(v, dict):
        return v
    raise ValueError("Expected object.")


def _as_str(v: Any) -> str:
    if isinstance(v, str):
        return v
    raise ValueError("Expected string.")


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    raise ValueError("Expected boolean.")


def _as_float(v: Any) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    raise ValueError("Expected number.")


def _as_str_list(v: Any) -> list[str]:
    if not isinstance(v, list):
        raise ValueError("Expected list.")
    out: list[str] = []
    for item in v:
        if not isinstance(item, str):
            raise ValueError("Expected list of strings.")
        if item:
            out.append(item)
    return out


def _as_env_dict(v: Any) -> dict[str, str]:
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise ValueError("Expected env object.")
    out: dict[str, str] = {}
    for k, val in v.items():
        if not isinstance(k, str) or not k:
            continue
        if not isinstance(val, str):
            continue
        out[k] = val
    return out


def _load_mcp_config_dict(data: Any, *, source: str) -> McpConfig:
    root = _as_dict(data)
    servers_raw = root.get("mcpServers", {})
    if servers_raw is None:
        servers_raw = {}
    servers_obj = _as_dict(servers_raw)

    servers: dict[str, McpServerConfig] = {}
    for name, raw in servers_obj.items():
        if not isinstance(name, str) or not name.strip():
            continue
        cfg = _as_dict(raw)
        enabled = _as_bool(cfg.get("enabled", False))
        command = _as_str(cfg.get("command", ""))
        args = _as_str_list(cfg.get("args", []))
        env = _as_env_dict(cfg.get("env"))
        cwd_raw = cfg.get("cwd")
        if isinstance(cwd_raw, str):
            cwd = cwd_raw.strip() or None
        else:
            cwd = None
        timeout_s = _as_float(cfg.get("timeout_s", 60))

        servers[name] = McpServerConfig(
            name=name,
            enabled=enabled,
            command=command.strip(),
            args=args,
            env=env,
            cwd=cwd,
            timeout_s=timeout_s,
        )

    return McpConfig(servers=servers, source=source)


def mcp_config_path_for_project(project_root: Path) -> Path:
    return project_root / ".aura" / "config" / MCP_CONFIG_FILENAME


def load_mcp_config(*, project_root: Path) -> McpConfig:
    path = mcp_config_path_for_project(project_root)
    if not path.exists():
        return McpConfig(servers={}, source=None)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"MCP config file is not valid JSON: {path} ({e})") from e
    return _load_mcp_config_dict(data, source=str(path))
