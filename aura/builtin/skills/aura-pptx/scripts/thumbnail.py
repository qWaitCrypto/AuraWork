#!/usr/bin/env python3
"""
Generate slide thumbnails from a PPTX using a headless PDF renderer.

This script expects a PDF renderer (soffice/libreoffice) and pdftoppm in the environment.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def _which(*names: str) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def _convert_to_pdf(pptx: Path, tmpdir: Path) -> Path:
    soffice = _which("soffice", "libreoffice")
    if not soffice:
        raise ValueError("Missing soffice/libreoffice in PATH. Cannot render PPTX to PDF.")
    cmd = [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(tmpdir), str(pptx)]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    candidates = list(tmpdir.glob("*.pdf"))
    if not candidates:
        raise ValueError("PDF conversion did not produce a PDF output.")
    # Prefer matching stem, otherwise take first.
    for cand in candidates:
        if cand.stem == pptx.stem:
            return cand
    return candidates[0]


def _pdf_to_images(pdf_path: Path, out_dir: Path, *, fmt: str, dpi: int) -> list[str]:
    pdftoppm = _which("pdftoppm")
    if not pdftoppm:
        raise ValueError("Missing pdftoppm in PATH. Cannot convert PDF to images.")
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / "slide"
    fmt_flag = "-png" if fmt == "png" else "-jpeg"
    cmd = [pdftoppm, "-r", str(dpi), fmt_flag, str(pdf_path), str(prefix)]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return sorted(str(p) for p in out_dir.glob(f"slide-*.{fmt}"))


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate PPTX slide thumbnails.")
    ap.add_argument("pptx", type=Path)
    ap.add_argument("--out-dir", type=Path, default=Path("artifacts/pptx_thumbs"))
    ap.add_argument("--format", choices=["png", "jpg"], default="png")
    ap.add_argument("--dpi", type=int, default=150)
    args = ap.parse_args()

    pptx = args.pptx.resolve()
    if not pptx.exists():
        raise SystemExit(f"PPTX not found: {pptx}")

    with tempfile.TemporaryDirectory(prefix="pptx_thumbs_") as tmp:
        tmpdir = Path(tmp)
        pdf_path = _convert_to_pdf(pptx, tmpdir)
        images = _pdf_to_images(pdf_path, args.out_dir.resolve(), fmt=args.format, dpi=args.dpi)

    payload = {
        "ok": True,
        "pptx": str(pptx),
        "out_dir": str(args.out_dir.resolve()),
        "format": args.format,
        "dpi": args.dpi,
        "images": images,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
