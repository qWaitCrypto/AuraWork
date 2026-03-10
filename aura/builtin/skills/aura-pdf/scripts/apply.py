#!/usr/bin/env python3
"""
Apply an aura_pdf plan.json to PDF files.

This script is intentionally plan-driven so subagents can execute deterministically
without reading implementation details.
"""
from __future__ import annotations
import logging

import argparse
import io
import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import plan as planmod


logger = logging.getLogger(__name__)

def _try_import_pypdf():
    try:
        from pypdf import PdfReader, PdfWriter  # type: ignore

        return PdfReader, PdfWriter
    except Exception:
        return None


def _try_import_pdfplumber():
    try:
        import pdfplumber  # type: ignore
        return pdfplumber
    except Exception:
        return None


def _try_import_pytesseract():
    try:
        import pytesseract  # type: ignore
        return pytesseract
    except Exception:
        return None


def _try_import_pdf2image():
    try:
        import pdf2image  # type: ignore
        return pdf2image
    except Exception:
        return None


def _parse_pages_spec(spec: str, *, total_pages: int) -> list[int]:
    """
    Parse "1-3,5,10-12" into a sorted list of 0-based page indices.
    """
    spec = (spec or "").strip()
    if not spec or spec.lower() == "all":
        return list(range(total_pages))

    out: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            a, b = token.split("-", 1)
            start = int(a.strip())
            end = int(b.strip())
            if start > end:
                start, end = end, start
            for p in range(start, end + 1):
                out.add(p - 1)
        else:
            out.add(int(token) - 1)
    return sorted([p for p in out if 0 <= p < total_pages])


def _merge(files: list[Path], output: Path) -> None:
    imported = _try_import_pypdf()
    if imported is None:
        raise RuntimeError("pypdf not available (required for merge).")
    PdfReader, PdfWriter = imported

    writer = PdfWriter()
    for f in files:
        reader = PdfReader(str(f))
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception as e:
                raise RuntimeError(f"Encrypted PDF cannot be opened: {e}") from e
        for page in reader.pages:
            writer.add_page(page)

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as fp:
        writer.write(fp)


def _split(input_pdf: Path, *, pages: str, output: Path) -> None:
    imported = _try_import_pypdf()
    if imported is None:
        raise RuntimeError("pypdf not available (required for split).")
    PdfReader, PdfWriter = imported

    reader = PdfReader(str(input_pdf))
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception as e:
            raise RuntimeError(f"Encrypted PDF cannot be opened: {e}") from e

    indices = _parse_pages_spec(pages, total_pages=len(reader.pages))
    writer = PdfWriter()
    for idx in indices:
        writer.add_page(reader.pages[idx])
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as f:
        writer.write(f)


def _fill_form(input_pdf: Path, *, fields: dict[str, Any], output: Path) -> None:
    imported = _try_import_pypdf()
    if imported is None:
        raise RuntimeError("pypdf not available (required for fill_form).")
    PdfReader, PdfWriter = imported
    try:
        from pypdf.generic import BooleanObject, NameObject  # type: ignore
    except Exception:
        BooleanObject = None  # type: ignore[assignment]
        NameObject = None  # type: ignore[assignment]

    reader = PdfReader(str(input_pdf))
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception as e:
            raise RuntimeError(f"Encrypted PDF cannot be opened: {e}") from e

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    for page in writer.pages:
        try:
            writer.update_page_form_field_values(page, fields)  # type: ignore[attr-defined]
        except Exception:
            continue

    # Preserve AcroForm and request appearance regeneration (best-effort).
    try:
        root = reader.trailer.get("/Root", {})
        acro = root.get("/AcroForm") if isinstance(root, dict) else None
        if acro is not None and NameObject is not None and BooleanObject is not None:
            writer._root_object.update({NameObject("/AcroForm"): acro})  # type: ignore[attr-defined]
            try:
                writer._root_object["/AcroForm"].update({NameObject("/NeedAppearances"): BooleanObject(True)})  # type: ignore[index]
            except Exception:
                logger.warning("Suppressed exception in _fill_form.", exc_info=True)
    except Exception:
        logger.warning("Suppressed exception in _fill_form.", exc_info=True)

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as f:
        writer.write(f)


