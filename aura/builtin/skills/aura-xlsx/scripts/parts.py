#!/usr/bin/env python3
"""
Package part inspection for XLSX (zip-based).

- list_parts: enumerate zip entries and count "risk parts"
- diff_parts: compare before/after parts (helps detect openpyxl dropping features)
- analyze_workbook: combine zip inspection + OOXML stats (formulas, named ranges, etc.)
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

import xlsx_ooxml

RISK_PREFIXES = [
    "xl/pivotCache/",
    "xl/pivotTables/",
    "xl/charts/",
    "xl/drawings/",
    "xl/ctrlProps/",
    "xl/activeX/",
]

def list_parts(xlsx_path: Path) -> Dict[str, Any]:
    risk = {p: 0 for p in RISK_PREFIXES}
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        names = zf.namelist()
        for n in names:
            for pref in RISK_PREFIXES:
                if n.startswith(pref):
                    risk[pref] += 1
    risk_present = {k: v for k, v in risk.items() if v}
    return {"parts_count": len(names), "risk_parts": risk_present, "parts_sample": names[:50]}

def diff_parts(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    # We only have samples by default; compute diff by re-reading full lists if needed.
    # For now, compare risk part counts and overall parts count.
    b = before.get("risk_parts", {})
    a = after.get("risk_parts", {})
    keys = set(b.keys()) | set(a.keys())
    delta = {k: int(a.get(k, 0)) - int(b.get(k, 0)) for k in sorted(keys)}
    return {
        "parts_count_before": before.get("parts_count"),
        "parts_count_after": after.get("parts_count"),
        "risk_delta": {k: v for k, v in delta.items() if v != 0},
    }

def _try_load_openpyxl():
    try:
        from openpyxl import load_workbook  # type: ignore
        return load_workbook
    except Exception:  # pragma: no cover
        return None

def analyze_workbook(xlsx_path: Path) -> Dict[str, Any]:
    zip_info = list_parts(xlsx_path)

    # Prefer OOXML-based analysis (fast + dependency-light + robust for complex workbooks).
    rep = xlsx_ooxml.analyze_workbook_ooxml(xlsx_path)
    rep["zip"] = zip_info
    return rep
