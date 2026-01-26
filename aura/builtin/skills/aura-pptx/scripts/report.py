#!/usr/bin/env python3
"""
Assemble a report.json for aura_pptx following references/aura_report_schema.md.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


_RISK_KEYS = (
    "has_charts",
    "has_pivots",
    "has_controls",
    "has_macros",
    "has_formulas",
    "has_external_links",
    "risk_level",
)

_RISK_RANK = {"low": 1, "medium": 2, "high": 3}


def _filter_risk_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {k: value[k] for k in _RISK_KEYS if k in value}


def _max_risk_level(a: Any, b: Any) -> str:
    a_rank = _RISK_RANK.get(a, 0) if isinstance(a, str) else 0
    b_rank = _RISK_RANK.get(b, 0) if isinstance(b, str) else 0
    if a_rank >= b_rank and a_rank:
        return a
    if b_rank:
        return b
    return "low"


def build_report(
    *,
    plan: dict[str, Any],
    input_file: str,
    output_file: str,
    apply_report: dict[str, Any],
    gate_a: dict[str, Any],
    analysis: dict[str, Any] | None = None,
    analysis_artifact_path: str = "",
    apply_report_artifact_path: str = "",
    gate_a_artifact_path: str = "",
) -> dict[str, Any]:
    task_id = ""
    meta = plan.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("id"), str):
        task_id = meta["id"]

    ok_a = bool(gate_a.get("ok", False))
    overall_status = "success" if ok_a else "failed_gate_a"

    analysis_risk = _filter_risk_summary(analysis.get("risk_summary") if isinstance(analysis, dict) else None)
    apply_risk = _filter_risk_summary(apply_report.get("risk_summary"))
    risk_summary: dict[str, Any] = {
        "has_charts": False,
        "has_pivots": False,
        "has_controls": False,
        "has_macros": False,
        "has_formulas": False,
        "has_external_links": False,
        "risk_level": "low",
    }
    if analysis_risk:
        risk_summary.update({k: analysis_risk.get(k) for k in _RISK_KEYS if k != "risk_level"})
    elif apply_risk:
        risk_summary.update({k: apply_risk.get(k) for k in _RISK_KEYS if k != "risk_level"})
    risk_summary["risk_level"] = _max_risk_level(analysis_risk.get("risk_level"), apply_risk.get("risk_level"))

    artifacts: list[dict[str, Any]] = []
    if analysis_artifact_path:
        artifacts.append({"type": "analysis_report", "path": analysis_artifact_path})
    if apply_report_artifact_path:
        artifacts.append({"type": "apply_report", "path": apply_report_artifact_path})
    if gate_a_artifact_path:
        artifacts.append({"type": "validate_report", "path": gate_a_artifact_path})

    return {
        "task_id": task_id,
        "format": "pptx",
        "input_file": input_file,
        "output_file": output_file,
        "timestamp": _utc_now_iso(),
        "engine": apply_report.get("engine", ""),
        "touched_parts": apply_report.get("touched_parts", []),
        "risk_summary": risk_summary,
        "gates": {
            "gate_a": gate_a,
            "gate_b": {"required": False, "skipped": False, "ok": None, "reason": "N/A for pptx"},
        },
        "overall_status": overall_status,
        "artifacts": artifacts,
        "change_summary": apply_report.get("change_summary", ""),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Build aura_pptx report.json")
    ap.add_argument("plan_json", type=Path)
    ap.add_argument("--analysis", type=Path, default=None, help="Optional analysis.json from analyze.py")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--apply-report", type=Path, required=True)
    ap.add_argument("--gate-a", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    plan = json.loads(args.plan_json.read_text(encoding="utf-8"))
    analysis = json.loads(args.analysis.read_text(encoding="utf-8")) if args.analysis else None
    apply_report = json.loads(args.apply_report.read_text(encoding="utf-8"))
    gate_a = json.loads(args.gate_a.read_text(encoding="utf-8"))

    rep = build_report(
        plan=plan,
        input_file=args.input,
        output_file=args.output,
        apply_report=apply_report,
        gate_a=gate_a,
        analysis=analysis,
        analysis_artifact_path=str(args.analysis) if args.analysis else "",
        apply_report_artifact_path=str(args.apply_report),
        gate_a_artifact_path=str(args.gate_a),
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

