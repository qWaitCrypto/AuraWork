#!/usr/bin/env python3
"""
Gate A (structure) validation for PDF.

For PDF, Gate A is a basic "can parse / not obviously broken" check.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _try_import_pypdf():
    try:
        from pypdf import PdfReader  # type: ignore
        return PdfReader
    except Exception:
        return None


def gate_a_validate(pdf_path: Path) -> dict[str, Any]:
    PdfReader = _try_import_pypdf()
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    is_valid_pdf = False
    page_count: int | None = None
    is_encrypted: bool | None = None

    if PdfReader is None:
        errors.append(
            {
                "part_uri": str(pdf_path),
                "error_type": "consistency",
                "message": "pypdf not available; cannot validate PDF parseability.",
            }
        )
        return {
            "ok": False,
            "scope": ["pdf"],
            "is_valid_pdf": False,
            "page_count": None,
            "is_encrypted": None,
            "errors": errors,
            "warnings": warnings,
        }

    try:
        reader = PdfReader(str(pdf_path))
        is_encrypted = bool(getattr(reader, "is_encrypted", False))
        if is_encrypted:
            try:
                rc = reader.decrypt("")
            except Exception:
                warnings.append(
                    {
                        "part_uri": str(pdf_path),
                        "error_type": "consistency",
                        "message": "PDF is encrypted; parseability may be limited.",
                    }
                )
                rc = 0

            if rc:
                try:
                    page_count = len(reader.pages)
                except Exception:
                    page_count = None
            else:
                page_count = None
                warnings.append(
                    {
                        "part_uri": str(pdf_path),
                        "error_type": "consistency",
                        "message": "PDF is encrypted; page_count not verified without password.",
                    }
                )
        else:
            page_count = len(reader.pages)
        is_valid_pdf = True
    except Exception as e:
        errors.append({"part_uri": str(pdf_path), "error_type": "consistency", "message": f"PDF parse failed: {e}"})

    return {
        "ok": len(errors) == 0,
        "scope": ["pdf"],
        "is_valid_pdf": is_valid_pdf,
        "page_count": page_count,
        "is_encrypted": is_encrypted,
        "errors": errors,
        "warnings": warnings,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Gate A validate a PDF (basic parse check).")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    rep = gate_a_validate(args.pdf)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
