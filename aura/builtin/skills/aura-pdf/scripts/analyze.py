#!/usr/bin/env python3
"""
Analyze a PDF file for basic structure/risk signals.

This is best-effort: if optional dependencies are missing, analysis is partial.
"""
from __future__ import annotations
import logging

import argparse
import json
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

def _try_import_pypdf():
    try:
        from pypdf import PdfReader  # type: ignore
        return PdfReader
    except Exception:
        return None


def _try_import_pdfplumber():
    try:
        import pdfplumber  # type: ignore
        return pdfplumber
    except Exception:
        return None


def _safe_str(value: Any) -> str:
    try:
        return str(value)
    except Exception:
        return ""


def _extract_metadata(reader: Any) -> dict[str, str]:
    meta: dict[str, str] = {}
    try:
        obj = getattr(reader, "metadata", None)
        if obj:
            for k, v in dict(obj).items():
                meta[_safe_str(k)] = _safe_str(v)
    except Exception:
        logger.warning("Suppressed exception in _extract_metadata.", exc_info=True)
    return meta


def _count_images_pypdf(reader: Any, *, max_pages: int = 5) -> int:
    count = 0
    try:
        pages = list(reader.pages)[:max_pages]
    except Exception:
        return 0

    for page in pages:
        try:
            images = list(getattr(page, "images"))  # type: ignore[arg-type]
            count += len(images)
            continue
        except Exception:
            logger.warning("Suppressed exception in _count_images_pypdf.", exc_info=True)

        # Fallback: best-effort resource scan
        try:
            resources = page.get("/Resources", {})  # type: ignore[attr-defined]
            xobj = resources.get("/XObject", {}) if isinstance(resources, dict) else {}
            if not isinstance(xobj, dict):
                continue
            for _name, obj in xobj.items():
                try:
                    o = obj.get_object()  # type: ignore[attr-defined]
                except Exception:
                    o = obj
                if isinstance(o, dict) and o.get("/Subtype") == "/Image":
                    count += 1
        except Exception:
            continue

    return count


def _detect_scanned(
    *,
    pdf_path: Path,
    page_count: int | None,
    images_count: int | None,
) -> tuple[bool | None, list[str]]:
    warnings: list[str] = []
    if page_count is None:
        return None, ["Unable to detect scanned PDF (unknown page count)."]

    max_pages = min(page_count, 3)
    if max_pages <= 0:
        return None, ["Unable to detect scanned PDF (empty PDF)."]

    plumber = _try_import_pdfplumber()
    if plumber is None:
        warnings.append("pdfplumber not available; scanned detection is best-effort.")

    texts: list[str] = []
    if plumber is not None:
        try:
            with plumber.open(str(pdf_path)) as pdf:
                for i in range(max_pages):
                    t = pdf.pages[i].extract_text() or ""
                    texts.append(t.strip())
        except Exception:
            warnings.append("pdfplumber failed during scanned detection; falling back to pypdf text extraction.")

    if not texts:
        PdfReader = _try_import_pypdf()
        if PdfReader is None:
            warnings.append("pypdf not available; cannot extract text for scanned detection.")
            return None, warnings
        try:
            reader = PdfReader(str(pdf_path))
            if getattr(reader, "is_encrypted", False):
                warnings.append("PDF is encrypted; scanned detection may be inaccurate.")
            for i in range(max_pages):
                try:
                    texts.append((reader.pages[i].extract_text() or "").strip())
                except Exception:
                    texts.append("")
        except Exception:
            warnings.append("pypdf failed during scanned detection.")
            return None, warnings

    has_any_text = any(t for t in texts)
    if has_any_text:
        return False, warnings

    if isinstance(images_count, int) and images_count > 0:
        return True, warnings

    return None, warnings


def analyze_pdf(pdf_path: Path) -> dict[str, Any]:
    PdfReader = _try_import_pypdf()
    pages: int | None = None
    is_encrypted: bool | None = None
    has_forms: bool | None = None
    has_images: int | None = None
    metadata: dict[str, str] = {}
    warnings: list[str] = []

    if PdfReader is None:
        warnings.append("pypdf not available; PDF analysis is partial.")
    else:
        try:
            reader = PdfReader(str(pdf_path))
            is_encrypted = bool(getattr(reader, "is_encrypted", False))
            if is_encrypted:
                try:
                    reader.decrypt("")  # best-effort
                except Exception:
                    logger.warning("Suppressed exception in analyze_pdf.", exc_info=True)
            pages = len(reader.pages)
            metadata = _extract_metadata(reader)
            has_images = _count_images_pypdf(reader)
            try:
                root = reader.trailer.get("/Root", {})
                has_forms = bool(root and root.get("/AcroForm"))
            except Exception:
                has_forms = False
        except Exception as e:
            warnings.append(f"Failed to open PDF: {e}")

    risk_level = "low"
    if is_encrypted:
        risk_level = "medium"
    elif has_forms:
        risk_level = "medium"

    is_scanned, scan_warnings = _detect_scanned(pdf_path=pdf_path, page_count=pages, images_count=has_images)
    warnings.extend(scan_warnings)

    return {
        "file": str(pdf_path),
        "format": "pdf",
        "structure": {"pages": pages, "is_encrypted": is_encrypted, "has_forms": has_forms},
        "is_scanned": is_scanned,
        "is_encrypted": is_encrypted,
        "has_forms": has_forms,
        "has_images": has_images,
        "metadata": metadata,
        "risk_summary": {
            "has_charts": False,
            "has_pivots": False,
            "has_controls": bool(has_forms),
            "has_macros": False,
            "has_formulas": False,
            "has_external_links": False,
            "risk_level": risk_level,
        },
        "warnings": warnings,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Analyze a PDF file (basic metadata/risk).")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    rep = analyze_pdf(args.pdf)
    payload = json.dumps(rep, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
