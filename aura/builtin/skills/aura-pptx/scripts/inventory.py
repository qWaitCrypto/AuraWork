#!/usr/bin/env python3
"""
Extract a planning-oriented inventory from a PPTX file.

This is intended for subagents: generate a compact JSON that helps answer:
- Which slides exist (1-based indices)?
- Which shapes have text / placeholders?
- What are the shape ids (for replace_text.shape_id)?
- What placeholders exist (for fill_placeholder.placeholder_id)?
"""
from __future__ import annotations

import argparse
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rels": "http://schemas.openxmlformats.org/package/2006/relationships",
}

REL_NS = {"rels": NS["rels"]}

_TOKEN_RE = re.compile(r"\{\{[^{}]{1,200}\}\}")


def _read_xml_from_zip(zf: zipfile.ZipFile, name: str) -> ET.Element:
    data = zf.read(name)
    return ET.fromstring(data)


def _load_slide_order(zf: zipfile.ZipFile) -> list[str]:
    pres_root = _read_xml_from_zip(zf, "ppt/presentation.xml")
    rels_root = _read_xml_from_zip(zf, "ppt/_rels/presentation.xml.rels")

    rid_to_target: dict[str, str] = {}
    for rel in rels_root.findall("rels:Relationship", REL_NS):
        rid = rel.attrib.get("Id")
        target = rel.attrib.get("Target")
        if rid and target:
            rid_to_target[rid] = target

    sld_ids = pres_root.find("p:sldIdLst", NS)
    if sld_ids is None:
        return []

    parts: list[str] = []
    for sld in sld_ids.findall("p:sldId", NS):
        rid = sld.attrib.get(f"{{{NS['r']}}}id") or sld.attrib.get("r:id") or ""
        target = rid_to_target.get(rid, "")
        if not target:
            continue
        if target.startswith("/"):
            target = target.lstrip("/")
        parts.append(f"ppt/{target}")
    return parts


def _shape_bbox_emu(shape: ET.Element) -> dict[str, int] | None:
    xfrm = shape.find(".//a:xfrm", NS)
    if xfrm is None:
        return None
    off = xfrm.find("a:off", NS)
    ext = xfrm.find("a:ext", NS)
    if off is None or ext is None:
        return None
    try:
        return {
            "x": int(off.attrib.get("x", "0")),
            "y": int(off.attrib.get("y", "0")),
            "cx": int(ext.attrib.get("cx", "0")),
            "cy": int(ext.attrib.get("cy", "0")),
        }
    except Exception:
        return None


def _extract_shapes(slide_root: ET.Element) -> list[dict[str, Any]]:
    shapes: list[dict[str, Any]] = []
    for sp in slide_root.findall(".//p:sp", NS):
        cnv = sp.find(".//p:cNvPr", NS)
        shape_id = cnv.attrib.get("id") if cnv is not None else ""
        name = cnv.attrib.get("name") if cnv is not None else ""

        ph = sp.find(".//p:ph", NS)
        placeholder_type = ph.attrib.get("type") if ph is not None else ""
        placeholder_idx = ph.attrib.get("idx") if ph is not None else ""

        texts = [t.text or "" for t in sp.findall(".//a:t", NS)]
        text = "".join(texts).strip()

        bbox = _shape_bbox_emu(sp)

        has_placeholder = bool(ph is not None)
        has_text = bool(text)

        if not (has_placeholder or has_text):
            continue

        tokens = _TOKEN_RE.findall(text) if text else []
        shapes.append(
            {
                "shape_id": shape_id,
                "name": name,
                "placeholder": {"type": placeholder_type, "idx": placeholder_idx} if has_placeholder else None,
                "text": text,
                "tokens": tokens,
                "bbox_emu": bbox,
            }
        )
    return shapes


def extract_inventory(pptx_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(pptx_path, "r") as zf:
        slide_parts = _load_slide_order(zf)
        slides: list[dict[str, Any]] = []
        all_tokens: set[str] = set()
        for i, part in enumerate(slide_parts, start=1):
            try:
                slide_root = _read_xml_from_zip(zf, part)
            except Exception:
                slides.append(
                    {
                        "slide_id": i,
                        "part_uri": part,
                        "shapes": [],
                        "errors": [f"Failed to parse slide XML: {part}"],
                    }
                )
                continue
            shapes = _extract_shapes(slide_root)
            for s in shapes:
                for tok in s.get("tokens") or []:
                    if isinstance(tok, str) and tok:
                        all_tokens.add(tok)
            slides.append(
                {
                    "slide_id": i,
                    "part_uri": part,
                    "shapes": shapes,
                }
            )

    placeholder_count = 0
    shape_count = 0
    for s in slides:
        for sh in s.get("shapes", []):
            shape_count += 1
            if sh.get("placeholder") is not None:
                placeholder_count += 1

    return {
        "file": str(pptx_path),
        "format": "pptx",
        "structure": {
            "slides": len(slides),
            "shapes_with_text_or_placeholders": shape_count,
            "placeholders": placeholder_count,
        },
        "token_candidates": sorted(all_tokens),
        "slides": slides,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract a planning inventory from a PPTX file.")
    ap.add_argument("pptx", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    rep = extract_inventory(args.pptx)
    payload = json.dumps(rep, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()

