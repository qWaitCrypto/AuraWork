from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ..stores import ArtifactStore
from .browser_steps import parse_browser_steps


def _maybe_int(args: dict[str, Any], key: str) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Invalid '{key}' (expected int).")
    return value


def _maybe_float(args: dict[str, Any], key: str) -> float | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"Invalid '{key}' (expected number).")
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError(f"Invalid '{key}' (expected number).")


def _resolve_in_project(project_root: Path, rel: str) -> Path:
    rel_path = Path(rel)
    if rel_path.is_absolute():
        raise PermissionError("Path must be relative to project root.")
    candidate = (project_root / rel_path).resolve()
    project_root_resolved = project_root.resolve()
    if candidate != project_root_resolved and project_root_resolved not in candidate.parents:
        raise PermissionError("Path escapes project root.")
    return candidate


def _is_screenshot_to_stdout(argv: list[str]) -> bool:
    if not argv or argv[0] != "screenshot":
        return False
    # `agent-browser screenshot` outputs to stdout unless a path argument is provided.
    # We treat any non-flag argument as a path.
    non_flags = [a for a in argv[1:] if isinstance(a, str) and a and not a.startswith("-")]
    return len(non_flags) == 0


@dataclass(slots=True)
class BrowserRunTool:
    artifact_store: ArtifactStore

    name: ClassVar[str] = "browser__run"
    description: ClassVar[str] = (
        "Run agent-browser commands in a safer, structured way (no shell). "
        "Use for web navigation, snapshots, and extraction. "
        "Some state-changing operations may require user approval depending on policy."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "minItems": 1,
                "description": "Sequence of agent-browser commands (without the leading 'agent-browser').",
                "items": {
                    "anyOf": [
                        {"type": "string", "description": 'e.g. "open https://example.com"'},
                        {"type": "array", "items": {"type": "string"}, "minItems": 1, "description": "argv form"},
                        {
                            "type": "object",
                            "properties": {
                                "command": {"type": "string", "description": "agent-browser subcommand"},
                                "args": {"type": "array", "items": {"type": "string"}, "description": "argv tail"},
                            },
                            "required": ["command"],
                            "additionalProperties": False,
                        },
                    ]
                },
            },
            "cwd": {"type": "string", "description": "Relative working directory (default '.')."},
            "timeout_s": {"type": "number", "minimum": 0, "description": "Per-step timeout seconds (default 30)."},
            "max_output_chars": {
                "type": "integer",
                "minimum": 1,
                "description": "Maximum characters returned for stdout/stderr per step (default 16000).",
            },
            "max_binary_bytes": {
                "type": "integer",
                "minimum": 1,
                "description": "Maximum bytes captured for binary stdout (default 5000000).",
            },
        },
        "required": ["steps"],
        "additionalProperties": False,
    }

    def execute(self, *, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
        steps = parse_browser_steps(args.get("steps"))
        cwd_rel = str(args.get("cwd") or ".")
        cwd_path = _resolve_in_project(project_root, cwd_rel)
        timeout_s = _maybe_float(args, "timeout_s")
        if timeout_s is None:
            timeout_s = 30.0
        max_output_chars = _maybe_int(args, "max_output_chars") or 16000
        max_binary_bytes = _maybe_int(args, "max_binary_bytes") or 5_000_000

        binary = shutil.which("agent-browser")
        if not binary:
            raise FileNotFoundError(
                "agent-browser not found in PATH. Install it or add it to PATH, then retry. "
                "Tip: validate with `which agent-browser` in your shell."
            )

        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"

        results: list[dict[str, Any]] = []
        for step_argv in steps:
            full_argv = [binary, *step_argv]
            started = time.monotonic()
            proc = subprocess.Popen(
                full_argv,
                cwd=str(cwd_path),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )

            timed_out = False
            try:
                stdout_b, stderr_b = proc.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os_pid = getattr(proc, "pid", None)
                    if isinstance(os_pid, int):
                        with contextlib.suppress(Exception):
                            os.killpg(os_pid, signal.SIGKILL)
                except Exception:
                    pass
                try:
                    proc.kill()
                except Exception:
                    pass
                stdout_b, stderr_b = proc.communicate()

            duration_ms = int((time.monotonic() - started) * 1000)
            exit_code = proc.returncode if proc.returncode is not None else -1

            stdout_truncated = False
            stderr_truncated = False
            stdout_text: str | None = None
            stdout_ref: dict[str, Any] | None = None

            if _is_screenshot_to_stdout(step_argv):
                payload = stdout_b or b""
                truncated_bytes = False
                if len(payload) > max_binary_bytes:
                    payload = payload[:max_binary_bytes]
                    truncated_bytes = True
                ref = self.artifact_store.put(
                    payload,
                    kind="browser_screenshot",
                    meta={"summary": "Browser screenshot (stdout)", "truncated": truncated_bytes},
                )
                stdout_ref = ref.to_dict()
                stdout_text = None
                stdout_truncated = truncated_bytes
            else:
                stdout_text = (stdout_b or b"").decode("utf-8", errors="replace")
                if len(stdout_text) > max_output_chars:
                    stdout_truncated = True
                    stdout_text = stdout_text[:max_output_chars] + "…"

            stderr_text = (stderr_b or b"").decode("utf-8", errors="replace")
            if len(stderr_text) > max_output_chars:
                stderr_truncated = True
                stderr_text = stderr_text[:max_output_chars] + "…"

            results.append(
                {
                    "argv": full_argv,
                    "exit_code": exit_code,
                    "timed_out": timed_out,
                    "duration_ms": duration_ms,
                    "stdout": stdout_text,
                    "stdout_ref": stdout_ref,
                    "stderr": stderr_text,
                    "truncated": {"stdout": stdout_truncated, "stderr": stderr_truncated},
                }
            )

        ok = all(r.get("exit_code") == 0 and not r.get("timed_out") for r in results)
        return {
            "ok": ok,
            "steps": results,
        }