def _add_watermark_overlay(input_pdf: Path, *, watermark_file: Path, pages: str | None, output: Path) -> None:
    imported = _try_import_pypdf()
    if imported is None:
        raise RuntimeError("pypdf not available (required for add_watermark).")
    PdfReader, PdfWriter = imported

    reader = PdfReader(str(input_pdf))
    wm = PdfReader(str(watermark_file))
    if not wm.pages:
        raise RuntimeError("watermark_file has no pages.")
    wm_page = wm.pages[0]

    indices = None
    if pages:
        indices = set(_parse_pages_spec(pages, total_pages=len(reader.pages)))

    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        if indices is None or i in indices:
            try:
                page.merge_page(wm_page)  # type: ignore[attr-defined]
            except Exception:
                logger.warning("Suppressed exception in _add_watermark_overlay.", exc_info=True)
        writer.add_page(page)

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as f:
        writer.write(f)

def _encrypt_pdf(input_pdf: Path, *, user_password: str, owner_password: str | None, output: Path) -> None:
    imported = _try_import_pypdf()
    if imported is None:
        raise RuntimeError("pypdf not available (required for encrypt).")
    PdfReader, PdfWriter = imported

    reader = PdfReader(str(input_pdf))
    if getattr(reader, "is_encrypted", False):
        raise RuntimeError("Input PDF is encrypted; decrypt it first before encrypting.")

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    try:
        writer.encrypt(user_password, owner_password=owner_password)  # type: ignore[call-arg]
    except TypeError:
        writer.encrypt(user_password, owner_password or user_password)  # type: ignore[call-arg]

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as f:
        writer.write(f)


def _decrypt_pdf(input_pdf: Path, *, password: str, output: Path) -> None:
    imported = _try_import_pypdf()
    if imported is None:
        raise RuntimeError("pypdf not available (required for decrypt).")
    PdfReader, PdfWriter = imported

    reader = PdfReader(str(input_pdf))
    if getattr(reader, "is_encrypted", False):
        try:
            rc = reader.decrypt(password)
        except Exception as e:
            raise RuntimeError(f"Decrypt failed: {e}") from e
        if not rc:
            raise RuntimeError("Decrypt failed: wrong password or unsupported encryption.")

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as f:
        writer.write(f)


