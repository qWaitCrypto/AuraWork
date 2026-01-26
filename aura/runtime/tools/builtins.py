from __future__ import annotations

import fnmatch
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Missing or invalid '{key}' (expected non-empty string).")
    return value


def _maybe_int(args: dict[str, Any], key: str) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"Invalid '{key}' (expected int).")
    if not isinstance(value, int):
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
    raise ValueError(f"Invalid '{key}' (expected boolean).")


def _maybe_str_list(args: dict[str, Any], key: str) -> list[str] | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(f"Invalid '{key}' (expected list of strings).")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError(f"Invalid '{key}' (expected list of non-empty strings).")
        out.append(item)
    return out


def _resolve_in_project(project_root: Path, rel: str) -> Path:
    rel_path = Path(rel)
    if rel_path.is_absolute():
        raise PermissionError("Path must be relative to project root.")
    candidate = (project_root / rel_path).resolve()
    project_root_resolved = project_root.resolve()
    if candidate != project_root_resolved and project_root_resolved not in candidate.parents:
        raise PermissionError("Path escapes project root.")
    return candidate


@dataclass(frozen=True, slots=True)
class ProjectReadTextTool:
    name: str = "project__read_text"
    description: str = (
        "Read a UTF-8 text file under the project root. "
        "Returns at most max_chars characters (default 8000) and always records an artifact reference."
    )
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path within the project root."},
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return (default 8000).",
                    "minimum": 1,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
        path = _require_str(args, "path")
        max_chars = _maybe_int(args, "max_chars") or 8000
        file_path = _resolve_in_project(project_root, path)
        data = file_path.read_bytes()
        text = data.decode("utf-8", errors="replace")
        truncated = False
        if len(text) > max_chars:
            truncated = True
            text = text[:max_chars]
        return {
            "path": str(Path(path)),
            "truncated": truncated,
            "content": text,
        }

@dataclass(frozen=True, slots=True)
class ProjectSearchTextTool:
    name: str = "project__search_text"
    description: str = (
        "Search UTF-8 text files under the project root for a query. "
        "Returns a bounded list of matches (default max_results=20)."
    )
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search string or regex pattern."},
                "path": {
                    "type": "string",
                    "description": "Relative directory to search within (default '.').",
                },
                "regex": {"type": "boolean", "description": "Treat query as regex (default false)."},
                "case_sensitive": {"type": "boolean", "description": "Case sensitive search (default true)."},
                "include_globs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional include globs matched against relative path.",
                },
                "exclude_globs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional exclude globs matched against relative path.",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum matches to return (default 20).",
                },
                "max_chars_per_match": {
                    "type": "integer",
                    "minimum": 10,
                    "description": "Maximum characters to return per matching line (default 200).",
                },
                "max_file_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Skip files larger than this (default 2_000_000).",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
        query = _require_str(args, "query")
        rel_base = str(args.get("path") or ".")
        base_dir = _resolve_in_project(project_root, rel_base)
        regex = _maybe_bool(args, "regex") or False
        case_sensitive = _maybe_bool(args, "case_sensitive")
        if case_sensitive is None:
            case_sensitive = True
        include_globs = _maybe_str_list(args, "include_globs") or []
        exclude_globs = _maybe_str_list(args, "exclude_globs") or []
        max_results = _maybe_int(args, "max_results") or 20
        max_chars_per_match = _maybe_int(args, "max_chars_per_match") or 200
        max_file_bytes = _maybe_int(args, "max_file_bytes") or 2_000_000

        ignored_dirs = {
            ".git",
            ".aura",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            "node_modules",
            "dist",
            "build",
        }

        if regex:
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                pattern = re.compile(query, flags)
            except re.error as e:
                return {
                    "ok": False,
                    "query": query,
                    "regex": True,
                    "case_sensitive": case_sensitive,
                    "base": str(Path(rel_base)),
                    "error": f"Invalid regex: {e}",
                    "matches": [],
                }
        else:
            pattern = None
            needle = query if case_sensitive else query.lower()

        matches: list[dict[str, Any]] = []
        truncated = False
        files_scanned = 0

        def _path_included(rel_path: str) -> bool:
            if include_globs and not any(fnmatch.fnmatch(rel_path, g) for g in include_globs):
                return False
            if exclude_globs and any(fnmatch.fnmatch(rel_path, g) for g in exclude_globs):
                return False
            return True

        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d not in ignored_dirs]
            for filename in files:
                file_path = Path(root) / filename
                try:
                    rel_path = str(file_path.relative_to(project_root))
                except Exception:
                    continue
                if not _path_included(rel_path):
                    continue
                try:
                    st = file_path.stat()
                except OSError:
                    continue
                if st.st_size > max_file_bytes:
                    continue
                try:
                    data = file_path.read_bytes()
                except OSError:
                    continue
                if b"\x00" in data:
                    continue

                files_scanned += 1
                text = data.decode("utf-8", errors="replace")
                for line_no, line in enumerate(text.splitlines(), start=1):
                    if pattern is not None:
                        for m in pattern.finditer(line):
                            col = m.start() + 1
                            snippet = line
                            if len(snippet) > max_chars_per_match:
                                snippet = snippet[: max_chars_per_match - 1] + "…"
                            matches.append(
                                {
                                    "path": rel_path,
                                    "line": line_no,
                                    "col": col,
                                    "match": m.group(0),
                                    "text": snippet,
                                }
                            )
                            if len(matches) >= max_results:
                                truncated = True
                                break
                        if truncated:
                            break
                    else:
                        hay = line if case_sensitive else line.lower()
                        idx = hay.find(needle)
                        if idx != -1:
                            snippet = line
                            if len(snippet) > max_chars_per_match:
                                snippet = snippet[: max_chars_per_match - 1] + "…"
                            matches.append(
                                {
                                    "path": rel_path,
                                    "line": line_no,
                                    "col": idx + 1,
                                    "match": query,
                                    "text": snippet,
                                }
                            )
                            if len(matches) >= max_results:
                                truncated = True
                                break
                    if truncated:
                        break
                if truncated:
                    break
            if truncated:
                break

        return {
            "ok": True,
            "query": query,
            "regex": regex,
            "case_sensitive": case_sensitive,
            "base": str(Path(rel_base)),
            "files_scanned": files_scanned,
            "truncated": truncated,
            "matches": matches,
        }


