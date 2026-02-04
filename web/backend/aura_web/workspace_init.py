from __future__ import annotations

from pathlib import Path


_MODELS_JSON_TEMPLATE_LINES: list[str] = [
    "{",
    '  "default_profile": "main",',
    '  "profiles": {',
    '    "main": {',
    '      "provider_kind": "openai_compatible",',
    '      "base_url": "",',
    '      "model": "your-model-name",',
    '      "api_key": "",',
    '      "timeout_s": 60,',
    '      "limits": { "context_limit_tokens": null, "max_output_tokens": null },',
    '      "context_management": {',
    '        "auto_compact_threshold_ratio": null,',
    '        "history_budget_ratio": 0.2,',
    '        "history_budget_fallback_tokens": 8000,',
    '        "tool_output_budget_tokens": 400',
    "      },",
    '      "capabilities": { "supports_tools": true, "supports_streaming": true }',
    "    },",
    '    "anthropic": {',
    '      "provider_kind": "anthropic",',
    '      "base_url": "",',
    '      "model": "claude-3-5-sonnet-20241022",',
    '      "api_key": "replace-me",',
    '      "max_tokens": 1024,',
    '      "timeout_s": 60,',
    '      "limits": { "context_limit_tokens": null, "max_output_tokens": null },',
    '      "context_management": {',
    '        "auto_compact_threshold_ratio": null,',
    '        "history_budget_ratio": 0.2,',
    '        "history_budget_fallback_tokens": 8000,',
    '        "tool_output_budget_tokens": 400',
    "      },",
    '      "capabilities": { "supports_tools": true, "supports_streaming": true }',
    "    },",
    '    "gemini": {',
    '      "provider_kind": "gemini",',
    '      "base_url": "",',
    '      "model": "gemini-2.5-flash-lite",',
    '      "api_key": "replace-me",',
    '      "timeout_s": 60,',
    '      "limits": { "context_limit_tokens": null, "max_output_tokens": null },',
    '      "context_management": {',
    '        "auto_compact_threshold_ratio": null,',
    '        "history_budget_ratio": 0.2,',
    '        "history_budget_fallback_tokens": 8000,',
    '        "tool_output_budget_tokens": 400',
    "      },",
    '      "capabilities": { "supports_tools": true, "supports_streaming": true },',
    '      "default_params": {',
    '        "generationConfig": {',
    '          "thinkingConfig": { "includeThoughts": true, "thinkingBudget": 8192 }',
    "        }",
    "      }",
    "    }",
    "  }",
    "}",
    "",
]


_MCP_JSON_TEMPLATE_LINES: list[str] = [
    "{",
    '  "_comment": "MCP server configuration. Add servers under mcpServers and set enabled=true. Note: Aura treats MCP tools as high-risk by default (approval-gated unless tool approval mode is trusted).",',
    '  "mcpServers": {',
    '    "filesystem": {',
    '      "_comment": "General-purpose filesystem MCP. Prefer Aura built-in project tools when possible. IMPORTANT: restrict allowed directories.",',
    '      "enabled": false,',
    '      "command": "npx",',
    '      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/allowed/directory"],',
    '      "env": {},',
    '      "cwd": "",',
    '      "timeout_s": 60',
    "    },",
    '    "memory": {',
    '      "_comment": "General-purpose memory MCP (useful for long-lived notes/recall across sessions).",',
    '      "enabled": false,',
    '      "command": "npx",',
    '      "args": ["-y", "@modelcontextprotocol/server-memory"],',
    '      "env": {},',
    '      "cwd": "",',
    '      "timeout_s": 60',
    "    },",
    '    "fetch": {',
    '      "_comment": "Network-capable fetch MCP. Keep disabled unless you explicitly want network access via MCP.",',
    '      "enabled": false,',
    '      "command": "uvx",',
    '      "args": ["mcp-server-fetch"],',
    '      "env": {},',
    '      "cwd": "",',
    '      "timeout_s": 60',
    "    },",
    '    "example": {',
    '      "enabled": false,',
    '      "command": "",',
    '      "args": [],',
    '      "env": {},',
    '      "cwd": "",',
    '      "timeout_s": 60',
    "    }",
    "  }",
    "}",
    "",
]


def init_workspace(project_root: Path) -> None:
    """Initialize an Aura workspace directory for web sessions.

    Creates the `.aura/` directory tree and seeds `.aura/config/models.json` and
    `.aura/config/mcp.json` if missing.

    This mirrors `aura/cli.py:_cmd_init` but avoids importing/invoking the CLI.
    """

    project_root = project_root.expanduser().resolve()

    if project_root.exists() and not project_root.is_dir():
        raise ValueError(f"path exists and is not a directory: {project_root}")

    project_root.mkdir(parents=True, exist_ok=True)

    system_dirs = [
        project_root / ".aura" / "config",
        project_root / ".aura" / "policy",
        project_root / ".aura" / "skills",
        project_root / ".aura" / "sessions",
        project_root / ".aura" / "events",
        project_root / ".aura" / "artifacts",
        project_root / ".aura" / "runs",
        project_root / ".aura" / "state",
        project_root / ".aura" / "index",
        project_root / ".aura" / "cache",
        project_root / ".aura" / "tmp",
        project_root / ".aura" / "state" / "approvals",
    ]

    for directory in system_dirs:
        directory.mkdir(parents=True, exist_ok=True)

    models_path = project_root / ".aura" / "config" / "models.json"
    if not models_path.exists():
        models_path.write_text("\n".join(_MODELS_JSON_TEMPLATE_LINES), encoding="utf-8")

    mcp_path = project_root / ".aura" / "config" / "mcp.json"
    if not mcp_path.exists():
        mcp_path.write_text("\n".join(_MCP_JSON_TEMPLATE_LINES), encoding="utf-8")
