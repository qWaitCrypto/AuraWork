from __future__ import annotations

import ipaddress
import json
import shlex
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..event_bus import EventBus

from ..protocol import ArtifactRef
from ..error_codes import ErrorCode
from ..models import WorkSpec

from ..stores import ArtifactStore
from .builtins import _resolve_in_project
from .registry import ToolRegistry
from .browser_steps import parse_browser_steps


class ToolRuntimeError(RuntimeError):
    pass


def _elide_tail(s: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(s) <= max_chars:
        return s
    return s[: max(0, max_chars - 1)].rstrip() + "…"


def _diff_add_del_counts(unified_diff_text: str) -> tuple[int, int]:
    adds = 0
    dels = 0
    for line in str(unified_diff_text).splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        if line.startswith("+"):
            adds += 1
            continue
        if line.startswith("-"):
            dels += 1
            continue
    return adds, dels


def _unified_diff_changed_lines(unified_diff_text: str, *, max_lines: int = 12, max_line_chars: int = 180) -> list[str]:
    """
    Return a compact preview of changed lines with line numbers.

    We only show +/- lines (no context), and use line numbers implied by unified diff hunk headers:
      @@ -old_start,old_len +new_start,new_len @@
    """

    lines = str(unified_diff_text).splitlines()
    out: list[str] = []

    old_line_no: int | None = None
    new_line_no: int | None = None

    def _parse_hunk_header(h: str) -> tuple[int, int] | None:
        if not h.startswith("@@"):
            return None
        try:
            parts = h.split()
            old_part = next(p for p in parts if p.startswith("-"))
            new_part = next(p for p in parts if p.startswith("+"))
            old_start = int(old_part[1:].split(",")[0])
            new_start = int(new_part[1:].split(",")[0])
            return old_start, new_start
        except Exception:
            return None

    saw_any = False
    for line in lines:
        if line.startswith("--- ") or line.startswith("+++ "):
            continue
        if line.startswith("@@"):
            parsed = _parse_hunk_header(line)
            if parsed is not None:
                if saw_any and out and out[-1].strip() != "⋮":
                    out.append(f"{'':>5}  ⋮")
                old_line_no, new_line_no = parsed
                saw_any = True
            else:
                old_line_no, new_line_no = None, None
            continue

        if old_line_no is None or new_line_no is None:
            continue

        if line.startswith(" "):
            old_line_no += 1
            new_line_no += 1
            continue
        if line.startswith("-"):
            out.append(f"{old_line_no:>5} {_elide_tail(line, max_line_chars)}")
            old_line_no += 1
        elif line.startswith("+"):
            out.append(f"{new_line_no:>5} {_elide_tail(line, max_line_chars)}")
            new_line_no += 1
        else:
            continue

        if len(out) >= max_lines:
            break

    return out


def file_edit_ui_details(*, diffs: list[dict[str, Any]] | None, changed_files: list[str] | None) -> list[str] | None:
    if not isinstance(diffs, list) or not diffs:
        return None

    total_changed = len(changed_files) if isinstance(changed_files, list) else None

    out: list[str] = []
    file_headers = 0
    for item in diffs:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        diff_text = item.get("diff") or ""
        adds, dels = _diff_add_del_counts(str(diff_text))
        suffix = f"(+{adds} -{dels})"
        if item.get("truncated") is True:
            suffix += " (diff truncated)"
        moved_from = item.get("moved_from")
        if isinstance(moved_from, str) and moved_from.strip():
            out.append(f"• Edited {path.strip()} {suffix} (moved from {moved_from.strip()})")
        else:
            out.append(f"• Edited {path.strip()} {suffix}")
        file_headers += 1

        preview = _unified_diff_changed_lines(str(diff_text))
        out.extend(preview)
        out.append("")

    if out and out[-1] == "":
        out.pop()

    if total_changed is not None and total_changed > file_headers:
        out.append(f"... ({total_changed - file_headers} more file(s))")

    return out or None


def _summarize_shell_run_args(args: dict[str, Any], *, max_chars: int = 120) -> str:
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return "Run shell command"
    one_line = " ".join(command.splitlines()).strip()
    one_line = " ".join(one_line.split())
    one_line = _elide_tail(one_line, max_chars)
    return f"Run $ {one_line}"


def _browser_step_is_high_risk(step: list[str]) -> bool:
    if not step:
        return True

    cmd = str(step[0] or "").strip().lower()
    if not cmd:
        return True
    if cmd == "search":
        # `agent-browser search <query>` is a convenience wrapper around normal browsing.
        return False
    if cmd == "open":
        # Allow normal web browsing without approval; gate local/insecure schemes.
        url = next((t for t in step[1:] if isinstance(t, str) and "://" in t), None)
        if isinstance(url, str):
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                return True
            host = parsed.hostname
            if isinstance(host, str) and host:
                host_l = host.lower()
                if host_l in {"localhost"} or host_l.endswith(".local"):
                    return True
                try:
                    ip = ipaddress.ip_address(host_l.strip("[]"))
                    if ip.is_loopback or ip.is_private or ip.is_link_local:
                        return True
                except ValueError:
                    pass
        return False

    if cmd == "tab":
        # Most tab ops are safe. If a URL is provided (e.g. `tab new <url>`), apply the same
        # safety rules as `open` so tab creation can't bypass local/insecure gating.
        url = next((t for t in step[1:] if isinstance(t, str) and "://" in t), None)
        if isinstance(url, str):
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                return True
            host = parsed.hostname
            if isinstance(host, str) and host:
                host_l = host.lower()
                if host_l in {"localhost"} or host_l.endswith(".local"):
                    return True
                try:
                    ip = ipaddress.ip_address(host_l.strip("[]"))
                    if ip.is_loopback or ip.is_private or ip.is_link_local:
                        return True
                except ValueError:
                    pass
        return False

    if cmd in {"back", "forward", "reload", "close", "snapshot", "get", "wait", "scroll", "scrollintoview", "hover", "is"}:
        return False

    # Lightweight interactions (still considered low-risk browsing).
    if cmd in {"click", "dblclick", "focus", "press", "fill", "type", "select", "check", "uncheck", "keydown", "keyup"}:
        return False

    if cmd == "screenshot":
        # `agent-browser screenshot` is safe. When writing to a path, treat as higher risk.
        non_flags = [a for a in step[1:] if isinstance(a, str) and a and not a.startswith("-")]
        return len(non_flags) > 0

    if cmd == "find":
        # `find` can embed an action word; only gate operations that are clearly higher-risk.
        if any(str(tok).lower() == "upload" for tok in step[1:] if isinstance(tok, str)):
            return True
        return False

    if cmd in {"tab", "window", "frame", "mouse"}:
        return False

    if cmd in {"pdf", "record", "upload", "eval", "drag"}:
        return True

    if cmd == "cookies":
        return True

    if cmd == "storage":
        return True

    if cmd == "network":
        return True

    if cmd == "set":
        if len(step) >= 2 and str(step[1] or "").strip().lower() in {"headers", "credentials"}:
            return True
        return False

    if cmd == "dialog":
        return True

    # Default: unknown browser operation, require approval.
    return True


class InspectionDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ToolApprovalMode(StrEnum):
    """
    Tool approval policy for a session.

    - strict: require approval for every tool call (including reads/search).
    - standard: require approval only for high-risk tool calls (default).
    - trusted: never require approval (dangerous).
    """

    STRICT = "strict"
    STANDARD = "standard"
    TRUSTED = "trusted"


@dataclass(frozen=True, slots=True)
class InspectionResult:
    decision: InspectionDecision
    action_summary: str
    risk_level: str | None = None
    reason: str | None = None
    error_code: ErrorCode | None = None
    diff_ref: ArtifactRef | None = None


@dataclass(frozen=True, slots=True)
class PlannedToolCall:
    tool_execution_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    arguments_ref: ArtifactRef


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """
    Optional execution context passed to tools.

    Most tools ignore this. It's used by complex tools (e.g. subagents) that
    need to emit progress events and attach diagnostics to the current request.
    """

    session_id: str
    request_id: str | None
    turn_id: str | None
    tool_execution_id: str
    event_bus: EventBus | None = None


class ToolRuntime:
    def __init__(
        self,
        *,
        project_root: Path,
        registry: ToolRegistry,
        artifact_store: ArtifactStore,
        approval_mode: ToolApprovalMode = ToolApprovalMode.STANDARD,
    ) -> None:
        self._project_root = project_root.expanduser().resolve()
        self._registry = registry
        self._artifact_store = artifact_store
        self._approval_mode = approval_mode
        self._work_spec_var: ContextVar[WorkSpec | None] = ContextVar("aura_work_spec", default=None)

    def set_approval_mode(self, mode: ToolApprovalMode) -> None:
        self._approval_mode = mode

    def get_approval_mode(self) -> ToolApprovalMode:
        return self._approval_mode

    @property
    def project_root(self) -> Path:
        return self._project_root

    @property
    def artifact_store(self) -> ArtifactStore:
        return self._artifact_store

    def get_tool(self, tool_name: str):
        return self._registry.get(tool_name)

    def get_work_spec(self) -> WorkSpec | None:
        return self._work_spec_var.get()

    @contextmanager
    def work_spec_context(self, work_spec: WorkSpec | None):
        token: Token[WorkSpec | None] = self._work_spec_var.set(work_spec)
        try:
            yield
        finally:
            self._work_spec_var.reset(token)

    def plan(self, *, tool_execution_id: str, tool_name: str, tool_call_id: str, arguments: dict[str, Any]) -> PlannedToolCall:
        if not tool_call_id:
            raise ToolRuntimeError("Tool call is missing tool_call_id; cannot return tool_result.")
        if not tool_name:
            raise ToolRuntimeError("Tool call is missing tool name.")
        if not isinstance(arguments, dict):
            raise ToolRuntimeError("Tool call arguments must be an object.")

        args_ref = self._artifact_store.put(
            json.dumps(arguments, ensure_ascii=False, sort_keys=True, indent=2),
            kind="tool_args",
            meta={"summary": f"{tool_name} args"},
        )
        return PlannedToolCall(
            tool_execution_id=tool_execution_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            arguments_ref=args_ref,
        )

    def _inspect_work_spec_scope(self, planned: PlannedToolCall) -> InspectionResult | None:
        work_spec = self._work_spec_var.get()
        if work_spec is None:
            return None

        scope = work_spec.resource_scope
        roots_raw = scope.workspace_roots or []
        domains_raw = scope.domain_allowlist or []
        file_types_raw = scope.file_type_allowlist or []

        allowed_roots: list[Path] = []
        for root in roots_raw:
            if not isinstance(root, str) or not root.strip():
                continue
            try:
                allowed_roots.append(_resolve_in_project(self._project_root, root.strip()))
            except Exception:
                return InspectionResult(
                    decision=InspectionDecision.DENY,
                    action_summary="Invalid WorkSpec workspace_roots entry",
                    risk_level="high",
                    reason=f"Invalid workspace root: {root!r}",
                    error_code=ErrorCode.BAD_REQUEST,
                    diff_ref=None,
                )

        allowed_file_suffixes: set[str] = set()
        for ft in file_types_raw:
            if not isinstance(ft, str):
                continue
            norm = ft.strip().lower()
            if not norm:
                continue
            if not norm.startswith("."):
                norm = "." + norm
            allowed_file_suffixes.add(norm)

        def _work_spec_requires_skill_plan_json() -> bool:
            # Doc/PDF/XLSX/PPTX workflows rely on an intermediate plan.json to drive the skill runner.
            # Treat that plan file as an allowed intermediate output even when WorkSpec restricts the
            # final deliverable file types (e.g. file_type_allowlist=["docx"]).
            outs = work_spec.expected_outputs or []
            for out in outs:
                fmt = str(getattr(out, "format", "") or "").strip().lower().lstrip(".")
                if fmt in {"docx", "pdf", "xlsx", "pptx"}:
                    return True
            return False

        def _domain_allowed(host: str) -> bool:
            host_l = host.lower().strip(".")
            for entry in domains_raw:
                if not isinstance(entry, str):
                    continue
                entry_l = entry.lower().strip().strip(".")
                if not entry_l:
                    continue
                if entry_l == "*":
                    return True
                if entry_l.startswith("*.") and len(entry_l) > 2:
                    base = entry_l[2:]
                    if host_l == base or host_l.endswith("." + base):
                        return True
                if host_l == entry_l or host_l.endswith("." + entry_l):
                    return True
            return False

        if planned.tool_name == "browser__run" and domains_raw:
            try:
                steps = parse_browser_steps(planned.arguments.get("steps"))
            except Exception:
                return None
            for step in steps:
                if not step:
                    continue
                if step[0] != "open":
                    continue
                url = next((t for t in step[1:] if isinstance(t, str) and "://" in t), None)
                if not isinstance(url, str) or not url:
                    continue
                parsed = urlparse(url)
                if parsed.scheme not in {"http", "https"}:
                    continue
                host = parsed.hostname
                if isinstance(host, str) and host and not _domain_allowed(host):
                    diff_ref = self._build_args_preview(planned, summary="Preview for blocked browser__run (WorkSpec)")
                    return InspectionResult(
                        decision=InspectionDecision.REQUIRE_APPROVAL,
                        action_summary="Blocked browser automation outside domain allowlist",
                        risk_level="high",
                        reason=f"WorkSpec scope violation: domain not in allowlist: {host}",
                        error_code=ErrorCode.PERMISSION,
                        diff_ref=diff_ref,
                    )

        if not allowed_roots:
            return None

        def _path_allowed(rel: str) -> bool:
            try:
                candidate = _resolve_in_project(self._project_root, rel)
            except Exception:
                return False
            for root in allowed_roots:
                if candidate == root or root in candidate.parents:
                    return True
            return False

        def _file_type_allowed(rel: str) -> bool:
            if not allowed_file_suffixes:
                return True
            suffix = Path(rel).suffix.lower()
            if not suffix:
                return False
            if suffix == ".json" and Path(rel).name.lower() == "plan.json" and _work_spec_requires_skill_plan_json():
                return True
            return suffix in allowed_file_suffixes

        target_paths: list[str] = []
        file_paths: list[str] = []

        if planned.tool_name in {"project__read_text", "project__text_stats"}:
            p = planned.arguments.get("path")
            if isinstance(p, str) and p.strip():
                target_paths.append(p.strip())
                file_paths.append(p.strip())

        elif planned.tool_name == "project__read_text_many":
            ps = planned.arguments.get("paths")
            if isinstance(ps, list):
                for item in ps:
                    if isinstance(item, str) and item.strip():
                        target_paths.append(item.strip())
                        file_paths.append(item.strip())

        elif planned.tool_name in {"project__list_dir", "project__search_text"}:
            p = planned.arguments.get("path")
            if p is None:
                target_paths.append(".")
            elif isinstance(p, str) and p.strip():
                target_paths.append(p.strip())

        elif planned.tool_name == "project__glob":
            base = planned.arguments.get("base")
            if base is None:
                target_paths.append(".")
            elif isinstance(base, str) and base.strip():
                target_paths.append(base.strip())

        elif planned.tool_name in {"project__apply_edits", "project__apply_patch", "project__patch"}:
            try:
                if planned.tool_name == "project__apply_edits":
                    from .apply_edits_tool import list_apply_edits_target_paths

                    file_paths = list_apply_edits_target_paths(planned.arguments)
                elif planned.tool_name == "project__apply_patch":
                    patch_text = planned.arguments.get("patch")
                    if isinstance(patch_text, str) and patch_text.strip():
                        from .apply_patch_tool import list_patch_target_paths

                        file_paths = list_patch_target_paths(patch_text)
                else:
                    diff_text = planned.arguments.get("diff")
                    if isinstance(diff_text, str) and diff_text.strip():
                        from .patch_tool import unified_diff_target_paths

                        file_paths = unified_diff_target_paths(diff_text)
                target_paths.extend(file_paths)
            except Exception:
                return None

        for rel in target_paths:
            if not _path_allowed(rel):
                diff_ref = self._build_args_preview(planned, summary="Preview for blocked path (WorkSpec)")
                return InspectionResult(
                    decision=InspectionDecision.REQUIRE_APPROVAL,
                    action_summary="Blocked path outside workspace scope",
                    risk_level="high",
                    reason=f"WorkSpec scope violation: path not in workspace_roots: {rel}",
                    error_code=ErrorCode.PERMISSION,
                    diff_ref=diff_ref,
                )

        for rel in file_paths:
            if not _file_type_allowed(rel):
                diff_ref = self._build_args_preview(planned, summary="Preview for blocked file type (WorkSpec)")
                return InspectionResult(
                    decision=InspectionDecision.REQUIRE_APPROVAL,
                    action_summary="Blocked file type outside allowlist",
                    risk_level="high",
                    reason=f"WorkSpec scope violation: file type not in allowlist: {rel}",
                    error_code=ErrorCode.PERMISSION,
                    diff_ref=diff_ref,
                )

        return None

    def inspect(self, planned: PlannedToolCall) -> InspectionResult:
        tool = self._registry.get(planned.tool_name)
        if tool is None:
            return InspectionResult(
                decision=InspectionDecision.DENY,
                action_summary=f"Unknown tool: {planned.tool_name}",
                risk_level="high",
                reason="Tool is not registered.",
                error_code=ErrorCode.TOOL_UNKNOWN,
            )

        work_spec_violation = self._inspect_work_spec_scope(planned)
        if work_spec_violation is not None:
            return work_spec_violation

        # Domain invariants / mandatory approvals (override all modes).
        if planned.tool_name == "snapshot__rollback":
            target = planned.arguments.get("target")
            create_backup = planned.arguments.get("create_backup")
            backup_label = planned.arguments.get("backup_label")
            preview = (
                "Rollback project files to an internal snapshot ref.\n\n"
                f"Target: {target}\n"
                f"Create backup: {create_backup}\n"
                f"Backup label: {backup_label}\n\n"
                "WARNING: This operation overwrites files in the project working tree.\n"
                "Approval is required.\n"
            )
            diff_ref = self._artifact_store.put(preview, kind="diff", meta={"summary": "Snapshot rollback preview"})
            return InspectionResult(
                decision=InspectionDecision.REQUIRE_APPROVAL,
                action_summary=f"Rollback snapshot: {target}",
                risk_level="high",
                reason="Rolling back overwrites project files and may discard uncommitted changes.",
                error_code=None,
                diff_ref=diff_ref,
            )
        sealed = self._is_spec_sealed()
        sealed_violation = self._check_sealed_spec_violation(planned, sealed=sealed)
        if sealed_violation is not None:
            return sealed_violation

        if planned.tool_name in {"spec__apply", "spec__seal"}:
            return self._inspect_spec_workflow(planned)

        if planned.tool_name == "browser__run":
            return self._inspect_browser_run(planned)

        if planned.tool_name == "shell__run" and _shell_run_is_allowlisted(self._project_root, planned.arguments):
            summary = _summarize_shell_run_args(planned.arguments)
            return InspectionResult(
                decision=InspectionDecision.ALLOW,
                action_summary=f"{summary} (allowlisted)",
                risk_level="high",
                reason="Matched local allowlist.",
                error_code=None,
                diff_ref=None,
            )

        if self._approval_mode is ToolApprovalMode.TRUSTED:
            return InspectionResult(
                decision=InspectionDecision.ALLOW,
                action_summary=f"Execute tool: {planned.tool_name}",
                risk_level="high",
                reason="Approval mode is trusted (auto-allow).",
                error_code=None,
                diff_ref=None,
            )

        if self._approval_mode is ToolApprovalMode.STRICT:
            return self._inspect_strict(planned)

        tool_name = planned.tool_name

        if tool_name == "session__export":
            diff_ref = self._build_args_preview(planned, summary="Preview for session__export (bundle output)")
            return InspectionResult(
                decision=InspectionDecision.REQUIRE_APPROVAL,
                action_summary=f"Export session: {planned.arguments.get('session_id')}",
                risk_level="high",
                reason="Export writes files into the project; approval required in standard mode.",
                error_code=None,
                diff_ref=diff_ref,
            )

        if planned.tool_name == "shell__run":
            summary = _summarize_shell_run_args(planned.arguments)
            try:
                diff_ref = self._build_shell_run_preview(planned)
            except Exception as e:
                code = _classify_tool_exception(e)
                return InspectionResult(
                    decision=InspectionDecision.DENY,
                    action_summary="Invalid shell command request.",
                    risk_level="high",
                    reason=str(e),
                    error_code=code,
                    diff_ref=None,
                )
            return InspectionResult(
                decision=InspectionDecision.REQUIRE_APPROVAL,
                action_summary=summary,
                risk_level="high",
                reason="Shell commands can modify files and system state.",
                error_code=None,
                diff_ref=diff_ref,
            )

        if planned.tool_name in {"project__apply_patch", "project__apply_edits", "project__patch"}:
            try:
                if planned.tool_name == "project__apply_patch":
                    diff_ref = self._build_apply_patch_preview(planned)
                    action = "Apply patch"
                elif planned.tool_name == "project__patch":
                    diff_ref = self._build_project_patch_preview(planned)
                    action = "Apply patch"
                else:
                    diff_ref = self._build_apply_edits_preview(planned)
                    action = "Apply edits"
            except Exception as e:
                code = _classify_tool_exception(e)
                return InspectionResult(
                    decision=InspectionDecision.DENY,
                    action_summary="Invalid file edit request.",
                    risk_level="high",
                    reason=str(e),
                    error_code=code,
                    diff_ref=None,
                )
            return InspectionResult(
                decision=InspectionDecision.REQUIRE_APPROVAL,
                action_summary=action,
                risk_level="high",
                reason="This tool modifies project files; approval required in standard mode.",
                error_code=None,
                diff_ref=diff_ref,
            )

        return InspectionResult(
            decision=InspectionDecision.ALLOW,
            action_summary=f"Execute tool: {planned.tool_name}",
            risk_level="low",
            reason=None,
            error_code=None,
            diff_ref=None,
        )

    def _build_browser_run_preview(self, planned: PlannedToolCall) -> ArtifactRef:
        steps = parse_browser_steps(planned.arguments.get("steps"))
        lines: list[str] = ["browser__run", ""]
        for step in steps:
            rendered = " ".join(shlex.quote(s) for s in ["agent-browser", *step])
            lines.append(f"$ {rendered}")
        lines.append("")
        preview = "\n".join(lines).rstrip() + "\n"
        return self._artifact_store.put(preview, kind="diff", meta={"summary": "Browser command preview"})

    def _inspect_browser_run(self, planned: PlannedToolCall) -> InspectionResult:
        try:
            steps = parse_browser_steps(planned.arguments.get("steps"))
        except Exception as e:
            return InspectionResult(
                decision=InspectionDecision.DENY,
                action_summary="Invalid browser command request.",
                risk_level="high",
                reason=str(e),
                error_code=ErrorCode.BAD_REQUEST,
                diff_ref=None,
            )

        require_approval = any(_browser_step_is_high_risk(step) for step in steps)
        if require_approval:
            diff_ref = self._build_browser_run_preview(planned)
            return InspectionResult(
                decision=InspectionDecision.REQUIRE_APPROVAL,
                action_summary="Run browser automation",
                risk_level="high",
                reason="Browser command may change remote or local state.",
                error_code=None,
                diff_ref=diff_ref,
            )

        cmds = ", ".join(step[0] for step in steps if step)
        return InspectionResult(
            decision=InspectionDecision.ALLOW,
            action_summary=f"Run browser automation: {cmds}",
            risk_level="low",
            reason=None,
            error_code=None,
            diff_ref=None,
        )

    def _is_spec_sealed(self) -> bool:
        import json

        path = self._project_root / ".aura" / "state" / "spec_state.json"
        if not path.exists():
            return False
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(raw, dict):
            return False
        return str(raw.get("status") or "") == "sealed"

    def _check_sealed_spec_violation(self, planned: PlannedToolCall, *, sealed: bool) -> InspectionResult | None:
        if not sealed:
            return None
        tool_name = planned.tool_name
        if tool_name in {"project__apply_patch", "project__apply_edits", "project__patch"}:
            try:
                if tool_name == "project__apply_patch":
                    patch_text = planned.arguments.get("patch")
                    if isinstance(patch_text, str) and patch_text.strip():
                        from .apply_patch_tool import list_patch_target_paths

                        targets = list_patch_target_paths(patch_text)
                    else:
                        targets = []
                elif tool_name == "project__patch":
                    diff_text = planned.arguments.get("diff")
                    if isinstance(diff_text, str) and diff_text.strip():
                        from .patch_tool import unified_diff_target_paths

                        targets = unified_diff_target_paths(diff_text)
                    else:
                        targets = []
                else:
                    from .apply_edits_tool import list_apply_edits_target_paths

                    targets = list_apply_edits_target_paths(planned.arguments)

                for p in targets:
                    if _is_under_spec_dir(p):
                        return InspectionResult(
                            decision=InspectionDecision.DENY,
                            action_summary="Blocked edit touching sealed spec/",
                            risk_level="high",
                            reason="Spec is sealed; do not modify spec/ via generic file tools. Use spec workflow tools.",
                            error_code=ErrorCode.PERMISSION,
                            diff_ref=None,
                        )
            except Exception:
                # If args are invalid we let the tool fail later with a clearer error.
                pass
        return None

    def _inspect_spec_workflow(self, planned: PlannedToolCall) -> InspectionResult:
        tool_name = planned.tool_name
        if tool_name == "spec__apply":
            proposal_id = planned.arguments.get("proposal_id")
            diff_ref = None
            reason = None
            if isinstance(proposal_id, str) and proposal_id:
                try:
                    record = _load_spec_proposal_record(self._project_root, proposal_id)
                    reason_raw = record.get("reason")
                    if isinstance(reason_raw, str) and reason_raw.strip():
                        reason = reason_raw.strip()
                    raw_ref = record.get("diff_ref")
                    if isinstance(raw_ref, dict):
                        diff_ref = ArtifactRef.from_dict(raw_ref)
                except Exception:
                    diff_ref = self._build_args_preview(planned, summary="Preview for spec__apply (proposal diff unavailable)")
            else:
                diff_ref = self._build_args_preview(planned, summary="Preview for spec__apply (missing proposal_id)")
            return InspectionResult(
                decision=InspectionDecision.REQUIRE_APPROVAL,
                action_summary=f"Apply spec proposal: {proposal_id}",
                risk_level="high",
                reason=reason or "Applying a spec proposal modifies author-visible spec/ files.",
                error_code=None,
                diff_ref=diff_ref,
            )

        if tool_name == "spec__seal":
            label = planned.arguments.get("label")
            preview = (
                "Seal spec into a version label.\n\n"
                "This will create an internal snapshot (git) and mark spec as sealed.\n"
                "Default snapshot exclusions:\n"
                "- .aura/state/git\n"
                "- .aura/cache\n"
                "- .aura/index\n"
                "- .aura/tmp\n"
                "- .aura/events\n"
                "- .aura/sessions\n"
                "- .aura/artifacts\n"
            )
            if isinstance(label, str) and label.strip():
                preview = f"Label: {label.strip()}\n\n" + preview
            diff_ref = self._artifact_store.put(preview, kind="diff", meta={"summary": "Spec seal preview"})
            return InspectionResult(
                decision=InspectionDecision.REQUIRE_APPROVAL,
                action_summary=f"Seal spec: {label}",
                risk_level="high",
                reason="Sealing spec creates a version point and makes spec read-only.",
                error_code=None,
                diff_ref=diff_ref,
            )

        return InspectionResult(
            decision=InspectionDecision.DENY,
            action_summary=f"Unknown spec workflow tool: {tool_name}",
            risk_level="high",
            reason="Unsupported spec workflow operation.",
            error_code=ErrorCode.TOOL_UNKNOWN,
            diff_ref=None,
        )

    def _build_shell_run_preview(self, planned: PlannedToolCall) -> ArtifactRef:
        command = planned.arguments.get("command")
        cwd = planned.arguments.get("cwd") or "."
        timeout_s = planned.arguments.get("timeout_s")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("shell__run: missing command")
        preview = f"$ {command}\n(cwd: {cwd})\n(timeout_s: {timeout_s})"
        return self._artifact_store.put(
            preview,
            kind="diff",
            meta={"summary": "Shell command preview"},
        )

    def _format_file_edit_preview(
        self, *, title: str, changed_files: list[str] | None, diffs: list[dict[str, Any]] | None
    ) -> str:
        lines: list[str] = [title, ""]
        details = file_edit_ui_details(diffs=diffs, changed_files=changed_files)
        if not details:
            lines.append("(no diff preview available)")
            return "\n".join(lines).rstrip() + "\n"
        lines.extend(details)
        return "\n".join(lines).rstrip() + "\n"

    def _build_apply_patch_preview(self, planned: PlannedToolCall) -> ArtifactRef:
        patch_text = planned.arguments.get("patch")
        if not isinstance(patch_text, str) or not patch_text.strip():
            raise ValueError("project__apply_patch: missing patch")
        from .apply_patch_tool import ProjectApplyPatchTool

        args: dict[str, Any] = {"patch": patch_text, "dry_run": True}
        max_diff_chars = planned.arguments.get("max_diff_chars")
        max_diffs = planned.arguments.get("max_diffs")
        if isinstance(max_diff_chars, int) and max_diff_chars >= 1:
            args["max_diff_chars"] = max_diff_chars
        if isinstance(max_diffs, int) and max_diffs >= 1:
            args["max_diffs"] = max_diffs

        tool = ProjectApplyPatchTool()
        result = tool.execute(args=args, project_root=self._project_root)
        preview = self._format_file_edit_preview(
            title="project__apply_patch (dry-run preview)",
            changed_files=result.get("changed_files") if isinstance(result, dict) else None,
            diffs=result.get("diffs") if isinstance(result, dict) else None,
        )
        preview = _elide_tail(preview, 100_000)
        return self._artifact_store.put(preview, kind="diff", meta={"summary": "Patch dry-run diff preview"})

    def _build_apply_edits_preview(self, planned: PlannedToolCall) -> ArtifactRef:
        from .apply_edits_tool import ProjectApplyEditsTool

        args: dict[str, Any] = dict(planned.arguments)
        args["dry_run"] = True

        tool = ProjectApplyEditsTool()
        result = tool.execute(args=args, project_root=self._project_root)
        preview = self._format_file_edit_preview(
            title="project__apply_edits (dry-run preview)",
            changed_files=result.get("changed_files") if isinstance(result, dict) else None,
            diffs=result.get("diffs") if isinstance(result, dict) else None,
        )
        preview = _elide_tail(preview, 100_000)
        return self._artifact_store.put(preview, kind="diff", meta={"summary": "Edits dry-run diff preview"})

    def _build_project_patch_preview(self, planned: PlannedToolCall) -> ArtifactRef:
        diff_text = planned.arguments.get("diff")
        if not isinstance(diff_text, str) or not diff_text.strip():
            raise ValueError("project__patch: missing diff")
        from .patch_tool import ProjectPatchTool

        args: dict[str, Any] = {"diff": diff_text, "dry_run": True}
        max_diff_chars = planned.arguments.get("max_diff_chars")
        max_diffs = planned.arguments.get("max_diffs")
        if isinstance(max_diff_chars, int) and max_diff_chars >= 1:
            args["max_diff_chars"] = max_diff_chars
        if isinstance(max_diffs, int) and max_diffs >= 1:
            args["max_diffs"] = max_diffs

        tool = ProjectPatchTool()
        result = tool.execute(args=args, project_root=self._project_root)
        preview = self._format_file_edit_preview(
            title="project__patch (dry-run preview)",
            changed_files=result.get("changed_files") if isinstance(result, dict) else None,
            diffs=result.get("diffs") if isinstance(result, dict) else None,
        )
        preview = _elide_tail(preview, 100_000)
        return self._artifact_store.put(preview, kind="diff", meta={"summary": "Patch dry-run diff preview"})

    def _inspect_strict(self, planned: PlannedToolCall) -> InspectionResult:
        tool_name = planned.tool_name

        # High-risk tools: try to produce a meaningful diff/preview.
        if tool_name == "project__apply_patch":
            try:
                diff_ref = self._build_apply_patch_preview(planned)
            except Exception as e:
                code = _classify_tool_exception(e)
                return InspectionResult(
                    decision=InspectionDecision.DENY,
                    action_summary="Invalid patch request.",
                    risk_level="high",
                    reason=str(e),
                    error_code=code,
                    diff_ref=None,
                )
            return InspectionResult(
                decision=InspectionDecision.REQUIRE_APPROVAL,
                action_summary="Apply patch",
                risk_level="high",
                reason="Strict mode: approve every tool call.",
                error_code=None,
                diff_ref=diff_ref,
            )

        if tool_name == "project__patch":
            try:
                diff_ref = self._build_project_patch_preview(planned)
            except Exception as e:
                code = _classify_tool_exception(e)
                return InspectionResult(
                    decision=InspectionDecision.DENY,
                    action_summary="Invalid patch request.",
                    risk_level="high",
                    reason=str(e),
                    error_code=code,
                    diff_ref=None,
                )
            return InspectionResult(
                decision=InspectionDecision.REQUIRE_APPROVAL,
                action_summary="Apply patch",
                risk_level="high",
                reason="Strict mode: approve every tool call.",
                error_code=None,
                diff_ref=diff_ref,
            )

        if tool_name == "project__apply_edits":
            try:
                diff_ref = self._build_apply_edits_preview(planned)
            except Exception as e:
                code = _classify_tool_exception(e)
                return InspectionResult(
                    decision=InspectionDecision.DENY,
                    action_summary="Invalid edits request.",
                    risk_level="high",
                    reason=str(e),
                    error_code=code,
                    diff_ref=None,
                )
            return InspectionResult(
                decision=InspectionDecision.REQUIRE_APPROVAL,
                action_summary="Apply edits",
                risk_level="high",
                reason="Strict mode: approve every tool call.",
                error_code=None,
                diff_ref=diff_ref,
            )

        if tool_name == "shell__run":
            summary = _summarize_shell_run_args(planned.arguments)
            try:
                diff_ref = self._build_shell_run_preview(planned)
            except Exception as e:
                code = _classify_tool_exception(e)
                return InspectionResult(
                    decision=InspectionDecision.DENY,
                    action_summary="Invalid shell command request.",
                    risk_level="high",
                    reason=str(e),
                    error_code=code,
                    diff_ref=None,
                )
            return InspectionResult(
                decision=InspectionDecision.REQUIRE_APPROVAL,
                action_summary=summary,
                risk_level="high",
                reason="Strict mode: approve every tool call.",
                error_code=None,
                diff_ref=diff_ref,
            )

        # Low-risk tools: require approval but only preview args (do not read file contents).
        diff_ref = self._build_args_preview(planned, summary=f"Preview for {tool_name}")
        return InspectionResult(
            decision=InspectionDecision.REQUIRE_APPROVAL,
            action_summary=f"Execute tool: {tool_name}",
            risk_level="low",
            reason="Strict mode: approve every tool call.",
            error_code=None,
            diff_ref=diff_ref,
        )

    def _build_args_preview(self, planned: PlannedToolCall, *, summary: str) -> ArtifactRef:
        text = json.dumps(planned.arguments, ensure_ascii=False, sort_keys=True, indent=2)
        return self._artifact_store.put(text, kind="diff", meta={"summary": summary})


def _classify_tool_exception(exc: BaseException) -> ErrorCode:
    if isinstance(exc, ToolRuntimeError):
        msg = str(exc).lower()
        if "unknown tool" in msg:
            return ErrorCode.TOOL_UNKNOWN
        return ErrorCode.TOOL_FAILED
    if isinstance(exc, PermissionError):
        return ErrorCode.PERMISSION
    if isinstance(exc, FileNotFoundError):
        return ErrorCode.NOT_FOUND
    if isinstance(exc, TimeoutError):
        return ErrorCode.TIMEOUT
    if isinstance(exc, (ValueError, json.JSONDecodeError)):
        return ErrorCode.BAD_REQUEST
    if isinstance(exc, OSError):
        return ErrorCode.UNKNOWN
    return ErrorCode.UNKNOWN


_TOOL_END_STATUS_MAP: dict[str, str] = {
    "ok": "succeeded",
    "success": "succeeded",
    "succeeded": "succeeded",
    "completed": "succeeded",
    "done": "succeeded",
    "error": "failed",
    "failed": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "denied": "blocked",
    "blocked": "blocked",
    "needs_approval": "needs_approval",
    "require_approval": "needs_approval",
    "requires_approval": "needs_approval",
    "pending_approval": "needs_approval",
    "running": "running",
}


def _normalize_tool_end_status(status: str | None) -> str:
    raw = str(status or "").strip().lower()
    if not raw:
        return "unknown"
    return _TOOL_END_STATUS_MAP.get(raw, "unknown")


def _status_from_error_code(*, error_code: str | None, fallback: str = "failed") -> str:
    code = str(error_code or "").strip().lower()
    if code == ErrorCode.PERMISSION.value:
        return "blocked"
    if code == ErrorCode.APPROVAL_PENDING.value:
        return "needs_approval"
    return _normalize_tool_end_status(fallback)


def _tool_result_needs_approval(raw: dict[str, Any]) -> bool:
    status = raw.get("status")
    if _normalize_tool_end_status(str(status) if isinstance(status, str) else None) == "needs_approval":
        return True
    blocked = raw.get("blocked_approval")
    if isinstance(blocked, (dict, list)) and blocked:
        return True
    needs = raw.get("needs_approval")
    if isinstance(needs, list) and any(isinstance(x, dict) for x in needs):
        return True
    node_results = raw.get("node_results")
    if isinstance(node_results, dict):
        for v in node_results.values():
            if not isinstance(v, dict):
                continue
            if _normalize_tool_end_status(str(v.get("status") or "")) == "needs_approval":
                return True
    return False


def _classify_tool_result(*, tool_name: str, raw: Any) -> tuple[str, bool, str | None, str | None]:
    """
    Map a tool's returned value to a normalized (status, ok, error_code, error) tuple.

    Tool implementations can either:
    - raise exceptions (hard failure), or
    - return a dict with an `ok: bool` field (soft failure).

    We treat `ok=False` as non-success and distinguish approval-blocked vs failed.
    """

    if not isinstance(raw, dict):
        return ("succeeded", True, None, None)

    ok_val = raw.get("ok")
    if isinstance(ok_val, bool) and ok_val is True:
        return ("succeeded", True, None, None)

    if isinstance(ok_val, bool) and ok_val is False:
        if _tool_result_needs_approval(raw):
            msg = raw.get("message") or raw.get("error") or "Approval required."
            msg_s = str(msg) if msg is not None else "Approval required."
            return ("needs_approval", False, ErrorCode.APPROVAL_PENDING.value, _elide_tail(msg_s, 400))

        # Best-effort error code + message extraction for soft failures.
        error_code = raw.get("error_code")
        if isinstance(error_code, str) and error_code.strip():
            code = error_code.strip()
        else:
            code = ErrorCode.TOOL_FAILED.value

        msg = raw.get("error") or raw.get("message")
        msg_s: str | None = None
        if isinstance(msg, str) and msg.strip():
            msg_s = msg.strip()

        # Tool-specific heuristics
        if tool_name == "browser__run":
            steps = raw.get("steps")
            if isinstance(steps, list):
                for step in steps:
                    if not isinstance(step, dict):
                        continue
                    if step.get("timed_out") is True:
                        code = ErrorCode.TIMEOUT.value
                        argv = step.get("argv")
                        argv_s = " ".join([str(x) for x in argv]) if isinstance(argv, list) else None
                        msg_s = msg_s or f"browser__run timed out ({argv_s or 'agent-browser'})"
                        break
                    exit_code = step.get("exit_code")
                    if isinstance(exit_code, int) and exit_code != 0:
                        stderr = step.get("stderr")
                        if isinstance(stderr, str) and stderr.strip():
                            msg_s = msg_s or stderr.strip()
                        msg_s = msg_s or f"browser__run failed (exit_code={exit_code})"
                        break

        if msg_s is None:
            msg_s = f"{tool_name} returned ok=false"
        msg_s = _elide_tail(msg_s, 400)

        # Optional: map common messages to more specific codes.
        msg_l = msg_s.lower()
        if code == ErrorCode.TOOL_FAILED.value:
            if "invalid" in msg_l or "expected" in msg_l:
                code = ErrorCode.BAD_REQUEST.value
            elif "not found" in msg_l:
                code = ErrorCode.NOT_FOUND.value
            elif "permission" in msg_l or "denied" in msg_l:
                code = ErrorCode.PERMISSION.value
            elif "timeout" in msg_l or "timed out" in msg_l:
                code = ErrorCode.TIMEOUT.value

        status = _status_from_error_code(error_code=code, fallback="failed")
        return (status, False, code, msg_s)

    # No `ok` field: treat as success.
    return ("succeeded", True, None, None)


def _is_under_spec_dir(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("/")
    return normalized == "spec" or normalized.startswith("spec/")


def _tool_approval_policy_path(project_root: Path) -> Path:
    # Keep this in the author-visible policy dir so users can audit/edit it.
    return project_root / ".aura" / "policy" / "tool_approvals.json"


def _load_tool_approval_policy(project_root: Path) -> dict[str, Any]:
    path = _tool_approval_policy_path(project_root)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_tool_approval_policy(project_root: Path, policy: dict[str, Any]) -> None:
    path = _tool_approval_policy_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(policy, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    tmp.replace(path)


def add_shell_run_allowlist_rule(*, project_root: Path, command_prefix: str, cwd: str | None) -> None:
    command_prefix = " ".join(str(command_prefix).splitlines()).strip()
    if not command_prefix:
        return
    policy = _load_tool_approval_policy(project_root)
    rules = policy.get("shell__run_allow", [])
    if not isinstance(rules, list):
        rules = []
    entry: dict[str, Any] = {"command_prefix": command_prefix}
    if cwd is not None and str(cwd).strip():
        entry["cwd"] = str(cwd).strip()
    for existing in rules:
        if not isinstance(existing, dict):
            continue
        if existing.get("command_prefix") == entry.get("command_prefix") and existing.get("cwd") == entry.get("cwd"):
            return
    rules.append(entry)
    policy["shell__run_allow"] = rules
    _save_tool_approval_policy(project_root, policy)


def _normalize_shell_command(command: Any) -> str | None:
    if not isinstance(command, str):
        return None
    one_line = " ".join(command.strip().splitlines()).strip()
    return one_line if one_line else None


def _shell_run_is_allowlisted(project_root: Path, args: dict[str, Any]) -> bool:
    cmd = _normalize_shell_command(args.get("command"))
    if not cmd:
        return False
    cwd = args.get("cwd")
    cwd_s = str(cwd).strip() if isinstance(cwd, str) else None

    policy = _load_tool_approval_policy(project_root)
    rules = policy.get("shell__run_allow", [])
    if not isinstance(rules, list):
        return False
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        prefix = rule.get("command_prefix")
        if not isinstance(prefix, str) or not prefix.strip():
            continue
        if not cmd.startswith(prefix.strip()):
            continue
        rule_cwd = rule.get("cwd")
        if isinstance(rule_cwd, str) and rule_cwd.strip():
            if cwd_s is None or cwd_s != rule_cwd.strip():
                continue
        return True
    return False


def _load_spec_proposal_record(project_root: Path, proposal_id: str) -> dict[str, Any]:
    import json

    root = project_root / ".aura" / "state" / "spec" / "proposals"
    path = root / f"{proposal_id}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Invalid proposal record.")
    return raw
