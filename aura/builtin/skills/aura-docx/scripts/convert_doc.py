#!/usr/bin/env python3
"""
Optional: Convert legacy .doc to .docx using LibreOffice (soffice).

This script does not produce PDFs; it only converts to DOCX.

Usage:
  python convert_doc.py input.doc --out output.docx
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

def main() -> None:
    ap = argparse.ArgumentParser(description="Convert .doc to .docx using LibreOffice.")
    ap.add_argument("doc", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise SystemExit("LibreOffice 'soffice' not found on PATH. Please convert .doc to .docx manually.")

    outdir = args.out.parent
    outdir.mkdir(parents=True, exist_ok=True)

    # LibreOffice chooses output file name based on input name; we move/rename after.
    cmd = [soffice, "--headless", "--nologo", "--nolockcheck", "--convert-to", "docx", "--outdir", str(outdir), str(args.doc)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"LibreOffice conversion failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")

    produced = outdir / (args.doc.stem + ".docx")
    if not produced.exists():
        raise SystemExit(f"Conversion completed but output not found: {produced}")

    if produced.resolve() != args.out.resolve():
        if args.out.exists():
            args.out.unlink()
        produced.rename(args.out)

    print(str(args.out))

if __name__ == "__main__":
    main()
