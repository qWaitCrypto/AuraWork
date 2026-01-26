#!/usr/bin/env python3
"""
Render a DOCX to page images for visual verification.

Pipeline:
  DOCX -> PDF (LibreOffice) -> images (pdftoppm)

This script is optional and only works when system dependencies are available.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _require_cmd(cmd: str, *, component: str) -> str:
    path = _which(cmd)
    if path:
        return path
    raise SystemExit(f"Missing required command: {cmd} ({component}).")


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(
            "Command failed:\n"
            + " ".join(cmd)
            + "\n\nSTDOUT:\n"
            + (proc.stdout or "")
            + "\nSTDERR:\n"
            + (proc.stderr or "")
        )


def _convert_docx_to_pdf(docx: Path, outdir: Path) -> Path:
    soffice = _require_cmd("soffice", component="LibreOffice")
    outdir.mkdir(parents=True, exist_ok=True)

    _run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(outdir), str(docx)])

    expected = outdir / f"{docx.stem}.pdf"
    if expected.exists():
        return expected

    pdfs = sorted(outdir.glob("*.pdf"))
    if len(pdfs) == 1:
        return pdfs[0]
    raise SystemExit(f"PDF output not found in {outdir} after conversion.")


def _pdf_to_images(*, pdf: Path, outdir: Path, dpi: int, fmt: str) -> list[Path]:
    pdftoppm = _require_cmd("pdftoppm", component="Poppler utils")
    outdir.mkdir(parents=True, exist_ok=True)

    fmt_lower = fmt.lower()
    if fmt_lower in ("jpg", "jpeg"):
        flag = "-jpeg"
        ext = "jpg"
    elif fmt_lower == "png":
        flag = "-png"
        ext = "png"
    else:
        raise SystemExit("Unsupported --format. Use: png | jpeg")

    prefix = outdir / pdf.stem
    _run([pdftoppm, flag, "-r", str(dpi), str(pdf), str(prefix)])

    images = sorted(outdir.glob(f"{pdf.stem}-*.{ext}"))
    return images


def main() -> None:
    ap = argparse.ArgumentParser(description="Render DOCX to page images (via LibreOffice + pdftoppm).")
    ap.add_argument("docx", type=Path)
    ap.add_argument("--outdir", type=Path, required=True, help="Output folder for page images")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--format", default="jpeg", help="png | jpeg")
    ap.add_argument("--keep-pdf", action="store_true", help="Keep the intermediate PDF file")
    ap.add_argument("--out", type=Path, default=None, help="Write a JSON manifest to this path")
    args = ap.parse_args()

    docx = args.docx.resolve()
    outdir = args.outdir.resolve()

    tmp_pdf_dir = outdir if args.keep_pdf else (outdir / "_tmp_pdf")
    pdf = _convert_docx_to_pdf(docx, tmp_pdf_dir)
    images = _pdf_to_images(pdf=pdf, outdir=outdir, dpi=args.dpi, fmt=args.format)

    if not args.keep_pdf:
        try:
            shutil.rmtree(tmp_pdf_dir, ignore_errors=True)
        except Exception:
            pass

    rep: dict[str, Any] = {
        "input_docx": str(docx),
        "output_dir": str(outdir),
        "dpi": args.dpi,
        "format": args.format,
        "pages": [str(p) for p in images],
        "page_count": len(images),
    }

    payload = json.dumps(rep, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
