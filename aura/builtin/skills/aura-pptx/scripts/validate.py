#!/usr/bin/env python3
"""
Gate A (structure) validation for PPTX.

Implements:
- XSD validation for key + touched parts (using ooxml/schemas)
- Packaging consistency checks (relationships + content types)
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from lxml import etree


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


def _schema_for_part(part_uri: str) -> Path | None:
    if part_uri == "[Content_Types].xml":
        return _SCHEMAS / "opc" / "opc-contentTypes.xsd"
    if part_uri.endswith(".rels"):
        return _SCHEMAS / "opc" / "opc-relationships.xsd"
    if part_uri.startswith("ppt/") and part_uri.endswith(".xml"):
        return _SCHEMAS / "ooxml" / "pml.xsd"
    return None


def _xsd_validate_parts(root: Path, scope_parts: list[str]) -> list[dict[str, Any]]:
    if validate_part is None:
        return [{
            "part_uri": "",
            "error_type": "xsd_validation",
            "message": "XSD validation tool not available (ooxml/tools/validate_part.py import failed).",
        }]

    out: list[dict[str, Any]] = []
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


def _check_rels_targets(root: Path) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    rel_ns = {"rels": "http://schemas.openxmlformats.org/package/2006/relationships"}
    for rels_path in root.rglob("*.rels"):
        try:
            parser = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False, huge_tree=True, recover=False)
            rels = etree.parse(str(rels_path), parser).getroot()
        except Exception as e:
            errors.append({"part_uri": str(rels_path.relative_to(root)), "error_type": "consistency", "message": f"RELS_PARSE: {e}"})
            continue
        for rel in rels.findall(".//rels:Relationship", rel_ns):
            target = rel.attrib.get("Target", "")
            mode = rel.attrib.get("TargetMode", "")
            if mode == "External" or "://" in target or target.startswith("mailto:"):
                continue
            base = rels_path.parent.parent if rels_path.parent.name == "_rels" else rels_path.parent
            resolved = (base / target).resolve()
            try:
                resolved.relative_to(root.resolve())
            except Exception:
                continue
            if not resolved.exists():
                errors.append(
                    {
                        "part_uri": str(rels_path.relative_to(root)),
                        "error_type": "consistency",
                        "message": f"Relationship target missing: {target}",
                    }
                )
    return errors


def _check_content_types(root: Path) -> list[dict[str, Any]]:
    ct = root / "[Content_Types].xml"
    if not ct.exists():
        return [{"part_uri": "[Content_Types].xml", "error_type": "consistency", "message": "Missing [Content_Types].xml"}]

    parser = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False, huge_tree=True, recover=False)
    try:
        doc = etree.parse(str(ct), parser)
    except Exception as e:
        return [{"part_uri": "[Content_Types].xml", "error_type": "consistency", "message": f"CT_PARSE: {e}"}]

    ns = {"ct": "http://schemas.openxmlformats.org/package/2006/content-types"}
    overrides = set()
    defaults = set()
    for ov in doc.findall(".//ct:Override", ns):
        pn = ov.get("PartName")
        if pn:
            overrides.add(pn.lstrip("/"))
    for d in doc.findall(".//ct:Default", ns):
        ext = d.get("Extension")
        if ext:
            defaults.add(ext.lower())

    errors: list[dict[str, Any]] = []
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(root).as_posix()
        if rel == "[Content_Types].xml":
            continue
        if rel in overrides:
            continue
        ext = p.suffix.lstrip(".").lower()
        # Special case: OPC root relationships part is named `.rels` (no basename),
        # but it should match the `rels` default content type.
        if not ext and p.name == ".rels":
            ext = "rels"
        if ext and ext in defaults:
            continue
        errors.append({"part_uri": "[Content_Types].xml", "error_type": "consistency", "message": f"Missing content type for part: {rel}"})
    return errors


def gate_a_validate(pptx_path: Path, touched_parts: list[str]) -> dict[str, Any]:
    tmpdir = Path(tempfile.mkdtemp(prefix="pptx_gate_a_"))
    try:
        with zipfile.ZipFile(pptx_path, "r") as zf:
            zf.extractall(tmpdir)

        scope = [
            "[Content_Types].xml",
            "_rels/.rels",
            "ppt/presentation.xml",
            "ppt/_rels/presentation.xml.rels",
        ]
        for p in touched_parts:
            if p not in scope:
                scope.append(p)

        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        errors.extend(_xsd_validate_parts(tmpdir, scope))
        errors.extend(_check_rels_targets(tmpdir))
        errors.extend(_check_content_types(tmpdir))

        ok = len(errors) == 0
        return {"ok": ok, "scope": scope, "errors": errors, "warnings": warnings}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Gate A validate a PPTX file (XSD + consistency).")
    ap.add_argument("pptx", type=Path)
    ap.add_argument("--apply-report", type=Path, default=None, help="apply_report.json to include touched parts in scope")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    touched: list[str] = []
    if args.apply_report and args.apply_report.exists():
        rep = json.loads(args.apply_report.read_text(encoding="utf-8"))
        if isinstance(rep, dict) and isinstance(rep.get("touched_parts"), list):
            touched = [str(x) for x in rep["touched_parts"] if isinstance(x, str)]

    gate_a = gate_a_validate(args.pptx, touched)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(gate_a, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

