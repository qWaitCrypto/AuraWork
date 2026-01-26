#!/usr/bin/env python3
"""
Gate A (structure) validation for DOCX.

Implements:
- XSD validation for key + touched parts (using ooxml/schemas)
- Packaging consistency checks (relationships + content types)
- Docx review-markup consistency (tracked changes + comments)

Output format follows references/aura_report_schema.md -> gates.gate_a.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Set, Tuple
from typing import Any

import xml.etree.ElementTree as ET

from utilities import (
    NS,
    ValidationMessage,
    ValidationReport,
    count_paragraphs_and_tables,
    is_docx_path,
    list_required_parts_missing,
    parse_rels,
    read_xml,
    resolve_internal_target,
    unzip_docx,
)

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_OOXML_ROOT = _SKILL_ROOT / "ooxml"
_SCHEMAS = _OOXML_ROOT / "schemas"
_TOOLS = _OOXML_ROOT / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

try:
    import validate_part  # type: ignore
except Exception:
    validate_part = None  # type: ignore

def _add(msgs: List[ValidationMessage], level: str, code: str, message: str, path: str = "") -> None:
    msgs.append(ValidationMessage(level=level, code=code, message=message, path=path))

def _check_required_parts(root: Path, errors: List[ValidationMessage]) -> None:
    missing = list_required_parts_missing(root)
    for p in missing:
        _add(errors, "error", "MISSING_PART", f"Required part missing: {p}", p)

def _check_xml_parse(root: Path, errors: List[ValidationMessage], warnings: List[ValidationMessage]) -> None:
    # Parse key XML files to ensure they're well-formed.
    key = [
        root / "[Content_Types].xml",
        root / "_rels" / ".rels",
        root / "word" / "document.xml",
    ]
    for p in key:
        if p.exists():
            try:
                read_xml(p)
            except Exception as e:
                _add(errors, "error", "XML_PARSE", f"Failed to parse XML: {e}", str(p.relative_to(root)))

def _schema_for_part(part_uri: str) -> Path | None:
    if part_uri == "[Content_Types].xml":
        return _SCHEMAS / "opc" / "opc-contentTypes.xsd"
    if part_uri.endswith(".rels"):
        return _SCHEMAS / "opc" / "opc-relationships.xsd"
    if part_uri.startswith("word/") and part_uri.endswith(".xml"):
        return _SCHEMAS / "ooxml" / "wml.xsd"
    return None

def _xsd_validate_parts(root: Path, scope_parts: List[str]) -> List[dict]:
    if validate_part is None:
        return [{
            "part_uri": "",
            "error_type": "xsd_validation",
            "message": "XSD validation tool not available (ooxml/tools/validate_part.py import failed).",
        }]

    out: List[dict] = []
    cache: dict[Path, Any] = {}
    for part in scope_parts:
        schema_path = _schema_for_part(part)
        if schema_path is None:
            continue
        xml_path = root / part
        if not xml_path.exists():
            continue
        try:
            schema = cache.get(schema_path)
            if schema is None:
                schema = validate_part.load_schema(schema_path)
                cache[schema_path] = schema
            errors = validate_part.validate(xml_path, schema)
        except Exception as e:
            out.append({"part_uri": part, "error_type": "xsd_validation", "message": str(e)})
            continue
        for line in errors[:200]:
            out.append({"part_uri": part, "error_type": "xsd_validation", "message": line})
    return out

def _check_content_types_complete(root: Path, errors: List[ValidationMessage]) -> None:
    ct = root / "[Content_Types].xml"
    if not ct.exists():
        return
    try:
        tree = read_xml(ct)
    except Exception as e:
        _add(errors, "error", "CT_PARSE", f"Failed to parse [Content_Types].xml: {e}", "[Content_Types].xml")
        return

    ns = {"ct": "http://schemas.openxmlformats.org/package/2006/content-types"}
    overrides = set()
    defaults = set()
    for ov in tree.getroot().findall("ct:Override", ns):
        pn = ov.attrib.get("PartName")
        if pn:
            overrides.add(pn.lstrip("/"))
    for d in tree.getroot().findall("ct:Default", ns):
        ext = d.attrib.get("Extension")
        if ext:
            defaults.add(ext.lower())

    for p in root.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(root).as_posix()
        if rel == "[Content_Types].xml":
            continue
        # Must be covered by override or default extension.
        if rel in overrides:
            continue
        ext = p.suffix.lstrip(".").lower()
        # Special case: OPC root relationships part is named `.rels` (no basename),
        # but it should match the `rels` default content type.
        if not ext and p.name == ".rels":
            ext = "rels"
        if ext and ext in defaults:
            continue
        _add(errors, "error", "CT_MISSING", f"Missing content type for part: {rel}", "[Content_Types].xml")

def _check_relationship_targets(root: Path, errors: List[ValidationMessage], warnings: List[ValidationMessage]) -> None:
    rel_files = [
        root / "_rels" / ".rels",
        root / "word" / "_rels" / "document.xml.rels",
    ]
    for rels_path in rel_files:
        if not rels_path.exists():
            continue
        try:
            rels = parse_rels(rels_path)
        except Exception as e:
            _add(errors, "error", "RELS_PARSE", f"Failed to parse relationships: {e}", str(rels_path.relative_to(root)))
            continue

        for rid, rtype, target in rels:
            # Skip external targets
            if "://" in target or target.startswith("mailto:"):
                continue
            resolved = resolve_internal_target(root, rels_path, target)
            if str(resolved) == ".":
                continue
            if resolved and not resolved.exists():
                # Some targets are in other folders; treat missing as warning or error based on type.
                sev = "error" if "comments" in target or "document.xml" in target else "warning"
                _add(
                    errors if sev == "error" else warnings,
                    sev,
                    "RELS_TARGET_MISSING",
                    f"Relationship target missing: {target} (Id={rid})",
                    str(rels_path.relative_to(root)),
                )

def _check_revisions(root: Path, errors: List[ValidationMessage], warnings: List[ValidationMessage], stats: Dict[str, int]) -> None:
    doc_path = root / "word" / "document.xml"
    if not doc_path.exists():
        return
    tree = read_xml(doc_path)
    r = tree.getroot()

    rev_ids: List[str] = []
    for tag in ("ins", "del"):
        for el in r.findall(f".//w:{tag}", NS):
            rid = el.attrib.get(f"{{{NS['w']}}}id") or el.attrib.get("w:id") or el.attrib.get("id")
            if rid is None:
                _add(warnings, "warning", "REV_ID_MISSING", f"Revision element w:{tag} missing id", "word/document.xml")
            else:
                rev_ids.append(str(rid))

    stats["revisions"] = len(rev_ids)
    dup = {x for x in rev_ids if rev_ids.count(x) > 1}
    if dup:
        _add(errors, "error", "REV_ID_DUP", f"Duplicate revision ids found: {sorted(list(dup))[:20]}", "word/document.xml")

def _extract_comment_ids_from_comments(comments_path: Path) -> Set[str]:
    tree = read_xml(comments_path)
    root = tree.getroot()
    ids: Set[str] = set()
    for c in root.findall(".//w:comment", NS):
        cid = c.attrib.get(f"{{{NS['w']}}}id") or c.attrib.get("w:id") or c.attrib.get("id")
        if cid is not None:
            ids.add(str(cid))
    return ids

def _extract_comment_refs_from_document(document_root: ET.Element) -> Tuple[Set[str], List[str], List[str]]:
    """
    Returns (all_ids_referenced, starts, ends) as strings.
    """
    refs: Set[str] = set()
    starts: List[str] = []
    ends: List[str] = []
    for tag, sink in (("commentRangeStart", starts), ("commentRangeEnd", ends), ("commentReference", [])):
        for el in document_root.findall(f".//w:{tag}", NS):
            cid = el.attrib.get(f"{{{NS['w']}}}id") or el.attrib.get("w:id") or el.attrib.get("id")
            if cid is not None:
                refs.add(str(cid))
                if tag != "commentReference":
                    sink.append(str(cid))
    return refs, starts, ends

def _check_comments(root: Path, errors: List[ValidationMessage], warnings: List[ValidationMessage], stats: Dict[str, int]) -> None:
    comments_path = root / "word" / "comments.xml"
    doc_path = root / "word" / "document.xml"
    if not doc_path.exists():
        return

    tree = read_xml(doc_path)
    doc_root = tree.getroot()

    refs, starts, ends = _extract_comment_refs_from_document(doc_root)
    stats["comment_refs_in_document"] = len(refs)
    stats["comment_ranges_start"] = len(starts)
    stats["comment_ranges_end"] = len(ends)

    if not comments_path.exists():
        if refs:
            _add(errors, "error", "COMMENTS_MISSING", "Document references comments but word/comments.xml is missing", "word/document.xml")
        stats["comments"] = 0
        return

    comment_ids = _extract_comment_ids_from_comments(comments_path)
    stats["comments"] = len(comment_ids)

    # referenced ids must exist in comments.xml
    missing = sorted(list(refs - comment_ids))
    if missing:
        _add(errors, "error", "COMMENT_ID_MISSING", f"Comment ids referenced but not defined: {missing[:30]}", "word/document.xml")

    # starts must match ends
    if sorted(starts) != sorted(ends):
        _add(errors, "error", "COMMENT_RANGE_MISMATCH", "commentRangeStart ids do not match commentRangeEnd ids", "word/document.xml")

    # duplicate comment ids in comments.xml
    # (ET doesn't give duplicates easily; re-parse and count)
    tree2 = read_xml(comments_path)
    root2 = tree2.getroot()
    seen = []
    for c in root2.findall(".//w:comment", NS):
        cid = c.attrib.get(f"{{{NS['w']}}}id") or c.attrib.get("w:id") or c.attrib.get("id")
        if cid is not None:
            seen.append(str(cid))
    dup = {x for x in seen if seen.count(x) > 1}
    if dup:
        _add(errors, "error", "COMMENT_ID_DUP", f"Duplicate comment ids in comments.xml: {sorted(list(dup))[:20]}", "word/comments.xml")

def validate_path(path: Path) -> ValidationReport:
    tmpdir = None
    root = path
    if is_docx_path(path):
        tmpdir = Path(tempfile.mkdtemp(prefix="docx_validate_"))
        unzip_docx(path, tmpdir)
        root = tmpdir

    errors: List[ValidationMessage] = []
    warnings: List[ValidationMessage] = []
    stats: Dict[str, int] = {}

    _check_required_parts(root, errors)
    if not errors:
        _check_xml_parse(root, errors, warnings)
        _check_relationship_targets(root, errors, warnings)
        _check_content_types_complete(root, errors)
        # Stats from document.xml
        doc_xml = root / "word" / "document.xml"
        if doc_xml.exists():
            paras, tbls = count_paragraphs_and_tables(doc_xml)
            stats["paragraphs"] = paras
            stats["tables"] = tbls
        _check_revisions(root, errors, warnings, stats)
        _check_comments(root, errors, warnings, stats)

    ok = len([e for e in errors if e.level == "error"]) == 0
    report = ValidationReport(ok=ok, errors=errors, warnings=warnings, stats=stats)

    if tmpdir is not None:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return report

def to_gate_a_dict(
    *,
    root: Path,
    rep: ValidationReport,
    scope_parts: List[str],
    xsd_errors: List[dict],
) -> dict:
    errors: List[dict] = []
    warnings: List[dict] = []

    for m in rep.errors:
        errors.append({"part_uri": m.path or "", "error_type": "consistency", "message": f"[{m.code}] {m.message}"})
    for w in rep.warnings:
        warnings.append({"part_uri": w.path or "", "error_type": "consistency", "message": f"[{w.code}] {w.message}"})
    for e in xsd_errors:
        errors.append(e)

    ok = (len(errors) == 0)
    return {"ok": ok, "scope": scope_parts, "errors": errors, "warnings": warnings}

def main() -> None:
    ap = argparse.ArgumentParser(description="Gate A validation for DOCX (.docx or unpacked folder).")
    ap.add_argument("path", type=Path, help="Path to .docx file or unpacked folder")
    ap.add_argument("--apply-report", type=Path, default=None, help="Optional apply_report.json to load touched_parts")
    ap.add_argument("--out", type=Path, default=None, help="Write gate_a JSON to this path")
    args = ap.parse_args()

    touched: List[str] = []
    if args.apply_report and args.apply_report.exists():
        try:
            ar = json.loads(args.apply_report.read_text(encoding="utf-8"))
            if isinstance(ar, dict) and isinstance(ar.get("touched_parts"), list):
                touched = [str(x) for x in ar.get("touched_parts") if isinstance(x, str)]
        except Exception:
            touched = []

    scope_parts: List[str] = [
        "[Content_Types].xml",
        "_rels/.rels",
        "word/document.xml",
        "word/_rels/document.xml.rels",
    ]
    for p in touched:
        if p not in scope_parts:
            scope_parts.append(p)

    rep = validate_path(args.path)
    # XSD validation operates on unpacked folder. Unpack if needed.
    tmpdir = None
    root = args.path
    if is_docx_path(args.path):
        tmpdir = Path(tempfile.mkdtemp(prefix="docx_gate_a_"))
        unzip_docx(args.path, tmpdir)
        root = tmpdir

    xsd_errors = _xsd_validate_parts(root, scope_parts)
    gate_a = to_gate_a_dict(root=root, rep=rep, scope_parts=scope_parts, xsd_errors=xsd_errors)

    if tmpdir is not None:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(gate_a, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        print(json.dumps(gate_a, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
