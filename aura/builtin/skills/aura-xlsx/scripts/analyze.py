#!/usr/bin/env python3
"""
Analyze an XLSX package for routing/plan validation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import zipfile
import xml.etree.ElementTree as ET

import parts


REL_NS = {"rels": "http://schemas.openxmlformats.org/package/2006/relationships"}


def _has_external_links(xlsx_path: Path) -> bool:
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        for name in zf.namelist():
            if not name.endswith(".rels"):
                continue
            try:
                root = ET.fromstring(zf.read(name))
            except Exception:
                continue
            for rel in root.findall("rels:Relationship", REL_NS):
                if rel.attrib.get("TargetMode") == "External":
                    return True
    return False


def analyze_package(xlsx_path: Path) -> dict:
    part_info = parts.list_parts(xlsx_path)
    structure = parts.analyze_workbook(xlsx_path)

    has_formulas = bool(int(structure.get("formula_cells_total", 0) or 0))
    has_macros = False
    try:
        with zipfile.ZipFile(xlsx_path, "r") as zf:
            has_macros = "xl/vbaProject.bin" in set(zf.namelist())
    except Exception:
        has_macros = False

    has_external_links = _has_external_links(xlsx_path)

    risk_level = "low"
    if has_macros or has_external_links:
        risk_level = "high"
    elif has_formulas or part_info.get("risk_parts"):
        risk_level = "medium"

    return {
        "file": str(xlsx_path),
        "format": "xlsx",
        "parts_count": part_info.get("parts_count"),
        "parts_sample": part_info.get("parts_sample", []),
        "structure": structure,
        "risk_summary": {
            "has_charts": bool(part_info.get("risk_parts", {}).get("xl/charts/")),
            "has_pivots": bool(
                part_info.get("risk_parts", {}).get("xl/pivotTables/") or part_info.get("risk_parts", {}).get("xl/pivotCache/")
            ),
            "has_controls": bool(
                part_info.get("risk_parts", {}).get("xl/ctrlProps/") or part_info.get("risk_parts", {}).get("xl/activeX/")
            ),
            "has_macros": has_macros,
            "has_formulas": has_formulas,
            "has_external_links": has_external_links,
            "risk_level": risk_level,
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Analyze XLSX package structure and risk parts.")
    ap.add_argument("xlsx", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    rep = analyze_package(args.xlsx)
    payload = json.dumps(rep, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
