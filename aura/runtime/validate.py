from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .approval import ApprovalRecord, ApprovalStatus
from .protocol import ArtifactRef, Event, EventKind
from .stores import FileApprovalStore


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    severity: str  # "error" | "warning"
    message: str
    location: str | None = None

    def render(self) -> str:
        loc = f"{self.location}: " if self.location else ""
        return f"{self.severity}: {loc}{self.message}"


def validate_project_session(
    *,
    project_root: Path,
    session_id: str,
    strict: bool = False,
) -> list[ValidationIssue]:
    project_root = project_root.expanduser().resolve()
    system_dir = project_root / ".aura"
    events_path = system_dir / "events" / f"{session_id}.jsonl"
    artifacts_dir = system_dir / "artifacts"
    approvals_dir = system_dir / "state" / "approvals"

    issues: list[ValidationIssue] = []
    events = _load_events(events_path, issues=issues)
    approvals = _load_approvals(approvals_dir, session_id=session_id, issues=issues)

    issues.extend(_validate_events(events, strict=strict))
    issues.extend(_validate_artifacts(events, artifacts_dir=artifacts_dir))
    issues.extend(_validate_tool_call_pairs(events))
    issues.extend(_validate_approval_consistency(events, approvals=approvals, strict=strict))
    return issues