@dataclass(frozen=True, slots=True)
class ShellRunTool:
    name: str = "shell__run"
    description: str = (
        "Run a shell command. This is high-risk and MUST be approved before execution. "
        "Command runs non-interactively and returns bounded stdout/stderr."
    )
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run."},
                "cwd": {"type": "string", "description": "Relative working directory (default '.')."},
                "timeout_s": {"type": "number", "minimum": 0, "description": "Command timeout seconds (default 30)."},
                "max_output_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum characters to return for each of stdout/stderr (default 16000).",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        }
    )

    def execute(self, *, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
        command = _require_str(args, "command")
        cwd_rel = str(args.get("cwd") or ".")
        cwd_path = _resolve_in_project(project_root, cwd_rel)
        timeout_s = _maybe_float(args, "timeout_s")
        if timeout_s is None:
            timeout_s = 30.0
        max_output_chars = _maybe_int(args, "max_output_chars") or 16000

        shell = os.environ.get("SHELL")
        if not shell:
            shell = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
        shell_flag = "-lc" if os.path.basename(shell) in {"bash", "zsh"} else "-c"

        env = dict(os.environ)
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_PAGER": "cat",
                "PAGER": "cat",
                "LESS": "-FRSX",
                "GIT_EDITOR": "true",
                "EDITOR": "true",
                "PYTHONUNBUFFERED": "1",
            }
        )

        started = time.monotonic()
        proc = subprocess.Popen(
            [shell, shell_flag, command],
            cwd=str(cwd_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            start_new_session=True,
        )

        def _truncate(s: str) -> tuple[str, bool]:
            if len(s) <= max_output_chars:
                return s, False
            return s[:max_output_chars] + "…", True

        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                proc.terminate()
            try:
                stdout, stderr = proc.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    proc.kill()
                stdout, stderr = proc.communicate()

        duration_ms = int((time.monotonic() - started) * 1000)
        out, out_trunc = _truncate(stdout or "")
        err, err_trunc = _truncate(stderr or "")
        exit_code = proc.returncode

        return {
            "ok": bool(exit_code == 0 and not timed_out),
            "command": command,
            "cwd": str(Path(cwd_rel)),
            "shell": shell,
            "timed_out": timed_out,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "stdout_truncated": out_trunc,
            "stderr_truncated": err_trunc,
            "stdout": out,
            "stderr": err,
        }
