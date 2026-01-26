#!/usr/bin/env python3
"""
One-shot pipeline runner for aura_docx.

Runs the closed loop in one command:
  analyze -> apply -> Gate A validate -> report

This is intended to be the primary entrypoint for subagents.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import analyze as analyze_mod  # noqa: E402
import apply as apply_mod  # noqa: E402
import report as report_mod  # noqa: E402
import validate as validate_mod  # noqa: E402
import utilities  # noqa: E402


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
    in_docx: Path,
    plan_path: Path,
    out_docx: Path | None,
    artifacts_dir: Path,
    run_id: str | None,
    overwrite: bool,
) -> dict[str, Any]:
    plan = _load_json(plan_path)
    if not isinstance(plan, dict):
        raise ValueError("plan.json must be a JSON object.")

    task_id = _compute_task_id(plan)
    rid = _sanitize_token(run_id or task_id)
    run_dir = _alloc_run_dir(artifacts_dir, f"aura_docx_{rid}")

    out_docx_final = out_docx.resolve() if out_docx else (run_dir / "output.docx")

    paths = RunPaths(
        run_dir=run_dir,
        plan_copy=run_dir / "plan.json",
        analysis=run_dir / "analysis.json",
        apply_report=run_dir / "apply_report.json",
        gate_a=run_dir / "gate_a.json",
        report=run_dir / "report.json",
    )

    shutil.copyfile(plan_path, paths.plan_copy)

    input_exists = in_docx.exists() and str(in_docx) != "-"
    if input_exists:
        analysis = analyze_mod.analyze_package(in_docx)
    else:
        analysis = {
            "file": "" if str(in_docx) == "-" else str(in_docx),
            "format": "docx",
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
                "has_tracked_changes": False,
                "has_comments": False,
                "risk_level": "low",
            },
            "note": "create_mode_no_input" if str(in_docx) == "-" else "input_missing_create_mode",
        }
    _write_json(paths.analysis, analysis)

    if out_docx_final.exists() and not overwrite:
        raise FileExistsError(f"Output already exists: {out_docx_final} (use --overwrite to replace it)")

    try:
        apply_report = apply_mod.apply_plan(in_docx, plan, out_docx_final)
    except Exception as e:
        risk = analysis.get("risk_summary") if isinstance(analysis, dict) else None
        apply_report = {
            "engine": "unknown",
            "touched_parts": [],
            "operations": [],
            "change_summary": f"apply_failed: {e}",
            "risk_summary": risk if isinstance(risk, dict) else {},
            "errors": [{"message": str(e)}],
        }
    _write_json(paths.apply_report, apply_report)

    touched_parts = apply_report.get("touched_parts", [])
    scope_parts: list[str] = [
        "[Content_Types].xml",
        "_rels/.rels",
        "word/document.xml",
        "word/_rels/document.xml.rels",
    ]
    if isinstance(touched_parts, list):
        for p in touched_parts:
            if isinstance(p, str) and p not in scope_parts:
                scope_parts.append(p)

    if out_docx_final.exists() and apply_report.get("engine") != "unknown":
        unpack_root = Path(tempfile.mkdtemp(prefix="docx_gate_a_"))
        try:
            utilities.unzip_docx(out_docx_final, unpack_root)
            rep = validate_mod.validate_path(unpack_root)
            xsd_errors = validate_mod._xsd_validate_parts(unpack_root, scope_parts)  # type: ignore[attr-defined]
            gate_a = validate_mod.to_gate_a_dict(
                root=unpack_root,
                rep=rep,
                scope_parts=scope_parts,
                xsd_errors=xsd_errors,
            )
        finally:
            shutil.rmtree(unpack_root, ignore_errors=True)
    else:
        gate_a = {
            "ok": False,
            "scope": scope_parts,
            "errors": [
                {
                    "part_uri": "docx",
                    "error_type": "consistency",
                    "message": str(apply_report.get("change_summary") or "apply_failed"),
                }
            ],
            "warnings": [],
        }

    _write_json(paths.gate_a, gate_a)

    output_for_report = str(out_docx_final) if out_docx_final.exists() else (str(in_docx) if input_exists else "")
    report = report_mod.build_report(
        plan=plan,
        input_file=str(in_docx) if input_exists else "",
        output_file=output_for_report,
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
        "input_docx": str(in_docx) if input_exists else "",
        "output_docx": str(out_docx_final) if out_docx_final.exists() else "",
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
    ap = argparse.ArgumentParser(description="aura_docx one-shot pipeline runner")
    ap.add_argument("in_docx", type=Path)
    ap.add_argument("plan_json", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="Output .docx path (default: <run_dir>/output.docx)")
    ap.add_argument("--artifacts-dir", type=Path, default=Path("artifacts"))
    ap.add_argument("--run-id", type=str, default=None, help="Run folder suffix (default: plan.meta.id or timestamp)")
    ap.add_argument("--overwrite", action="store_true", help="Allow overwriting --out if it already exists")
    args = ap.parse_args()

    in_docx = args.in_docx if str(args.in_docx) == "-" else args.in_docx.resolve()
    plan_path = args.plan_json.resolve()

    out_docx = args.out.resolve() if args.out else None

    try:
        summary = run_pipeline(
            in_docx=in_docx,
            plan_path=plan_path,
            out_docx=out_docx,
            artifacts_dir=args.artifacts_dir.resolve(),
            run_id=args.run_id,
            overwrite=args.overwrite,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except Exception as e:
        err = {"ok": False, "error": str(e), "input_docx": str(in_docx), "plan_json": str(plan_path)}
        print(json.dumps(err, ensure_ascii=False, indent=2))
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
