from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ..agent_browser import (
    agent_browser_session_for_aura_session,
    agent_browser_session_for_subagent_run,
    ensure_agent_browser_stream_port_for_session,
)
from ..stores import ArtifactStore
from .browser_steps import parse_browser_steps
from .runtime import ToolExecutionContext


logger = logging.getLogger(__name__)


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except Exception:
        return default
    return max(minimum, min(maximum, value))


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except Exception:
        return default
    return max(minimum, min(maximum, value))


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


def _maybe_bool(args: dict[str, Any], key: str) -> bool | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise ValueError(f"Invalid '{key}' (expected bool).")


def _resolve_in_project(project_root: Path, rel: str) -> Path:
    rel_path = Path(rel)
    if rel_path.is_absolute():
        raise PermissionError("Path must be relative to project root.")
    candidate = (project_root / rel_path).resolve()
    project_root_resolved = project_root.resolve()
    if candidate != project_root_resolved and project_root_resolved not in candidate.parents:
        raise PermissionError("Path escapes project root.")
    return candidate


def _screenshot_path_arg(argv: list[str]) -> str | None:
    if not argv or argv[0] != "screenshot":
        return None
    # Treat the first non-flag arg as the felt "path" argument.
    for a in argv[1:]:
        if isinstance(a, str) and a and not a.startswith("-"):
            return a
    return None


def _extract_screenshot_path(stdout_text: str) -> str | None:
    """
    Best-effort extraction of screenshot file path from agent-browser stdout.

    agent-browser v0.8+ saves screenshots to a file and reports the path.
    Output format may vary (plain path, JSON, etc.), so keep this heuristic.
    """

    import re

    def _strip_ansi(text: str) -> str:
        # agent-browser prints colored output by default (ANSI escape sequences).
        return re.sub(r"\x1b\[[0-9;]*m", "", text)

    s = _strip_ansi(str(stdout_text or "")).strip()
    if not s:
        return None

    # Try JSON first: {"success":true,"data":{"path":"..."}} or similar.
    try:
        import json

        obj = json.loads(s)
        if isinstance(obj, dict):
            data = obj.get("data")
            if isinstance(data, dict):
                p = data.get("path")
                if isinstance(p, str) and p.strip():
                    return p.strip()
            p = obj.get("path")
            if isinstance(p, str) and p.strip():
                return p.strip()
    except Exception:
        logger.warning("Failed to parse browser screenshot output as JSON.", exc_info=True)

    # Fallback: scan lines for a path-like substring ending in an image extension.
    # Handles common agent-browser output like: "Screenshot saved to /run/user/.../x.png"
    path_re = re.compile(r"(?P<path>(?:/[^\s\"']+|[^\s\"']+)\.(?:png|jpg|jpeg|webp))", re.IGNORECASE)
    for line in reversed(s.splitlines()):
        candidate = line.strip().strip('"').strip("'")
        if not candidate:
            continue
        m = path_re.search(candidate)
        if m:
            return m.group("path").strip()
    return None


def _is_open_step(step_argv: list[str]) -> bool:
    if not step_argv:
        return False
    return str(step_argv[0]).strip().lower() == "open"


def _effective_step_timeout_s(*, step_argv: list[str], timeout_s: float, timeout_explicit: bool) -> float:
    if timeout_explicit:
        return timeout_s
    if _is_open_step(step_argv):
        open_timeout_s = _env_float("AURA_BROWSER_OPEN_TIMEOUT_S", 45.0, minimum=1.0, maximum=600.0)
        return max(timeout_s, open_timeout_s)
    return timeout_s


def _is_retryable_open_stderr(stderr_text: str) -> bool:
    text = str(stderr_text or "").lower()
    if not text.strip():
        return False
    markers = (
        "timed out",
        "timeout",
        "temporary failure in name resolution",
        "connection reset",
        "connection closed",
        "econnreset",
        "econnrefused",
        "enetunreach",
        "net::err_",
        "err_connection_",
        "err_name_not_resolved",
        "err_network_changed",
        "err_internet_disconnected",
    )
    return any(marker in text for marker in markers)


def _should_retry_open_step(
    *,
    step_argv: list[str],
    timed_out: bool,
    exit_code: int,
    stderr_text: str,
    attempt: int,
    max_attempts: int,
) -> bool:
    if attempt >= max_attempts:
        return False
    if not _is_open_step(step_argv):
        return False
    if timed_out:
        return True
    if exit_code == 0:
        return False
    return _is_retryable_open_stderr(stderr_text)


