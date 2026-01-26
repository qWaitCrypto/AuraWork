#!/usr/bin/env python3
"""
Plan loading/validation for aura_pdf.

This keeps op types as a closed set, while allowing a few PDF-specific parameters.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SUPPORTED_OPS = {
    "merge",
    "split",
    "fill_form",
    "add_watermark",
    "extract_text",
    "extract_tables",
    "ocr_extract",
    "encrypt",
    "decrypt",
    "extract_images",
    "rotate",
    "get_metadata",
}


def _is_pages_spec(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_output_path(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and value > 0


def load_and_validate_plan(plan_path: Path) -> dict[str, Any]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(plan, dict):
        raise ValueError("Plan must be a JSON object.")

    ops = plan.get("operations")
    if not isinstance(ops, list) or not ops:
        raise ValueError("Plan must include a non-empty 'operations' list.")

    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            raise ValueError(f"Operation #{i} must be an object.")
        k = op.get("op")
        if k not in SUPPORTED_OPS:
            raise ValueError(f"Unsupported op '{k}' in operation #{i}. Supported: {sorted(SUPPORTED_OPS)}")

        if k == "merge":
            files = op.get("files")
            if not isinstance(files, list) or len(files) < 2 or not all(isinstance(x, str) and x for x in files):
                raise ValueError(f"Operation #{i} (merge) requires 'files' list of >=2 strings.")
            if not isinstance(op.get("output"), str) or not op["output"]:
                raise ValueError(f"Operation #{i} (merge) requires non-empty 'output'.")

        elif k == "split":
            if not isinstance(op.get("pages"), str) or not op["pages"]:
                raise ValueError(f"Operation #{i} (split) requires non-empty 'pages' string.")
            if not isinstance(op.get("output"), str) or not op["output"]:
                raise ValueError(f"Operation #{i} (split) requires non-empty 'output'.")

        elif k == "fill_form":
            fields = op.get("fields")
            if not isinstance(fields, dict) or not fields:
                raise ValueError(f"Operation #{i} (fill_form) requires non-empty 'fields' object.")
            if "output" in op and not isinstance(op.get("output"), str):
                raise ValueError(f"Operation #{i} (fill_form) optional 'output' must be a string.")

        elif k == "add_watermark":
            if not isinstance(op.get("watermark_file"), str) or not op["watermark_file"]:
                raise ValueError(f"Operation #{i} (add_watermark) requires non-empty 'watermark_file'.")
            if "pages" in op and not isinstance(op.get("pages"), str):
                raise ValueError(f"Operation #{i} (add_watermark) optional 'pages' must be a string.")
            if "output" in op and not isinstance(op.get("output"), str):
                raise ValueError(f"Operation #{i} (add_watermark) optional 'output' must be a string.")

        elif k in {"extract_text", "extract_tables"}:
            if not isinstance(op.get("output"), str) or not op["output"]:
                raise ValueError(f"Operation #{i} ({k}) requires non-empty 'output' path.")
            if "pages" in op and not _is_pages_spec(op.get("pages")):
                raise ValueError(f"Operation #{i} ({k}) optional 'pages' must be a non-empty string.")

        elif k == "ocr_extract":
            if not _is_output_path(op.get("output")):
                raise ValueError(f"Operation #{i} (ocr_extract) requires non-empty 'output' path.")
            if "pages" in op and not _is_pages_spec(op.get("pages")):
                raise ValueError(f"Operation #{i} (ocr_extract) optional 'pages' must be a non-empty string.")
            if "lang" in op and (not isinstance(op.get("lang"), str) or not op["lang"].strip()):
                raise ValueError(f"Operation #{i} (ocr_extract) optional 'lang' must be a non-empty string.")
            if "dpi" in op and not _is_positive_int(op.get("dpi")):
                raise ValueError(f"Operation #{i} (ocr_extract) optional 'dpi' must be a positive integer.")

        elif k == "encrypt":
            if not isinstance(op.get("user_password"), str) or not op["user_password"].strip():
                raise ValueError(f"Operation #{i} (encrypt) requires non-empty 'user_password'.")
            if "owner_password" in op and (not isinstance(op.get("owner_password"), str) or not op["owner_password"].strip()):
                raise ValueError(f"Operation #{i} (encrypt) optional 'owner_password' must be a non-empty string.")
            if "output" in op and not _is_output_path(op.get("output")):
                raise ValueError(f"Operation #{i} (encrypt) optional 'output' must be a non-empty string.")

        elif k == "decrypt":
            if not isinstance(op.get("password"), str) or not op["password"].strip():
                raise ValueError(f"Operation #{i} (decrypt) requires non-empty 'password'.")
            if "output" in op and not _is_output_path(op.get("output")):
                raise ValueError(f"Operation #{i} (decrypt) optional 'output' must be a non-empty string.")

        elif k == "extract_images":
            if not isinstance(op.get("output_dir"), str) or not op["output_dir"].strip():
                raise ValueError(f"Operation #{i} (extract_images) requires non-empty 'output_dir'.")
            if "pages" in op and not _is_pages_spec(op.get("pages")):
                raise ValueError(f"Operation #{i} (extract_images) optional 'pages' must be a non-empty string.")
            if "format" in op and (not isinstance(op.get("format"), str) or not op["format"].strip()):
                raise ValueError(f"Operation #{i} (extract_images) optional 'format' must be a non-empty string.")

        elif k == "rotate":
            if not _is_pages_spec(op.get("pages")):
                raise ValueError(f"Operation #{i} (rotate) requires non-empty 'pages' string.")
            angle = op.get("angle")
            if not isinstance(angle, int) or angle % 90 != 0:
                raise ValueError(f"Operation #{i} (rotate) requires integer 'angle' in multiples of 90.")
            if "output" in op and not _is_output_path(op.get("output")):
                raise ValueError(f"Operation #{i} (rotate) optional 'output' must be a non-empty string.")

        elif k == "get_metadata":
            if "output" in op and not _is_output_path(op.get("output")):
                raise ValueError(f"Operation #{i} (get_metadata) optional 'output' must be a non-empty string.")

    return plan
