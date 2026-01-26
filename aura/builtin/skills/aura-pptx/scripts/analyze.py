#!/usr/bin/env python3
"""
Analyze a PPTX package for routing/plan validation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import zipfile
import xml.etree.ElementTree as ET

import parts


REL_NS = {"rels": "http://schemas.openxmlformats.org/package/2006/relationships"}


def _has_external_links(pptx_path: Path) -> bool:
    with zipfile.ZipFile(pptx_path, "r") as zf:
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


def _slide_count(pptx_path: Path) -> int:
    try:
        with zipfile.ZipFile(pptx_path, "r") as zf:
            data = zf.read("ppt/presentation.xml")
    except Exception:
        return 0
    try:
        root = ET.fromstring(data)
    except Exception:
        return 0
    ns = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main"}
    sld_ids = root.find("p:sldIdLst", ns)
    if sld_ids is None:
        return 0
    return len(sld_ids.findall("p:sldId", ns))


def analyze_package(pptx_path: Path) -> dict:
    part_info = parts.list_parts(pptx_path)
    slides = _slide_count(pptx_path)

    has_macros = False
    try:
        with zipfile.ZipFile(pptx_path, "r") as zf:
            has_macros = "ppt/vbaProject.bin" in set(zf.namelist())
    except Exception:
        has_macros = False

    has_external_links = _has_external_links(pptx_path)

    has_charts = bool(part_info.get("risk_parts", {}).get("ppt/charts/"))
    has_controls = bool(part_info.get("risk_parts", {}).get("ppt/activeX/") or part_info.get("risk_parts", {}).get("ppt/controlProps/"))

    risk_level = "low"
    if has_macros or has_external_links:
        risk_level = "high"
    elif has_charts or has_controls:
        risk_level = "medium"

    return {
        "file": str(pptx_path),
        "format": "pptx",
        "parts_count": part_info.get("parts_count"),
        "parts_sample": part_info.get("parts_sample", []),
        "structure": {"slides": slides},
        "risk_summary": {
            "has_charts": has_charts,
            "has_pivots": False,
            "has_controls": has_controls,
            "has_macros": has_macros,
            "has_formulas": False,
            "has_external_links": has_external_links,
            "risk_level": risk_level,
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Analyze PPTX package structure and risk parts.")
    ap.add_argument("pptx", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    rep = analyze_package(args.pptx)
    payload = json.dumps(rep, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()

