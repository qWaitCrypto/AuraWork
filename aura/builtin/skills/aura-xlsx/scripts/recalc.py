#!/usr/bin/env python3
"""
Gate B (semantics) validation for XLSX: recalc formulas and scan for error tokens.

This script produces a gate_b JSON object compatible with references/aura_report_schema.md.

Notes:
- This does NOT modify the input workbook in-place.
- If the recalculation engine is unavailable, Gate B is reported as skipped.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import xlsx_ooxml


def _find_soffice() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def _run_soffice_recalc(*, in_xlsx: Path, out_dir: Path, timeout_s: int) -> tuple[bool, str, Path | None]:
    soffice = _find_soffice()
    if not soffice:
        return False, "LibreOffice not available", None

    cmd = [
        soffice,
        "--headless",
        "--nologo",
        "--nolockcheck",
        "--norestore",
        "--convert-to",
        "xlsx",
        "--outdir",
        str(out_dir),
        str(in_xlsx),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return False, f"LibreOffice timed out after {timeout_s}s", None
    if proc.returncode != 0:
        return False, f"LibreOffice failed (rc={proc.returncode})", None

    produced = out_dir / f"{in_xlsx.stem}.xlsx"
    if produced.exists():
        return True, "", produced

    cands = sorted(out_dir.glob("*.xlsx"))
    if len(cands) == 1:
        return True, "", cands[0]
    return False, "LibreOffice conversion completed but output was not found", None


def gate_b_recalc(*, xlsx_path: Path, max_cells: int, timeout_s: int) -> dict:
    analysis = xlsx_ooxml.analyze_workbook_ooxml(xlsx_path)
    formula_count = int(analysis.get("formula_cells_total", 0) or 0)

    if formula_count <= 0:
        return {"required": False, "skipped": False, "ok": None, "reason": "No formulas in workbook"}

    with tempfile.TemporaryDirectory(prefix="xlsx_gate_b_") as td:
        out_dir = Path(td)
        ok, err, recalced = _run_soffice_recalc(in_xlsx=xlsx_path, out_dir=out_dir, timeout_s=timeout_s)
        if not ok or recalced is None:
            return {
                "required": True,
                "skipped": True,
                "skip_reason": err or "LibreOffice not available",
                "ok": None,
                "warning": "⚠️ Gate B skipped; formula results are not certified.",
            }

        total, _by_type, samples = xlsx_ooxml.scan_error_cells_ooxml(recalced, max_cells=max_cells)
        error_cells: list[dict] = []
        for err_type, locs in samples.items():
            for loc in locs:
                if "!" in loc:
                    sheet, cell = loc.split("!", 1)
                else:
                    sheet, cell = "", loc
                error_cells.append({"sheet": sheet, "cell": cell, "error_type": err_type})

        return {
            "required": True,
            "skipped": False,
            "ok": total == 0,
            "formula_count": formula_count,
            "formula_errors": total,
            "error_cells": error_cells,
        }


def main() -> None:
    ap = argparse.ArgumentParser(description="Gate B (recalc) validation for XLSX.")
    ap.add_argument("xlsx", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="Write gate_b JSON to this path")
    ap.add_argument("--max-cells", type=int, default=2_000_000)
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    rep = gate_b_recalc(xlsx_path=args.xlsx, max_cells=args.max_cells, timeout_s=args.timeout)
    payload = json.dumps(rep, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()

