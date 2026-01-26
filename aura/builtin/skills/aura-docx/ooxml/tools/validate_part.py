#!/usr/bin/env python3
"""
Validate an XML file against an XSD schema using lxml.

This is intentionally simple and designed for offline, schema-only structural validation.
It does NOT validate packaging-level semantics (relationships/content-types consistency),
nor application-level semantics (e.g., Excel formula correctness).

Usage:
  python validate_part.py --xsd path/to/schema.xsd --xml path/to/file.xml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lxml import etree


def load_schema(xsd_path: Path) -> etree.XMLSchema:
    parser = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False, huge_tree=True)
    # base_url is critical so that relative schemaLocation imports resolve correctly.
    xsd_doc = etree.parse(str(xsd_path), parser)
    return etree.XMLSchema(xsd_doc)


def validate(xml_path: Path, schema: etree.XMLSchema) -> list[str]:
    parser = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False, huge_tree=True, recover=False)
    doc = etree.parse(str(xml_path), parser)
    ok = schema.validate(doc)
    if ok:
        return []
    # Format errors
    errors = []
    for e in schema.error_log:
        errors.append(f"{xml_path}:{e.line}:{e.column}: {e.level_name}: {e.message}")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate an XML file against an XSD schema (lxml).")
    ap.add_argument("--xsd", required=True, help="Path to XSD schema file.")
    ap.add_argument("--xml", required=True, help="Path to XML file to validate.")
    args = ap.parse_args()

    xsd_path = Path(args.xsd).resolve()
    xml_path = Path(args.xml).resolve()

    if not xsd_path.is_file():
        print(f"ERROR: XSD not found: {xsd_path}", file=sys.stderr)
        return 2
    if not xml_path.is_file():
        print(f"ERROR: XML not found: {xml_path}", file=sys.stderr)
        return 2

    try:
        schema = load_schema(xsd_path)
    except Exception as e:
        print(f"ERROR: failed to load XSD schema: {xsd_path}\n{e}", file=sys.stderr)
        return 3

    try:
        errors = validate(xml_path, schema)
    except Exception as e:
        print(f"ERROR: failed to parse/validate XML: {xml_path}\n{e}", file=sys.stderr)
        return 4

    if not errors:
        print("OK")
        return 0

    print("FAILED")
    for line in errors[:200]:
        print(line)
    if len(errors) > 200:
        print(f"... ({len(errors)-200} more)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
