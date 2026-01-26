#!/usr/bin/env python3
"""
Analyze a DOCX package for routing/plan validation.

Outputs a JSON analysis used by apply/validate/report.
"""
from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path
from typing import Any, Dict, List

import xml.etree.ElementTree as ET

from document import analyze_docx
from utilities import NS


def _read_part(zf: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        return zf.read(name)
    except KeyError:
        return None


def _try_parse_xml(document_xml: bytes) -> ET.Element | None:
    try:
        return ET.fromstring(document_xml)
    except Exception:
        return None


def _has_revision_markup(root: ET.Element) -> bool:
    return bool(root.findall(".//w:ins", NS) or root.findall(".//w:del", NS))


def _has_comment_refs(root: ET.Element) -> bool:
    return bool(root.findall(".//w:commentReference", NS))


def _extract_plain_text(root: ET.Element) -> tuple[int, str]:
    paras: list[str] = []
    for p in root.findall(".//w:p", NS):
        texts: list[str] = []
        for t in p.findall(".//w:t", NS):
            if t.text:
                texts.append(t.text)
        line = "".join(texts).strip()
        if line:
            paras.append(line)
    text = "\n".join(paras).strip() + ("\n" if paras else "")
    return len(paras), text


def analyze_package(docx_path: Path) -> Dict[str, Any]:
    with zipfile.ZipFile(docx_path, "r") as zf:
        parts = zf.namelist()

        has_macros = any(p.endswith("vbaProject.bin") for p in parts)
        has_external_links = False
        # Best-effort: scan all rels for TargetMode="External"
        for p in parts:
            if not p.endswith(".rels"):
                continue
            try:
                rels = ET.fromstring(zf.read(p))
            except Exception:
                continue
            for rel in rels.findall(".//{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"):
                if rel.attrib.get("TargetMode") == "External":
                    has_external_links = True
                    break
            if has_external_links:
                break

        document_xml = _read_part(zf, "word/document.xml") or b""
        doc_root = _try_parse_xml(document_xml) if document_xml else None

        has_tracked_changes = _has_revision_markup(doc_root) if doc_root is not None else False
        has_comments_part = bool(_read_part(zf, "word/comments.xml"))
        has_comment_refs = _has_comment_refs(doc_root) if doc_root is not None else False
        has_comments = has_comments_part or has_comment_refs

    # Reuse existing structure analysis (python-docx if available, else OOXML fallback).
    structure = analyze_docx(docx_path)

    risk_level = "low"
    if has_macros or has_external_links:
        risk_level = "high"
    elif has_tracked_changes or has_comments:
        risk_level = "medium"

    return {
        "file": str(docx_path),
        "format": "docx",
        "parts_count": len(parts),
        "parts_sample": parts[:80],
        "structure": structure,
        "risk_summary": {
            "has_charts": False,
            "has_pivots": False,
            "has_controls": False,
            "has_macros": has_macros,
            "has_formulas": False,
            "has_external_links": has_external_links,
            "has_tracked_changes": has_tracked_changes,
            "has_comments": has_comments,
            "risk_level": risk_level,
        },
        "text_info": {
            "available": doc_root is not None,
            "paragraphs": _extract_plain_text(doc_root)[0] if doc_root is not None else 0,
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Analyze DOCX package structure and risk summary.")
    ap.add_argument("docx", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--text-out", type=Path, default=None, help="Optional: write extracted plain text to this path")
    args = ap.parse_args()

    rep = analyze_package(args.docx)

    if args.text_out:
        with zipfile.ZipFile(args.docx, "r") as zf:
            document_xml = _read_part(zf, "word/document.xml") or b""
        root = _try_parse_xml(document_xml) if document_xml else None
        text = _extract_plain_text(root)[1] if root is not None else ""
        args.text_out.parent.mkdir(parents=True, exist_ok=True)
        args.text_out.write_text(text, encoding="utf-8")
        rep["text_info"]["artifact"] = str(args.text_out)

    payload = json.dumps(rep, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
