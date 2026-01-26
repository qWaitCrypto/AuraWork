#!/usr/bin/env python3
"""
Utilities for the Aura DOCX Skill.

This module intentionally stays dependency-light. It uses the standard library,
and will use lxml if available for prettier XML formatting.
"""
from __future__ import annotations

import hashlib
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import xml.etree.ElementTree as ET

# WordprocessingML namespace map (stable URIs)
NS: Dict[str, str] = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rels": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "xml": "http://www.w3.org/XML/1998/namespace",
}

def sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

def is_docx_path(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() == ".docx"

def safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)

def unzip_docx(docx_path: Path, out_dir: Path) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    safe_mkdir(out_dir)
    with zipfile.ZipFile(docx_path, "r") as zf:
        zf.extractall(out_dir)

def zip_dir_to_docx(src_dir: Path, out_docx: Path) -> None:
    # Ensure parent exists
    safe_mkdir(out_docx.parent)
    # Create zip with .docx extension
    with zipfile.ZipFile(out_docx, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src_dir.rglob("*")):
            if p.is_dir():
                continue
            rel = p.relative_to(src_dir).as_posix()
            zf.write(p, rel)

def read_xml(path: Path) -> ET.ElementTree:
    # ET handles UTF-8 BOM and typical XML declarations
    with path.open("rb") as f:
        data = f.read()
    return ET.ElementTree(ET.fromstring(data))

def write_xml(tree: ET.ElementTree, path: Path) -> None:
    # Word files typically omit pretty printing; keep minimal stable output.
    data = ET.tostring(tree.getroot(), encoding="utf-8", xml_declaration=True)
    path.write_bytes(data)

def try_pretty_format_xml(xml_bytes: bytes) -> bytes:
    """
    Optional pretty printing for human editing.
    Uses lxml if available; otherwise returns original bytes.
    """
    try:
        from lxml import etree  # type: ignore
        parser = etree.XMLParser(remove_blank_text=False, recover=False)
        root = etree.fromstring(xml_bytes, parser)
        return etree.tostring(root, encoding="utf-8", xml_declaration=True, pretty_print=True)
    except Exception:
        return xml_bytes

def pretty_format_xml_file(path: Path) -> None:
    b = path.read_bytes()
    formatted = try_pretty_format_xml(b)
    path.write_bytes(formatted)

def list_required_parts_missing(root_dir: Path) -> List[str]:
    required = [
        "[Content_Types].xml",
        "_rels/.rels",
        "word/document.xml",
    ]
    missing = [p for p in required if not (root_dir / p).exists()]
    return missing

@dataclass
class ValidationMessage:
    level: str  # "error" | "warning"
    code: str
    message: str
    path: str = ""

@dataclass
class ValidationReport:
    ok: bool
    errors: List[ValidationMessage]
    warnings: List[ValidationMessage]
    stats: Dict[str, int]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": [m.__dict__ for m in self.errors],
            "warnings": [m.__dict__ for m in self.warnings],
            "stats": self.stats,
        }

def parse_rels(rels_path: Path) -> List[Tuple[str, str, str]]:
    """
    Returns list of (Id, Type, Target) for relationships.
    """
    tree = read_xml(rels_path)
    root = tree.getroot()
    rels = []
    for rel in root.findall(".//{%(rels)s}Relationship" % NS):
        rid = rel.attrib.get("Id", "")
        rtype = rel.attrib.get("Type", "")
        target = rel.attrib.get("Target", "")
        rels.append((rid, rtype, target))
    return rels

def resolve_internal_target(base_dir: Path, rels_file: Path, target: str) -> Path:
    """
    Resolve a relationship Target relative to the relationships file's parent part.
    For `word/_rels/document.xml.rels`, targets are relative to `word/` unless absolute.
    """
    # External targets can be URLs; return a non-existent sentinel.
    if "://" in target or target.startswith("mailto:"):
        return Path("")
    if target.startswith("/"):
        # Package absolute: strip leading slash
        return base_dir / target.lstrip("/")
    # Relative: relative to the part folder (e.g., word/)
    part_dir = rels_file.parent.parent  # e.g., word/_rels -> word
    return part_dir / target

def count_paragraphs_and_tables(document_xml: Path) -> Tuple[int, int]:
    tree = read_xml(document_xml)
    root = tree.getroot()
    paras = root.findall(".//w:p", NS)
    tbls = root.findall(".//w:tbl", NS)
    return len(paras), len(tbls)

def iter_revision_ids(document_root: ET.Element) -> List[str]:
    ids = []
    for tag in ("ins", "del"):
        for el in document_root.findall(f".//w:{tag}", NS):
            rid = el.attrib.get(f"{{{NS['w']}}}id") or el.attrib.get("w:id") or el.attrib.get("id")
            if rid is not None:
                ids.append(str(rid))
    return ids

def read_text_of_paragraph(p: ET.Element) -> str:
    texts = []
    for t in p.findall(".//w:t", NS):
        if t.text:
            texts.append(t.text)
    return "".join(texts).strip()

def build_heading_index(document_xml: Path) -> List[dict]:
    """
    A lightweight heading index based on paragraph style names (w:pStyle).
    """
    tree = read_xml(document_xml)
    root = tree.getroot()
    out = []
    for p in root.findall(".//w:p", NS):
        ppr = p.find("w:pPr", NS)
        style = None
        if ppr is not None:
            pstyle = ppr.find("w:pStyle", NS)
            if pstyle is not None:
                style = pstyle.attrib.get(f"{{{NS['w']}}}val") or pstyle.attrib.get("w:val") or pstyle.attrib.get("val")
        text = read_text_of_paragraph(p)
        if style and style.lower().startswith("heading") and text:
            out.append({"style": style, "text": text, "anchor": sha1_text(text[:200])})
    return out
