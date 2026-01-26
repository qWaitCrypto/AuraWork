#!/usr/bin/env python3
"""
One-shot pipeline runner for aura_xlsx.

Runs the closed loop in one command:
  analyze -> apply -> Gate A validate -> Gate B recalc (if needed) -> report

This is intended to be the primary entrypoint for subagents.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import analyze as analyze_mod  # noqa: E402
import apply as apply_mod  # noqa: E402
import plan as plan_mod  # noqa: E402
import recalc as recalc_mod  # noqa: E402
import report as report_mod  # noqa: E402
import validate as validate_mod  # noqa: E402


def _utc_now_compact() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z").replace(":", "").replace("-", "")


def _sanitize_token(value: str, *, max_len: int = 48) -> str:
    raw = (value or "").strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    cleaned = "".join(ch if ch in allowed else "_" for ch in raw)
    cleaned = cleaned.strip("._-")
    if not cleaned:
        cleaned = "run"
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip("._-")
    return cleaned or "run"


def _alloc_run_dir(base: Path, name: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    stem = _sanitize_token(name, max_len=64)
    for i in range(0, 10_000):
        cand = base / (stem if i == 0 else f"{stem}-{i}")
        try:
            cand.mkdir(parents=True, exist_ok=False)
            return cand
        except FileExistsError:
            continue
    raise RuntimeError("Unable to allocate a unique artifacts directory.")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass(frozen=True, slots=True)
class RunPaths:
    run_dir: Path
    plan_copy: Path
    analysis: Path
    apply_report: Path
    gate_a: Path
    gate_b: Path
    report: Path


def _compute_task_id(plan: dict[str, Any]) -> str:
    meta = plan.get("meta")
    if isinstance(meta, dict):
        task_id = meta.get("id")
        if isinstance(task_id, str) and task_id.strip():
            return task_id.strip()
    return f"task_{_utc_now_compact()}"


def run_pipeline(
    *,
    in_xlsx: Path,
    plan_path: Path,
    out_xlsx: Path | None,
    artifacts_dir: Path,
    run_id: str | None,
    mode: str,
    overwrite: bool,
    max_cells: int,
    timeout_s: int,
) -> dict[str, Any]:
    plan = plan_mod.load_and_validate_plan(plan_path)

    task_id = _compute_task_id(plan)
    rid = _sanitize_token(run_id or task_id)
    run_dir = _alloc_run_dir(artifacts_dir, f"aura_xlsx_{rid}")
    out_xlsx_final = out_xlsx.resolve() if out_xlsx else (run_dir / "output.xlsx")

    paths = RunPaths(
        run_dir=run_dir,
        plan_copy=run_dir / "plan.json",
        analysis=run_dir / "analysis.json",
        apply_report=run_dir / "apply_report.json",
        gate_a=run_dir / "gate_a.json",
        gate_b=run_dir / "gate_b.json",
        report=run_dir / "report.json",
    )

    shutil.copyfile(plan_path, paths.plan_copy)

    input_exists = str(in_xlsx) != "-" and in_xlsx.exists()
    if input_exists:
        analysis = analyze_mod.analyze_package(in_xlsx)
    else:
        analysis = {
            "file": "" if str(in_xlsx) == "-" else str(in_xlsx),
            "format": "xlsx",
            "parts_count": 0,
            "parts_sample": [],
            "structure": {},
            "risk_summary": {
                "has_charts": False,
                "has_pivots": False,
                "has_controls": False,
                "has_macros": False,
                "has_formulas": False,
                "has_external_links": False,
                "risk_level": "low",
            },
            "note": "create_mode_no_input" if str(in_xlsx) == "-" else "input_missing_create_mode",
        }
    _write_json(paths.analysis, analysis)

    if out_xlsx_final.exists() and not overwrite:
        raise FileExistsError(f"Output already exists: {out_xlsx_final} (use --overwrite to replace it)")

    try:
        apply_report = apply_mod.apply_plan(in_xlsx, plan, out_xlsx_final, mode=mode)
    except Exception as e:
        risk = analysis.get("risk_summary") if isinstance(analysis, dict) else None
        apply_report = {
            "engine": "unknown",
            "touched_parts": [],
            "operations": [],
            "classification": plan_mod.classify_plan(plan),
            "parts_before": {},
            "parts_after": {},
            "parts_diff": {},
            "change_summary": f"apply_failed: {e}",
            "risk_summary": risk if isinstance(risk, dict) else {},
            "errors": [{"message": str(e)}],
        }
    _write_json(paths.apply_report, apply_report)

    touched_parts = apply_report.get("touched_parts", [])
    touched_list: list[str] = []
    if isinstance(touched_parts, list):
        touched_list = [p for p in touched_parts if isinstance(p, str)]

    if out_xlsx_final.exists() and apply_report.get("engine") != "unknown":
        try:
            gate_a = validate_mod.gate_a_validate(out_xlsx_final, touched_parts=touched_list)
        except Exception as e:
            gate_a = {
                "ok": False,
                "scope": ["xlsx"],
                "errors": [{"part_uri": "xlsx", "error_type": "consistency", "message": f"gate_a_failed: {e}"}],
                "warnings": [],
            }
    else:
        gate_a = {
            "ok": False,
            "scope": ["xlsx"],
            "errors": [{"part_uri": "xlsx", "error_type": "consistency", "message": str(apply_report.get("change_summary") or "apply_failed")}],
            "warnings": [],
        }
    _write_json(paths.gate_a, gate_a)

    has_formulas = bool(
        isinstance(apply_report, dict)
        and isinstance(apply_report.get("risk_summary"), dict)
        and apply_report["risk_summary"].get("has_formulas") is True
    )
    gate_b_required = has_formulas
    constraints = plan.get("constraints")
    if isinstance(constraints, dict) and constraints.get("require_zero_formula_errors") is False:
        gate_b_required = False

    if out_xlsx_final.exists() and apply_report.get("engine") != "unknown":
        try:
            gate_b = recalc_mod.gate_b_recalc(xlsx_path=out_xlsx_final, max_cells=max_cells, timeout_s=timeout_s)
        except Exception as e:
            gate_b = {
                "required": gate_b_required,
                "skipped": True,
                "skip_reason": f"gate_b_failed: {e}",
                "ok": None,
                "warning": "⚠️ Gate B skipped due to an error; formula results are not certified.",
            }
    else:
        gate_b = {
            "required": gate_b_required,
            "skipped": True,
            "skip_reason": "Skipped because apply failed",
            "ok": None,
            "warning": "⚠️ Gate B skipped; formula results are not certified.",
        }
    _write_json(paths.gate_b, gate_b)

    output_for_report = str(out_xlsx_final) if out_xlsx_final.exists() else str(in_xlsx)
    report = report_mod.build_report(
        plan=plan,
        input_file=str(in_xlsx) if input_exists else "",
        output_file=output_for_report,
        apply_report=apply_report,
        gate_a=gate_a,
        gate_b=gate_b,
        analysis=analysis,
        analysis_artifact_path=str(paths.analysis),
        apply_report_artifact_path=str(paths.apply_report),
        gate_a_artifact_path=str(paths.gate_a),
        gate_b_artifact_path=str(paths.gate_b),
    )
    _write_json(paths.report, report)

    overall_status = str(report.get("overall_status") or "")
    ok = overall_status == "success"

    output_str = str(out_xlsx_final) if out_xlsx_final.exists() else ""

    gates = report.get("gates", {}) if isinstance(report.get("gates"), dict) else {}
    gate_a_ok = bool(isinstance(gates.get("gate_a"), dict) and gates["gate_a"].get("ok") is True)
    gate_b_obj = gates.get("gate_b") if isinstance(gates.get("gate_b"), dict) else {}
    gate_b_required = bool(gate_b_obj.get("required") is True)
    gate_b_skipped = bool(gate_b_obj.get("skipped") is True)
    gate_b_ok = gate_b_obj.get("ok")

    return {
        "ok": ok,
        "overall_status": overall_status,
        "input_xlsx": str(in_xlsx),
        "output_xlsx": output_str,
        "artifacts_dir": str(paths.run_dir),
        "artifacts": {
            "plan": str(paths.plan_copy),
            "analysis": str(paths.analysis),
            "apply_report": str(paths.apply_report),
            "gate_a": str(paths.gate_a),
            "gate_b": str(paths.gate_b),
            "report": str(paths.report),
        },
        "gates": {
            "gate_a_ok": gate_a_ok,
            "gate_b_required": gate_b_required,
            "gate_b_skipped": gate_b_skipped,
            "gate_b_ok": gate_b_ok,
        },
        "engine": apply_report.get("engine", ""),
        "change_summary": report.get("change_summary", ""),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="aura_xlsx one-shot pipeline runner")
    ap.add_argument("in_xlsx", type=Path)
    ap.add_argument("plan_json", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="Output .xlsx path (default: <run_dir>/output.xlsx)")
    ap.add_argument("--artifacts-dir", type=Path, default=Path("artifacts"))
    ap.add_argument("--run-id", type=str, default=None, help="Run folder suffix (default: plan.meta.id or timestamp)")
    ap.add_argument("--mode", choices=["auto", "patch", "openpyxl"], default="auto")
    ap.add_argument("--overwrite", action="store_true", help="Allow overwriting --out if it already exists")
    ap.add_argument("--max-cells", type=int, default=2_000_000, help="Max cells to scan when checking formula errors")
    ap.add_argument("--timeout", type=int, default=180, help="LibreOffice timeout seconds (Gate B)")
    args = ap.parse_args()

    in_xlsx = args.in_xlsx if str(args.in_xlsx) == "-" else args.in_xlsx.resolve()
    plan_path = args.plan_json.resolve()
    out_xlsx = args.out.resolve() if args.out else None

    try:
        summary = run_pipeline(
            in_xlsx=in_xlsx,
            plan_path=plan_path,
            out_xlsx=out_xlsx,
            artifacts_dir=args.artifacts_dir.resolve(),
            run_id=args.run_id,
            mode=args.mode,
            overwrite=args.overwrite,
            max_cells=args.max_cells,
            timeout_s=args.timeout,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except Exception as e:
        err = {"ok": False, "error": str(e), "input_xlsx": str(in_xlsx), "plan_json": str(plan_path)}
        print(json.dumps(err, ensure_ascii=False, indent=2))
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