@dataclass(slots=True)
class BrowserRunTool:
    artifact_store: ArtifactStore

    _MAX_STEPS_PER_CALL: ClassVar[int] = 10

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
                "maxItems": _MAX_STEPS_PER_CALL,
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
            "continue_on_error": {
                "type": "boolean",
                "description": "Whether to continue remaining steps after a failed/timed-out step (default false).",
            },
        },
        "required": ["steps"],
        "additionalProperties": False,
    }

    def execute(self, *, args: dict[str, Any], project_root: Path, context: ToolExecutionContext | None = None) -> dict[str, Any]:
        steps = parse_browser_steps(args.get("steps"))
        if len(steps) > self._MAX_STEPS_PER_CALL:
            raise ValueError(
                f"Too many browser steps in one call: {len(steps)} > {self._MAX_STEPS_PER_CALL}. "
                f"Split into smaller batches (at most {self._MAX_STEPS_PER_CALL} steps per browser__run call)."
            )
        cwd_rel = str(args.get("cwd") or ".")
        cwd_path = _resolve_in_project(project_root, cwd_rel)
        timeout_arg = _maybe_float(args, "timeout_s")
        timeout_s = timeout_arg if timeout_arg is not None else 30.0
        max_output_chars = _maybe_int(args, "max_output_chars") or 16000
        max_binary_bytes = _maybe_int(args, "max_binary_bytes") or 5_000_000
        continue_on_error = _maybe_bool(args, "continue_on_error") or False
        open_step_max_attempts = _env_int("AURA_BROWSER_OPEN_MAX_ATTEMPTS", 2, minimum=1, maximum=4)
        open_step_retry_delay_s = _env_float("AURA_BROWSER_OPEN_RETRY_DELAY_S", 1.0, minimum=0.0, maximum=30.0)

        if continue_on_error and context is not None:
            meta = context.metadata if isinstance(getattr(context, "metadata", None), dict) else {}
            preset_name = meta.get("aura_subagent_preset") if isinstance(meta, dict) else None
            if isinstance(preset_name, str) and preset_name.strip() == "browser_worker":
                continue_on_error = False

        binary = shutil.which("agent-browser")
        if not binary:
            raise FileNotFoundError(
                "agent-browser not found in PATH. Install it or add it to PATH, then retry. "
                "Tip: validate with `which agent-browser` in your shell."
            )

        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        agent_session: str | None = None
        stream_port: int | None = None

        if context is not None:
            meta = context.metadata if isinstance(getattr(context, "metadata", None), dict) else {}
            preferred_session = meta.get("aura_browser_agent_session") if isinstance(meta, dict) else None
            if isinstance(preferred_session, str) and preferred_session.strip():
                agent_session = preferred_session.strip()
            else:
                subagent_run_id = meta.get("aura_subagent_run_id") if isinstance(meta, dict) else None
                if isinstance(subagent_run_id, str) and subagent_run_id.strip():
                    agent_session = agent_browser_session_for_subagent_run(
                        aura_session_id=context.session_id,
                        subagent_run_id=subagent_run_id.strip(),
                    )
                else:
                    agent_session = agent_browser_session_for_aura_session(context.session_id)

            env["AGENT_BROWSER_SESSION"] = agent_session
            if str(env.get("AURA_ENABLE_BROWSER_STREAMING") or "").strip() == "1":
                stream_port = ensure_agent_browser_stream_port_for_session(project_root, agent_session=agent_session)
                env["AGENT_BROWSER_STREAM_PORT"] = str(stream_port)

        results: list[dict[str, Any]] = []
        first_failure_index: int | None = None
        for step_index, step_argv in enumerate(steps):
            step_timeout_s = _effective_step_timeout_s(
                step_argv=step_argv,
                timeout_s=timeout_s,
                timeout_explicit=(timeout_arg is not None),
            )
            step_max_attempts = open_step_max_attempts if _is_open_step(step_argv) else 1
            step_result: dict[str, Any] | None = None
            attempt = 1
            while True:
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
                    stdout_b, stderr_b = proc.communicate(timeout=step_timeout_s)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    try:
                        os_pid = getattr(proc, "pid", None)
                        if isinstance(os_pid, int):
                            with contextlib.suppress(Exception):
                                os.killpg(os_pid, signal.SIGKILL)
                    except Exception:
                        logger.warning("Failed to kill process group for timed out browser step %s.", step_index, exc_info=True)
                    try:
                        proc.kill()
                    except Exception:
                        logger.warning("Failed to kill timed out browser process for step %s.", step_index, exc_info=True)
                    stdout_b, stderr_b = proc.communicate()

                duration_ms = int((time.monotonic() - started) * 1000)
                exit_code = proc.returncode if proc.returncode is not None else -1

                stdout_truncated = False
                stderr_truncated = False
                stdout_text: str | None = None
                stdout_ref: dict[str, Any] | None = None

                stdout_text = (stdout_b or b"").decode("utf-8", errors="replace")
                if len(stdout_text) > max_output_chars:
                    stdout_truncated = True
                    stdout_text = stdout_text[:max_output_chars] + "…"

                # agent-browser v0.8+ screenshots are written to a file and stdout reports the path.
                # Best effort: if this step is `screenshot` without explicit path, capture the image bytes as an artifact.
                if _screenshot_path_arg(step_argv) is None and (step_argv and step_argv[0] == "screenshot"):
                    shot_path = _extract_screenshot_path(stdout_text)
                    if shot_path:
                        try:
                            p = Path(shot_path).expanduser()
                            if p.is_file():
                                payload = p.read_bytes()
                                truncated_bytes = False
                                if len(payload) > max_binary_bytes:
                                    payload = payload[:max_binary_bytes]
                                    truncated_bytes = True
                                ref = self.artifact_store.put(
                                    payload,
                                    kind="browser_screenshot",
                                    meta={
                                        "summary": "Browser screenshot",
                                        "source": "agent-browser",
                                        "path": str(p),
                                        "truncated": truncated_bytes,
                                    },
                                )
                                stdout_ref = ref.to_dict()
                                stdout_truncated = stdout_truncated or truncated_bytes
                        except Exception:
                            logger.warning(
                                "Failed to capture browser screenshot artifact from path: %s",
                                shot_path,
                                exc_info=True,
                            )

                stderr_text = (stderr_b or b"").decode("utf-8", errors="replace")
                if len(stderr_text) > max_output_chars:
                    stderr_truncated = True
                    stderr_text = stderr_text[:max_output_chars] + "…"

                step_result = {
                    "step_index": step_index,
                    "argv": full_argv,
                    "exit_code": exit_code,
                    "timed_out": timed_out,
                    "duration_ms": duration_ms,
                    "stdout": stdout_text,
                    "stdout_ref": stdout_ref,
                    "stderr": stderr_text,
                    "truncated": {"stdout": stdout_truncated, "stderr": stderr_truncated},
                }

                if not _should_retry_open_step(
                    step_argv=step_argv,
                    timed_out=timed_out,
                    exit_code=exit_code,
                    stderr_text=stderr_text,
                    attempt=attempt,
                    max_attempts=step_max_attempts,
                ):
                    break

                logger.warning(
                    "Retrying browser open step after transient failure (%s/%s): %s",
                    attempt,
                    step_max_attempts,
                    " ".join(full_argv),
                )
                attempt += 1
                if open_step_retry_delay_s > 0:
                    time.sleep(open_step_retry_delay_s)

            if step_result is None:
                continue
            results.append(step_result)
            if (step_result.get("timed_out") is True or int(step_result.get("exit_code") or 0) != 0) and first_failure_index is None:
                first_failure_index = step_index
                if not continue_on_error:
                    break

        ok = first_failure_index is None
        out: dict[str, Any] = {
            "ok": ok,
            "steps": results,
            "requested_steps": len(steps),
            "executed_steps": len(results),
        }

        if first_failure_index is not None and 0 <= first_failure_index < len(results):
            failed_step = results[first_failure_index]
            out["failed_step_index"] = first_failure_index
            out["failed_step"] = failed_step

            stderr = failed_step.get("stderr")
            err = stderr.strip() if isinstance(stderr, str) and stderr.strip() else None
            if not err:
                if failed_step.get("timed_out") is True:
                    err = "browser__run step timed out"
                else:
                    exit_code = failed_step.get("exit_code")
                    if isinstance(exit_code, int):
                        err = f"browser__run step failed (exit_code={exit_code})"
            if isinstance(err, str) and err:
                out["error"] = err

            out["error_code"] = "timeout" if failed_step.get("timed_out") is True else "tool_failed"
            out["stopped_after_failure"] = (not continue_on_error) and (len(results) < len(steps))

        if isinstance(agent_session, str) and agent_session.strip():
            out["agent_session"] = agent_session.strip()
        if isinstance(stream_port, int) and 1 <= stream_port <= 65535:
            out["stream_port"] = int(stream_port)
        return out
