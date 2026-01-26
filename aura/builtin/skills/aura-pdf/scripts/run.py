#!/usr/bin/env python3
"""
One-shot pipeline runner for aura_pdf.

Runs the closed loop in one command:
  analyze -> apply -> Gate A validate -> report

This is intended to be the primary entrypoint for subagents.
"""
from __future__ import annotations

import argparse
import json
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


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _rewrite_relpath_to_run_dir(value: str, run_dir: Path) -> str:
    p = Path(value)
    if p.is_absolute():
        return str(p)
    parts = p.parts
    if parts and parts[0] == "artifacts":
        p = Path(*parts[1:])
    return str(run_dir / p)


def _compute_task_id(plan: dict[str, Any]) -> str:
    meta = plan.get("meta")
    if isinstance(meta, dict):
        task_id = meta.get("id")
        if isinstance(task_id, str) and task_id.strip():
            return task_id.strip()
    return f"task_{_utc_now_compact()}"


def _resolve_plan_for_run(
    *,
    plan: dict[str, Any],
    run_dir: Path,
    out_pdf: Path | None,
) -> tuple[dict[str, Any], Path | None]:
    resolved: dict[str, Any] = json.loads(json.dumps(plan))
    ops = resolved.get("operations", [])
    if not isinstance(ops, list):
        raise ValueError("plan.json must include an 'operations' list.")

    transform_ops = {"merge", "split", "fill_form", "add_watermark", "encrypt", "decrypt", "rotate"}
    transform = next((op for op in ops if isinstance(op, dict) and op.get("op") in transform_ops), None)

    produced_pdf: Path | None = None
    if isinstance(transform, dict):
        produced_pdf = out_pdf.resolve() if out_pdf else (run_dir / "output.pdf")
        transform["output"] = str(produced_pdf)

    for op in ops:
        if not isinstance(op, dict):
            continue
        k = op.get("op")
        if k in {"extract_text", "extract_tables", "ocr_extract"}:
            op["output"] = _rewrite_relpath_to_run_dir(str(op["output"]), run_dir)
        elif k == "extract_images":
            op["output_dir"] = _rewrite_relpath_to_run_dir(str(op["output_dir"]), run_dir)
        elif k == "get_metadata":
            out = op.get("output") or "pdf_metadata.json"
            op["output"] = _rewrite_relpath_to_run_dir(str(out), run_dir)

    return resolved, produced_pdf


@dataclass(frozen=True, slots=True)
class RunPaths:
    run_dir: Path
    plan_copy: Path
    analysis: Path
    apply_report: Path
    gate_a: Path
    report: Path


def run_pipeline(
    *,
    in_pdf: Path,
    plan_path: Path,
    out_pdf: Path | None,
    artifacts_dir: Path,
    run_id: str | None,
    overwrite: bool,
) -> dict[str, Any]:
    plan = plan_mod.load_and_validate_plan(plan_path)

    task_id = _compute_task_id(plan)
    rid = _sanitize_token(run_id or task_id)
    run_dir = _alloc_run_dir(artifacts_dir, f"aura_pdf_{rid}")

    paths = RunPaths(
        run_dir=run_dir,
        plan_copy=run_dir / "plan.json",
        analysis=run_dir / "analysis.json",
        apply_report=run_dir / "apply_report.json",
        gate_a=run_dir / "gate_a.json",
        report=run_dir / "report.json",
    )

    resolved_plan, produced_pdf = _resolve_plan_for_run(plan=plan, run_dir=run_dir, out_pdf=out_pdf)
    _write_json(paths.plan_copy, resolved_plan)

    analysis = analyze_mod.analyze_pdf(in_pdf)
    _write_json(paths.analysis, analysis)

    if produced_pdf and produced_pdf.exists() and not overwrite:
        raise FileExistsError(f"Output already exists: {produced_pdf} (use --overwrite to replace it)")

    try:
        apply_report = apply_mod.apply_plan(plan=resolved_plan, input_pdf=in_pdf, output_pdf=produced_pdf)
    except Exception as e:
        risk = analysis.get("risk_summary") if isinstance(analysis, dict) else None
        apply_report = {
            "engine": "unknown",
            "touched_parts": [],
            "operations": [],
            "produced_pdf": "",
            "artifacts": [],
            "change_summary": f"apply_failed: {e}",
            "risk_summary": risk if isinstance(risk, dict) else {},
            "errors": [{"message": str(e)}],
        }
    _write_json(paths.apply_report, apply_report)

    out_path_str = str(apply_report.get("produced_pdf") or "")
    out_path = Path(out_path_str).resolve() if out_path_str else None
    final_output = out_path if out_path and out_path.exists() else in_pdf

    gate_a = validate_mod.gate_a_validate(final_output)
    _write_json(paths.gate_a, gate_a)

    report = report_mod.build_report(
        plan=resolved_plan,
        input_file=str(in_pdf),
        output_file=str(final_output),
        apply_report=apply_report,
        gate_a=gate_a,
        analysis=analysis,
        analysis_artifact_path=str(paths.analysis),
        apply_report_artifact_path=str(paths.apply_report),
        gate_a_artifact_path=str(paths.gate_a),
    )
    _write_json(paths.report, report)

    overall_status = str(report.get("overall_status") or "")
    ok = overall_status == "success"

    return {
        "ok": ok,
        "overall_status": overall_status,
        "input_pdf": str(in_pdf),
        "output_pdf": str(final_output) if final_output != in_pdf else "",
        "artifacts_dir": str(paths.run_dir),
        "artifacts": {
            "plan": str(paths.plan_copy),
            "analysis": str(paths.analysis),
            "apply_report": str(paths.apply_report),
            "gate_a": str(paths.gate_a),
            "report": str(paths.report),
        },
        "gates": {
            "gate_a_ok": bool(gate_a.get("ok") is True),
        },
        "engine": apply_report.get("engine", ""),
        "change_summary": report.get("change_summary", ""),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="aura_pdf one-shot pipeline runner")
    ap.add_argument("in_pdf", type=Path)
    ap.add_argument("plan_json", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="Output .pdf path (default: <run_dir>/output.pdf when needed)")
    ap.add_argument("--artifacts-dir", type=Path, default=Path("artifacts"))
    ap.add_argument("--run-id", type=str, default=None, help="Run folder suffix (default: plan.meta.id or timestamp)")
    ap.add_argument("--overwrite", action="store_true", help="Allow overwriting --out if it already exists")
    args = ap.parse_args()

    in_pdf = args.in_pdf.resolve()
    plan_path = args.plan_json.resolve()
    out_pdf = args.out.resolve() if args.out else None

    try:
        summary = run_pipeline(
            in_pdf=in_pdf,
            plan_path=plan_path,
            out_pdf=out_pdf,
            artifacts_dir=args.artifacts_dir.resolve(),
            run_id=args.run_id,
            overwrite=args.overwrite,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except Exception as e:
        err = {"ok": False, "error": str(e), "input_pdf": str(in_pdf), "plan_json": str(plan_path)}
        print(json.dumps(err, ensure_ascii=False, indent=2))
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