def validate_bundle_dir(*, bundle_dir: Path, strict: bool = False) -> list[ValidationIssue]:
    bundle_dir = bundle_dir.expanduser().resolve()
    issues: list[ValidationIssue] = []

    session_json = bundle_dir / "session.json"
    if session_json.exists():
        try:
            raw = json.loads(session_json.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                issues.append(ValidationIssue("error", "session.json must be a JSON object.", str(session_json)))
        except json.JSONDecodeError as e:
            issues.append(ValidationIssue("error", f"session.json is not valid JSON: {e}", str(session_json)))

    events_path = bundle_dir / "events.jsonl"
    events = _load_events(events_path, issues=issues)

    issues.extend(_validate_events(events, strict=strict))
    issues.extend(_validate_artifacts(events, artifacts_dir=bundle_dir / "artifacts"))
    issues.extend(_validate_tool_call_pairs(events))
    return issues


def _load_events(path: Path, *, issues: list[ValidationIssue]) -> list[Event]:
    if not path.exists():
        issues.append(ValidationIssue("error", "Events file not found.", str(path)))
        return []

    events: list[Event] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            raw_line = line.rstrip("\n")
            if not raw_line.strip():
                continue
            try:
                data = json.loads(raw_line)
            except json.JSONDecodeError as e:
                issues.append(ValidationIssue("error", f"Invalid JSON: {e}", f"{path}:{line_no}"))
                continue
            if not isinstance(data, dict):
                issues.append(ValidationIssue("error", "Event line must be a JSON object.", f"{path}:{line_no}"))
                continue
            try:
                events.append(Event.from_dict(data))
            except Exception as e:
                issues.append(ValidationIssue("error", f"Invalid event shape: {e}", f"{path}:{line_no}"))
                continue
    return events


def _load_approvals(
    approvals_dir: Path, *, session_id: str, issues: list[ValidationIssue]
) -> dict[str, ApprovalRecord]:
    if not approvals_dir.exists():
        issues.append(ValidationIssue("warning", "Approvals directory not found; skipping approval checks.", str(approvals_dir)))
        return {}
    store = FileApprovalStore(approvals_dir)
    out: dict[str, ApprovalRecord] = {}
    try:
        records = store.list(session_id=session_id, status=None, request_id=None)
    except Exception as e:
        issues.append(ValidationIssue("error", f"Failed to list approvals: {e}", str(approvals_dir)))
        return {}
    for rec in records:
        out[rec.approval_id] = rec
    return out


def _validate_events(events: list[Event], *, strict: bool) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    known_kinds = {k.value for k in EventKind}
    step_expected = {
        EventKind.MODEL_SELECTED.value,
        EventKind.MODEL_RESOLUTION_FAILED.value,
        EventKind.LLM_REQUEST_STARTED.value,
        EventKind.LLM_RESPONSE_DELTA.value,
        EventKind.LLM_RESPONSE_COMPLETED.value,
        EventKind.LLM_REQUEST_FAILED.value,
        EventKind.TOOL_CALL_START.value,
        EventKind.TOOL_CALL_END.value,
        EventKind.PLAN_UPDATE.value,
    }
    seen_ids: set[str] = set()
    session_ids: set[str] = set()

    for idx, ev in enumerate(events):
        session_ids.add(ev.session_id)
        if ev.event_id in seen_ids:
            issues.append(ValidationIssue("error", f"Duplicate event_id: {ev.event_id}", f"event[{idx}]"))
        seen_ids.add(ev.event_id)

        if ev.kind not in known_kinds:
            sev = "error" if strict else "warning"
            issues.append(ValidationIssue(sev, f"Unknown event kind: {ev.kind}", f"event[{idx}]"))

        if ev.schema_version is None:
            issues.append(ValidationIssue("warning", "Missing schema_version.", f"event[{idx}]"))

        if ev.kind in step_expected and ev.step_id is None:
            issues.append(ValidationIssue("warning", "Missing step_id for step-scoped event.", f"event[{idx}]"))

        if ev.kind in {
            EventKind.OPERATION_FAILED.value,
            EventKind.OPERATION_CANCELLED.value,
            EventKind.LLM_REQUEST_FAILED.value,
        }:
            if not isinstance(ev.payload.get("error_code"), str) or not ev.payload.get("error_code"):
                issues.append(ValidationIssue("error", "Missing payload.error_code for failure/cancel event.", f"event[{idx}]"))

        if ev.kind == EventKind.TOOL_CALL_END.value:
            status = ev.payload.get("status")
            if status in {"failed", "denied", "cancelled"}:
                if not isinstance(ev.payload.get("error_code"), str) or not ev.payload.get("error_code"):
                    issues.append(ValidationIssue("error", "Missing payload.error_code for tool_call_end failure.", f"event[{idx}]"))

    if len(session_ids) > 1:
        issues.append(ValidationIssue("error", f"Multiple session_id values in log: {sorted(session_ids)}", "events"))
    return issues


def _validate_artifacts(events: Iterable[Event], *, artifacts_dir: Path) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not artifacts_dir.exists():
        issues.append(ValidationIssue("error", "Artifacts directory not found.", str(artifacts_dir)))
        return issues

    root = artifacts_dir.expanduser().resolve()
    seen: set[str] = set()
    for idx, ev in enumerate(events):
        for ref in _iter_artifact_refs(ev.to_dict()):
            if ref.artifact_id in seen:
                continue
            seen.add(ref.artifact_id)
            try:
                path = (root / ref.locator).resolve()
                if path != root and root not in path.parents:
                    issues.append(
                        ValidationIssue(
                            "error",
                            f"Artifact locator escapes artifacts root: {ref.locator!r}",
                            f"event[{idx}]",
                        )
                    )
                    continue
                if not path.is_file():
                    issues.append(
                        ValidationIssue(
                            "error",
                            f"Missing artifact file for locator={ref.locator!r} (artifact_id={ref.artifact_id}).",
                            f"event[{idx}]",
                        )
                    )
            except Exception as e:
                issues.append(
                    ValidationIssue(
                        "error",
                        f"Failed to resolve artifact locator={ref.locator!r}: {e}",
                        f"event[{idx}]",
                    )
                )
    return issues


def _validate_tool_call_pairs(events: list[Event]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    starts: dict[str, int] = {}
    ended: set[str] = set()

    for idx, ev in enumerate(events):
        if ev.kind == EventKind.TOOL_CALL_START.value:
            teid = ev.payload.get("tool_execution_id")
            if not isinstance(teid, str) or not teid:
                issues.append(ValidationIssue("error", "tool_call_start missing tool_execution_id.", f"event[{idx}]"))
                continue
            if teid in starts:
                issues.append(ValidationIssue("error", f"Duplicate tool_call_start for tool_execution_id={teid}.", f"event[{idx}]"))
            starts[teid] = idx

        if ev.kind == EventKind.TOOL_CALL_END.value:
            teid = ev.payload.get("tool_execution_id")
            if not isinstance(teid, str) or not teid:
                issues.append(ValidationIssue("error", "tool_call_end missing tool_execution_id.", f"event[{idx}]"))
                continue
            if teid not in starts:
                issues.append(ValidationIssue("error", f"tool_call_end without start for tool_execution_id={teid}.", f"event[{idx}]"))
                continue
            if teid in ended:
                issues.append(ValidationIssue("error", f"Duplicate tool_call_end for tool_execution_id={teid}.", f"event[{idx}]"))
            ended.add(teid)

    for teid, start_idx in starts.items():
        if teid not in ended:
            issues.append(ValidationIssue("error", f"tool_call_start without end for tool_execution_id={teid}.", f"event[{start_idx}]"))
    return issues


def _validate_approval_consistency(
    events: list[Event],
    *,
    approvals: dict[str, ApprovalRecord],
    strict: bool,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    approval_ids_in_events: set[str] = set()

    for idx, ev in enumerate(events):
        if ev.kind not in {
            EventKind.APPROVAL_REQUIRED.value,
            EventKind.APPROVAL_GRANTED.value,
            EventKind.APPROVAL_DENIED.value,
        }:
            continue
        approval_id = ev.payload.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id:
            issues.append(ValidationIssue("error", "Approval event missing approval_id.", f"event[{idx}]"))
            continue
        approval_ids_in_events.add(approval_id)

        rec = approvals.get(approval_id)
        if rec is None:
            sev = "error" if strict else "warning"
            issues.append(ValidationIssue(sev, f"Approval referenced in events but missing from store: {approval_id}", f"event[{idx}]"))
            continue

        if ev.kind == EventKind.APPROVAL_GRANTED.value and rec.status is not ApprovalStatus.GRANTED:
            issues.append(ValidationIssue("error", f"Approval status mismatch for {approval_id}: expected granted, got {rec.status.value}", f"event[{idx}]"))
        if ev.kind == EventKind.APPROVAL_DENIED.value and rec.status is not ApprovalStatus.DENIED:
            issues.append(ValidationIssue("error", f"Approval status mismatch for {approval_id}: expected denied, got {rec.status.value}", f"event[{idx}]"))

    for approval_id, rec in approvals.items():
        if approval_id not in approval_ids_in_events:
            issues.append(ValidationIssue("error", f"Approval exists in store but is not referenced by any approval_* event: {approval_id}", "approvals"))

    return issues


def _iter_artifact_refs(value: Any) -> Iterable[ArtifactRef]:
    required = {"artifact_id", "artifact_kind", "locator", "created_at"}
    if isinstance(value, dict):
        if required <= set(value.keys()):
            try:
                yield ArtifactRef.from_dict(value)
            except Exception:
                pass
        for v in value.values():
            yield from _iter_artifact_refs(v)
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_artifact_refs(item)
        return
    return []
