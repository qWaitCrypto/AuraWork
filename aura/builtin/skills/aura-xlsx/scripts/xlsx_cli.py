#!/usr/bin/env python3
"""
Unified CLI for Aura XLSX Skill.

Commands:
- analyze: workbook structure + risk features
- apply: apply a plan.json to produce an output workbook
- validate: recalc (if needed) + scan formula errors + part listing

This CLI coordinates:
- parts.py (package part listing)
- plan.py (plan validation + classification)
- patch_xlsx.py (value-only OOXML patcher)
- openpyxl_apply.py (structural edits via openpyxl)
- recalc.py (LibreOffice recalculation + error scanning)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import parts  # noqa: E402
import plan as planmod  # noqa: E402
import patch_xlsx  # noqa: E402
import openpyxl_apply  # noqa: E402
import recalc  # noqa: E402
import xlsx_ooxml  # noqa: E402

def main() -> None:
    ap = argparse.ArgumentParser(description="Aura XLSX Skill CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="Analyze workbook structure and risk parts.")
    a.add_argument("xlsx", type=Path)
    a.add_argument("--out", type=Path, default=None)

    x = sub.add_parser("extract", help="Extract a sheet range as JSON or CSV (cached values).")
    x.add_argument("xlsx", type=Path)
    x.add_argument("--sheet", required=True, help="Sheet name (as shown in Excel)")
    x.add_argument("--range", dest="range_ref", default=None, help="A1 range like A1:D10 (default: sheet dimension)")
    x.add_argument("--format", choices=["json", "csv"], default="json")
    x.add_argument("--out", type=Path, default=None, help="Write output to this path (default: stdout)")
    x.add_argument("--max-cells", type=int, default=200_000)

    p = sub.add_parser("apply", help="Apply a plan.json to an XLSX.")
    p.add_argument("in_xlsx", type=Path)
    p.add_argument("plan_json", type=Path)
    p.add_argument("out_xlsx", type=Path)
    p.add_argument("--mode", choices=["auto", "patch", "openpyxl"], default="auto")
    p.add_argument("--out", type=Path, default=None, help="Write apply report JSON")

    v = sub.add_parser("validate", help="Validate via recalc (if formulas) and scan for errors.")
    v.add_argument("xlsx", type=Path)
    v.add_argument("--report", type=Path, default=None)
    v.add_argument("--force", action="store_true", help="Force recalc even if no formulas detected")
    v.add_argument("--timeout", type=int, default=180)
    v.add_argument("--max-cells", type=int, default=2_000_000)

    args = ap.parse_args()

    if args.cmd == "analyze":
        rep = parts.analyze_workbook(args.xlsx)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        return

    if args.cmd == "extract":
        rep = xlsx_ooxml.extract_sheet_grid(args.xlsx, sheet=args.sheet, range_ref=args.range_ref, max_cells=args.max_cells)
        if args.format == "json":
            payload = json.dumps(rep, ensure_ascii=False, indent=2)
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(payload, encoding="utf-8")
            else:
                print(payload)
            return

        # csv
        import csv
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with args.out.open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerows(rep["grid"])
            print(json.dumps({"ok": True, "out": str(args.out), "rows": rep["rows"], "cols": rep["cols"]}, ensure_ascii=False))
        else:
            w = csv.writer(sys.stdout)
            w.writerows(rep["grid"])
        return

    if args.cmd == "apply":
        pl = planmod.load_and_validate_plan(args.plan_json)
        classification = planmod.classify_plan(pl)

        before_parts = parts.list_parts(args.in_xlsx)
        risk_present = bool(before_parts.get("risk_parts"))

        mode = args.mode
        chosen = mode
        if mode == "auto":
            # Value-only updates default to patch mode for template preservation.
            chosen = "patch" if classification["is_value_only"] else "openpyxl"

        if chosen == "patch":
            if not classification["is_value_only"]:
                raise SystemExit("Patch mode supports value-only plans only. Use --mode openpyxl.")
            patch_xlsx.apply_value_only_patch(args.in_xlsx, pl, args.out_xlsx)
            applied = {"engine": "patch", "classification": classification}
        else:
            out = openpyxl_apply.apply_plan_openpyxl(args.in_xlsx, pl, args.out_xlsx)
            applied = {"engine": "openpyxl", "openpyxl": out, "classification": classification}

        after_parts = parts.list_parts(args.out_xlsx)
        part_diff = parts.diff_parts(before_parts, after_parts)

        rep = {
            "in": str(args.in_xlsx),
            "out": str(args.out_xlsx),
            "engine": applied["engine"],
            "classification": classification,
            "parts_before": before_parts,
            "parts_after": after_parts,
            "parts_diff": part_diff,
        }

        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        return

    if args.cmd == "validate":
        analysis = parts.analyze_workbook(args.xlsx)
        formulas = analysis.get("formula_cells_total", 0)
        should_recalc = args.force or (formulas and formulas > 0)

        gate_b = None
        if should_recalc:
            gate_b = recalc.gate_b_recalc(xlsx_path=args.xlsx, max_cells=args.max_cells, timeout_s=args.timeout)
        else:
            gate_b = {"required": False, "skipped": False, "ok": None, "reason": "No formulas detected and --force not set."}

        rep = {
            "analysis": analysis,
            "gate_b": gate_b,
            "ok": bool(gate_b.get("ok", False)) if gate_b.get("required") else True,
        }

        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

        summary = {"ok": rep["ok"]}
        if gate_b.get("required"):
            summary["required"] = True
            summary["skipped"] = bool(gate_b.get("skipped"))
            summary["formula_errors"] = gate_b.get("formula_errors")
            if gate_b.get("skip_reason"):
                summary["skip_reason"] = gate_b.get("skip_reason")
        print(json.dumps(summary, ensure_ascii=False))
        return

if __name__ == "__main__":
    main()
