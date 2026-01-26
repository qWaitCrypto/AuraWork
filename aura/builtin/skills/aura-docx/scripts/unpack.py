#!/usr/bin/env python3
"""
Unpack a .docx into an editable folder.

By default, this preserves original XML byte-for-byte (no pretty printing).
Use --pretty to format XML for easier manual editing.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from utilities import pretty_format_xml_file, unzip_docx

XML_GLOBS = ("*.xml", "*.rels")

def main() -> None:
    ap = argparse.ArgumentParser(description="Unpack DOCX -> folder")
    ap.add_argument("docx", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--pretty", action="store_true", help="Pretty-format XML files for editing")
    ap.add_argument("--force", action="store_true", help="Overwrite out_dir if it already exists")
    args = ap.parse_args()

    if args.out_dir.exists() and not args.force:
        raise SystemExit(f"Refusing to overwrite existing directory: {args.out_dir} (use --force)")

    unzip_docx(args.docx, args.out_dir)

    if args.pretty:
        for g in XML_GLOBS:
            for p in args.out_dir.rglob(g):
                try:
                    pretty_format_xml_file(p)
                except Exception:
                    # Formatting is best-effort; do not fail unpack.
                    pass

    print(str(args.out_dir))

if __name__ == "__main__":
    main()