def _rotate_pdf(input_pdf: Path, *, pages: str, angle: int, output: Path) -> None:
    imported = _try_import_pypdf()
    if imported is None:
        raise RuntimeError("pypdf not available (required for rotate).")
    PdfReader, PdfWriter = imported

    reader = PdfReader(str(input_pdf))
    if getattr(reader, "is_encrypted", False):
        raise RuntimeError("Encrypted PDF cannot be rotated without decrypting first.")

    indices = set(_parse_pages_spec(pages, total_pages=len(reader.pages)))
    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        if i in indices:
            try:
                page.rotate(angle)  # type: ignore[attr-defined]
            except Exception:
                try:
                    if angle >= 0:
                        turns = (angle // 90) % 4
                        for _ in range(turns):
                            page.rotate_clockwise(90)  # type: ignore[attr-defined]
                    else:
                        turns = ((-angle) // 90) % 4
                        for _ in range(turns):
                            page.rotate_counter_clockwise(90)  # type: ignore[attr-defined]
                except Exception:
                    logger.warning("Suppressed exception in _rotate_pdf.", exc_info=True)
        writer.add_page(page)

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as f:
        writer.write(f)


def _extract_images(input_pdf: Path, *, pages: str | None, output_dir: Path, fmt: str | None) -> list[Path]:
    imported = _try_import_pypdf()
    if imported is None:
        raise RuntimeError("pypdf not available (required for extract_images).")
    PdfReader, _PdfWriter = imported

    try:
        from PIL import Image  # type: ignore
    except Exception:
        Image = None  # type: ignore[assignment]

    reader = PdfReader(str(input_pdf))
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:
            raise RuntimeError("Encrypted PDF cannot be processed for image extraction (decrypt required).")

    output_dir.mkdir(parents=True, exist_ok=True)

    indices = _parse_pages_spec(pages, total_pages=len(reader.pages)) if pages else list(range(len(reader.pages)))
    extracted: list[Path] = []

    for page_index in indices:
        page = reader.pages[page_index]
        page_images = []
        try:
            page_images = list(getattr(page, "images"))  # type: ignore[arg-type]
        except Exception:
            page_images = []

        for img_idx, img in enumerate(page_images, start=1):
            data = getattr(img, "data", None)
            if not isinstance(data, (bytes, bytearray)):
                continue
            ext = getattr(img, "extension", None)
            if not isinstance(ext, str) or not ext.strip():
                ext = "bin"
            ext = ext.lstrip(".")

            raw_path = output_dir / f"page_{page_index+1:04d}_img_{img_idx:03d}.{ext}"
            raw_path.write_bytes(bytes(data))
            extracted.append(raw_path)

            if fmt and fmt.lower() != ext.lower() and Image is not None:
                try:
                    im = Image.open(io.BytesIO(bytes(data)))
                    out_path = output_dir / f"page_{page_index+1:04d}_img_{img_idx:03d}.{fmt.lower()}"
                    im.save(out_path)
                    extracted.append(out_path)
                except Exception:
                    logger.warning("Suppressed exception in _extract_images.", exc_info=True)

    return extracted


def _get_metadata(input_pdf: Path) -> dict[str, Any]:
    imported = _try_import_pypdf()
    if imported is None:
        raise RuntimeError("pypdf not available (required for get_metadata).")
    PdfReader, _PdfWriter = imported

    reader = PdfReader(str(input_pdf))
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:
            raise RuntimeError("Encrypted PDF cannot be read for metadata (decrypt required).")

    meta_obj = getattr(reader, "metadata", None)
    meta: dict[str, Any] = {}
    if meta_obj:
        try:
            for k, v in dict(meta_obj).items():
                meta[str(k)] = str(v)
        except Exception:
            logger.warning("Suppressed exception in _get_metadata.", exc_info=True)

    return {
        "page_count": len(reader.pages),
        "is_encrypted": bool(getattr(reader, "is_encrypted", False)),
        "metadata": meta,
    }


def _ocr_extract(input_pdf: Path, *, pages: str | None, output: Path, lang: str | None, dpi: int | None) -> None:
    pytesseract = _try_import_pytesseract()
    pdf2image = _try_import_pdf2image()
    if pytesseract is None or pdf2image is None:
        raise RuntimeError("OCR dependencies not available (requires pytesseract + pdf2image).")
    if shutil.which("tesseract") is None:
        raise RuntimeError("OCR runtime not available (tesseract binary not found).")

    resolved_dpi = int(dpi) if isinstance(dpi, int) and dpi > 0 else 200
    resolved_lang = lang.strip() if isinstance(lang, str) and lang.strip() else "eng"

    # pdf2image uses 1-based page numbers.
    page_numbers: list[int] | None = None
    if pages and pages.strip() and pages.strip().lower() != "all":
        # We need total pages to bound the spec; use pypdf if available.
        imported = _try_import_pypdf()
        if imported is None:
            raise RuntimeError("pypdf not available (required to OCR a page subset).")
        PdfReader, _PdfWriter = imported
        reader = PdfReader(str(input_pdf))
        if getattr(reader, "is_encrypted", False):
            raise RuntimeError("Encrypted PDF cannot be OCRed without decrypting first.")
        indices = _parse_pages_spec(pages, total_pages=len(reader.pages))
        page_numbers = [i + 1 for i in indices]

    text_chunks: list[str] = []
    try:
        if page_numbers is None:
            images = pdf2image.convert_from_path(str(input_pdf), dpi=resolved_dpi)  # type: ignore[attr-defined]
            for img in images:
                text_chunks.append(pytesseract.image_to_string(img, lang=resolved_lang))
        else:
            for p in page_numbers:
                images = pdf2image.convert_from_path(str(input_pdf), dpi=resolved_dpi, first_page=p, last_page=p)  # type: ignore[attr-defined]
                if images:
                    text_chunks.append(pytesseract.image_to_string(images[0], lang=resolved_lang))
    except Exception as e:
        raise RuntimeError(f"OCR failed: {e}") from e

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n\n".join([t.strip() for t in text_chunks if isinstance(t, str)]).strip() + "\n", encoding="utf-8")


def _extract_text(input_pdf: Path, *, pages: str | None, output: Path) -> None:
    plumber = _try_import_pdfplumber()
    if plumber is not None:
        with plumber.open(str(input_pdf)) as pdf:
            indices: Iterable[int]
            if pages:
                indices = _parse_pages_spec(pages, total_pages=len(pdf.pages))
            else:
                indices = range(len(pdf.pages))
            chunks: list[str] = []
            for i in indices:
                t = pdf.pages[i].extract_text() or ""
                chunks.append(t)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("\n\n".join(chunks).strip() + "\n", encoding="utf-8")
        return

    imported = _try_import_pypdf()
    if imported is None:
        raise RuntimeError("Neither pdfplumber nor pypdf is available (required for extract_text).")
    PdfReader, _PdfWriter = imported
    reader = PdfReader(str(input_pdf))
    indices = _parse_pages_spec(pages, total_pages=len(reader.pages)) if pages else range(len(reader.pages))
    chunks = []
    for i in indices:
        try:
            chunks.append(reader.pages[i].extract_text() or "")
        except Exception:
            chunks.append("")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n\n".join(chunks).strip() + "\n", encoding="utf-8")


def _extract_tables(input_pdf: Path, *, pages: str | None, output: Path) -> None:
    plumber = _try_import_pdfplumber()
    if plumber is None:
        raise RuntimeError("pdfplumber not available (required for extract_tables).")

    with plumber.open(str(input_pdf)) as pdf:
        indices = _parse_pages_spec(pages, total_pages=len(pdf.pages)) if pages else range(len(pdf.pages))
        pages_out: list[dict[str, Any]] = []
        for i in indices:
            try:
                tables = pdf.pages[i].extract_tables()  # type: ignore[attr-defined]
            except Exception:
                tables = []
            pages_out.append({"page": i + 1, "tables": tables})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"pages": pages_out}, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_plan(*, plan: dict[str, Any], input_pdf: Path | None, output_pdf: Path | None) -> dict[str, Any]:
    ops = [op for op in plan.get("operations", []) if isinstance(op, dict)]
    kinds = [op.get("op") for op in ops if isinstance(op.get("op"), str)]

    transform_ops = {"merge", "split", "fill_form", "add_watermark", "encrypt", "decrypt", "rotate"}
    transforms = [op for op in ops if op.get("op") in transform_ops]
    if len(transforms) > 1:
        raise ValueError("PDF plan supports at most one transform op per run (merge/split/fill_form/add_watermark/encrypt/decrypt/rotate).")

    artifacts: list[dict[str, Any]] = []
    op_results: list[dict[str, Any]] = []

    produced_pdf: Path | None = None
    engine = ""

    # Transform first (if any)
    if transforms:
        op = transforms[0]
        k = op["op"]
        if k == "merge":
            files = [Path(x) for x in op["files"]]
            out = Path(op["output"])
            _merge(files, out)
            produced_pdf = out
            engine = "pypdf"
            op_results.append({"op": k, "files": [str(x) for x in files], "output": str(out)})

        elif k == "split":
            if input_pdf is None:
                raise ValueError("split requires --input.")
            out = Path(op["output"])
            _split(input_pdf, pages=op["pages"], output=out)
            produced_pdf = out
            engine = "pypdf"
            op_results.append({"op": k, "pages": op["pages"], "output": str(out)})

        elif k == "fill_form":
            if input_pdf is None:
                raise ValueError("fill_form requires --input.")
            out = Path(op.get("output") or (str(output_pdf) if output_pdf else ""))
            if not str(out):
                raise ValueError("fill_form requires --output (or op.output).")
            _fill_form(input_pdf, fields=op["fields"], output=out)
            produced_pdf = out
            engine = "pypdf"
            op_results.append({"op": k, "fields_count": len(op.get("fields", {})), "output": str(out)})

        elif k == "add_watermark":
            if input_pdf is None:
                raise ValueError("add_watermark requires --input.")
            out = Path(op.get("output") or (str(output_pdf) if output_pdf else ""))
            if not str(out):
                raise ValueError("add_watermark requires --output (or op.output).")
            wm = Path(op["watermark_file"])
            pages = op.get("pages")
            _add_watermark_overlay(input_pdf, watermark_file=wm, pages=pages if isinstance(pages, str) else None, output=out)
            produced_pdf = out
            engine = "pypdf"
            op_results.append({"op": k, "watermark_file": str(wm), "pages": pages or "all", "output": str(out)})

        elif k == "encrypt":
            if input_pdf is None:
                raise ValueError("encrypt requires --input.")
            out = Path(op.get("output") or (str(output_pdf) if output_pdf else ""))
            if not str(out):
                raise ValueError("encrypt requires --output (or op.output).")
            owner = op.get("owner_password")
            _encrypt_pdf(
                input_pdf,
                user_password=str(op["user_password"]),
                owner_password=str(owner) if isinstance(owner, str) and owner.strip() else None,
                output=out,
            )
            produced_pdf = out
            engine = "pypdf"
            op_results.append({"op": k, "output": str(out)})

        elif k == "decrypt":
            if input_pdf is None:
                raise ValueError("decrypt requires --input.")
            out = Path(op.get("output") or (str(output_pdf) if output_pdf else ""))
            if not str(out):
                raise ValueError("decrypt requires --output (or op.output).")
            _decrypt_pdf(input_pdf, password=str(op["password"]), output=out)
            produced_pdf = out
            engine = "pypdf"
            op_results.append({"op": k, "output": str(out)})

        elif k == "rotate":
            if input_pdf is None:
                raise ValueError("rotate requires --input.")
            out = Path(op.get("output") or (str(output_pdf) if output_pdf else ""))
            if not str(out):
                raise ValueError("rotate requires --output (or op.output).")
            _rotate_pdf(input_pdf, pages=str(op["pages"]), angle=int(op["angle"]), output=out)
            produced_pdf = out
            engine = "pypdf"
            op_results.append({"op": k, "pages": op["pages"], "angle": op["angle"], "output": str(out)})

    # Extractions (can run on produced_pdf if transform happened, otherwise on input_pdf)
    base_pdf = produced_pdf or input_pdf
    for op in ops:
        k = op.get("op")
        if k not in {"extract_text", "extract_tables", "ocr_extract", "extract_images", "get_metadata"}:
            continue
        if base_pdf is None:
            raise ValueError(f"{k} requires --input (or a transform op that produces a PDF).")
        pages = op.get("pages")

        if k == "extract_text":
            out = Path(op["output"])
            _extract_text(base_pdf, pages=pages if isinstance(pages, str) else None, output=out)
            artifacts.append({"type": "extract_text", "path": str(out)})
            op_results.append({"op": k, "output": str(out)})
            engine = engine or ("pdfplumber" if _try_import_pdfplumber() is not None else "pypdf")
            continue

        if k == "extract_tables":
            out = Path(op["output"])
            _extract_tables(base_pdf, pages=pages if isinstance(pages, str) else None, output=out)
            artifacts.append({"type": "extract_tables", "path": str(out)})
            op_results.append({"op": k, "output": str(out)})
            engine = engine or "pdfplumber"
            continue

        if k == "ocr_extract":
            out = Path(op["output"])
            lang = op.get("lang")
            dpi = op.get("dpi")
            _ocr_extract(
                base_pdf,
                pages=str(pages) if isinstance(pages, str) else None,
                output=out,
                lang=str(lang) if isinstance(lang, str) else None,
                dpi=int(dpi) if isinstance(dpi, int) else None,
            )
            artifacts.append({"type": "ocr_extract", "path": str(out)})
            op_results.append({"op": k, "output": str(out), "lang": lang or "", "dpi": dpi or 200})
            engine = engine or "pytesseract+pdf2image"
            continue

        if k == "extract_images":
            out_dir = Path(op["output_dir"])
            fmt = op.get("format")
            extracted = _extract_images(
                base_pdf,
                pages=str(pages) if isinstance(pages, str) else None,
                output_dir=out_dir,
                fmt=str(fmt) if isinstance(fmt, str) and fmt.strip() else None,
            )
            artifacts.append({"type": "extract_images", "path": str(out_dir)})
            op_results.append({"op": k, "output_dir": str(out_dir), "count": len(extracted)})
            engine = engine or "pypdf"
            continue

        if k == "get_metadata":
            meta = _get_metadata(base_pdf)
            out = Path(op.get("output") or "artifacts/pdf_metadata.json")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            artifacts.append({"type": "get_metadata", "path": str(out)})
            op_results.append({"op": k, "output": str(out)})
            engine = engine or "pypdf"
            continue

    counts = Counter([k for k in kinds if isinstance(k, str)])
    change_summary = ", ".join([f"{k}×{counts[k]}" for k in sorted(counts)]) if counts else ""

    # Best-effort PDF risk summary (forms/encryption).
    has_forms = False
    encrypted = False
    imported = _try_import_pypdf()
    if imported is not None and base_pdf is not None:
        PdfReader, _PdfWriter = imported
        try:
            reader = PdfReader(str(base_pdf))
            encrypted = bool(getattr(reader, "is_encrypted", False))
            try:
                root = reader.trailer.get("/Root", {})
                has_forms = bool(root and root.get("/AcroForm"))
            except Exception:
                has_forms = False
        except Exception:
            logger.warning("Suppressed exception in apply_plan.", exc_info=True)

    risk_level = "low"
    if encrypted or has_forms:
        risk_level = "medium"

    return {
        "engine": engine or "unknown",
        "touched_parts": [],
        "operations": op_results,
        "produced_pdf": str(produced_pdf) if produced_pdf else "",
        "artifacts": artifacts,
        "change_summary": change_summary,
        "risk_summary": {
            "has_charts": False,
            "has_pivots": False,
            "has_controls": bool(has_forms),
            "has_macros": False,
            "has_formulas": False,
            "has_external_links": False,
            "risk_level": risk_level,
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Apply aura_pdf plan.json operations.")
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--input", type=Path, default=None, help="Input PDF for split/fill/extract/watermark")
    ap.add_argument("--output", type=Path, default=None, help="Output PDF for ops that write a PDF (if op.output not set)")
    ap.add_argument("--out", type=Path, default=None, help="Write apply_report.json")
    args = ap.parse_args()

    plan = planmod.load_and_validate_plan(args.plan)
    rep = apply_plan(plan=plan, input_pdf=args.input, output_pdf=args.output)

    payload = json.dumps(rep, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
