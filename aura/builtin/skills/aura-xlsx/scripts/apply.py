#!/usr/bin/env python3
"""
Apply an XLSX plan.json (aura_plan_spec) to an XLSX package.

Produces a plan-aware apply_report.json with engine and touched_parts for Gate A.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from collections import Counter
from typing import Any, Dict, List

import parts
import plan as planmod
import patch_xlsx
import xlsx_ooxml
import zipfile


def _list_all_parts(xlsx_path: Path) -> List[str]:
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        return [n for n in zf.namelist() if not n.endswith("/")]


def _is_create_plan(plan: Dict[str, Any]) -> bool:
    for op in plan.get("operations", []):
        if isinstance(op, dict) and op.get("op") == "create_workbook":
            return True
    return False


def _sheet_to_part(in_xlsx: Path, sheet_name: str) -> str | None:
    import zipfile

    with zipfile.ZipFile(in_xlsx, "r") as zf:
        try:
            return xlsx_ooxml.get_sheet_part(zf, sheet_name)
        except Exception:
            return None


def apply_plan(in_xlsx: Path, plan: Dict[str, Any], out_xlsx: Path, mode: str) -> Dict[str, Any]:
    classification = planmod.classify_plan(plan)
    input_exists = str(in_xlsx) != "-" and in_xlsx.exists()
    create_mode = mode == "create" or (mode == "auto" and (not input_exists or _is_create_plan(plan)))
    before = parts.list_parts(in_xlsx) if input_exists else {"parts_count": 0, "risk_parts": {}, "parts_sample": []}
    risk_present = bool(before.get("risk_parts"))

    # Risk signals from the input package (macros/external links are package-level).
    has_macros = False
    has_external_links = False
    try:
        import zipfile
        import xml.etree.ElementTree as ET

        rel_ns = {"rels": "http://schemas.openxmlformats.org/package/2006/relationships"}
        with zipfile.ZipFile(in_xlsx, "r") as zf:
            names = set(zf.namelist())
            has_macros = "xl/vbaProject.bin" in names
            for name in names:
                if not name.endswith(".rels"):
                    continue
                try:
                    root = ET.fromstring(zf.read(name))
                except Exception:
                    continue
                for rel in root.findall("rels:Relationship", rel_ns):
                    if rel.attrib.get("TargetMode") == "External":
                        has_external_links = True
                        break
                if has_external_links:
                    break
    except Exception:
        pass

    chosen = mode
    if create_mode and mode == "patch":
        raise SystemExit("Patch mode cannot be used when creating a new workbook.")
    if mode == "auto" and not create_mode:
        chosen = "patch" if classification["is_value_only"] else "openpyxl"

    touched_parts: List[str] = []
    engine = chosen

    if create_mode:
        import openpyxl_apply

        openpyxl_apply.create_workbook_openpyxl(plan, out_xlsx)
        engine = "openpyxl_create"
        touched_parts = _list_all_parts(out_xlsx)
    elif chosen == "patch":
        if not classification["is_value_only"]:
            raise SystemExit("Patch mode supports value-only plans only. Use --mode openpyxl.")
        touched_parts = patch_xlsx.apply_value_only_patch(in_xlsx, plan, out_xlsx)
        engine = "patch"
    else:
        # openpyxl path (may be unavailable)
        import openpyxl_apply

        openpyxl_apply.apply_plan_openpyxl(in_xlsx, plan, out_xlsx)
        engine = "openpyxl"
        # Best-effort: mark worksheet parts for sheets referenced by ops.
        sheets = []
        for op in plan.get("operations", []):
            sh = op.get("sheet")
            if isinstance(sh, str):
                sheets.append(sh)
        for sh in sorted(set(sheets)):
            part = _sheet_to_part(in_xlsx, sh)
            if part:
                touched_parts.append(part)

    after = parts.list_parts(out_xlsx)
    part_diff = parts.diff_parts(before, after)

    # risk summary for report.json
    risk_level = "low"
    has_formulas = bool(parts.analyze_workbook(out_xlsx).get("formula_cells_total", 0))
    if has_macros or has_external_links:
        risk_level = "high"
    elif has_formulas or risk_present:
        risk_level = "medium"

    # Human-readable change summary
    op_kinds: List[str] = []
    op_results: List[Dict[str, Any]] = []
    for op in plan.get("operations", []):
        if not isinstance(op, dict):
            continue
        k = op.get("op")
        if not isinstance(k, str):
            continue
        op_kinds.append(k)
        if k == "create_workbook":
            sheets = op.get("sheets") if isinstance(op.get("sheets"), list) else []
            op_results.append({"op": k, "sheets": sheets})
        elif k == "add_sheet":
            op_results.append({"op": k, "name": op.get("name", ""), "index": op.get("index")})
        elif k == "rename_sheet":
            op_results.append({"op": k, "from": op.get("from", ""), "to": op.get("to", "")})
        elif k == "remove_sheet":
            op_results.append({"op": k, "name": op.get("name", "")})
        elif k == "define_named_ranges":
            names = op.get("names")
            op_results.append({"op": k, "count": len(names) if isinstance(names, dict) else 0})
        elif k == "freeze_panes":
            op_results.append({"op": k, "sheet": op.get("sheet", ""), "cell": op.get("cell", "")})
        elif k == "set_cells":
            cells = op.get("cells")
            op_results.append({"op": k, "sheet": op.get("sheet", ""), "cells": len(cells) if isinstance(cells, list) else 0})
        elif k == "set_range":
            op_results.append({"op": k, "sheet": op.get("sheet", ""), "range": op.get("range", "")})
        elif k == "fill_named_ranges":
            values = op.get("values")
            op_results.append({"op": k, "count": len(values) if isinstance(values, dict) else 0})
        elif k == "append_table_rows":
            rows = op.get("rows")
            op_results.append({"op": k, "sheet": op.get("sheet", ""), "rows": len(rows) if isinstance(rows, list) else 0})
        elif k in ("insert_rows", "insert_cols"):
            op_results.append({"op": k, "sheet": op.get("sheet", ""), "idx": op.get("idx"), "amount": op.get("amount")})
        elif k == "copy_range":
            op_results.append({"op": k, "sheet": op.get("sheet", ""), "src": op.get("src", ""), "dst": op.get("dst", "")})
        elif k == "merge_cells":
            op_results.append({"op": k, "sheet": op.get("sheet", ""), "range": op.get("range", "")})
        elif k == "set_style":
            op_results.append({"op": k, "sheet": op.get("sheet", ""), "target": op.get("cell") or op.get("range", "")})
        elif k == "set_row_height":
            op_results.append({"op": k, "sheet": op.get("sheet", ""), "row": op.get("row"), "height": op.get("height")})
        elif k == "set_column_width":
            op_results.append({"op": k, "sheet": op.get("sheet", ""), "column": op.get("column"), "width": op.get("width")})
        else:
            op_results.append({"op": k})

    counts = Counter(op_kinds)
    change_summary = ", ".join([f"{k}×{counts[k]}" for k in sorted(counts)]) if counts else ""

    return {
        "engine": engine,
        "touched_parts": touched_parts,
        "operations": op_results,
        "classification": classification,
        "parts_before": before,
        "parts_after": after,
        "parts_diff": part_diff,
        "change_summary": change_summary,
        "risk_summary": {
            "has_charts": bool(before.get("risk_parts", {}).get("xl/charts/")),
            "has_pivots": bool(before.get("risk_parts", {}).get("xl/pivotTables/") or before.get("risk_parts", {}).get("xl/pivotCache/")),
            "has_controls": bool(before.get("risk_parts", {}).get("xl/ctrlProps/") or before.get("risk_parts", {}).get("xl/activeX/")),
            "has_macros": has_macros,
            "has_formulas": has_formulas,
            "has_external_links": has_external_links,
            "risk_level": risk_level,
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Apply aura_xlsx plan.json to an XLSX file.")
    ap.add_argument("in_xlsx", type=Path)
    ap.add_argument("plan_json", type=Path)
    ap.add_argument("out_xlsx", type=Path)
    ap.add_argument("--mode", choices=["auto", "patch", "openpyxl", "create"], default="auto")
    ap.add_argument("--out", type=Path, default=None, help="Write apply_report.json")
    args = ap.parse_args()

    plan = planmod.load_and_validate_plan(args.plan_json)
    rep = apply_plan(args.in_xlsx, plan, args.out_xlsx, mode=args.mode)

    payload = json.dumps(rep, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
