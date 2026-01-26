#!/usr/bin/env python3
"""
Plan loading/validation for aura_pptx.

This is intentionally simple and strict:
- op types are a closed set
- required fields are validated per-op
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SUPPORTED_OPS = {"replace_text", "fill_placeholder", "add_slide", "delete_slide", "reorder_slides"}
PATCH_OPS = {"replace_text", "fill_placeholder", "delete_slide", "reorder_slides", "duplicate_slide"}
CREATE_OPS = {
    "create_deck",
    "set_slide_size",
    "add_title",
    "add_textbox",
    "add_shape",
    "add_image",
    "set_slide_bg",
    "add_notes",
}

# NOTE: duplicate_slide is patch-mode structural duplication (OOXML-level best-effort).
SUPPORTED_OPS = SUPPORTED_OPS | {"duplicate_slide"} | CREATE_OPS


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

        if k == "replace_text":
            if not isinstance(op.get("slide_id"), int) or op["slide_id"] < 1:
                raise ValueError(f"Operation #{i} (replace_text) requires integer slide_id >= 1.")
            if not isinstance(op.get("find"), str) or not op["find"]:
                raise ValueError(f"Operation #{i} (replace_text) requires non-empty 'find'.")
            if not isinstance(op.get("replace"), str):
                raise ValueError(f"Operation #{i} (replace_text) requires 'replace' (string).")
            if "shape_id" in op and not isinstance(op.get("shape_id"), str):
                raise ValueError(f"Operation #{i} (replace_text) optional 'shape_id' must be a string.")

        elif k == "fill_placeholder":
            if not isinstance(op.get("slide_id"), int) or op["slide_id"] < 1:
                raise ValueError(f"Operation #{i} (fill_placeholder) requires integer slide_id >= 1.")
            if not isinstance(op.get("placeholder_id"), str) or not op["placeholder_id"]:
                raise ValueError(f"Operation #{i} (fill_placeholder) requires non-empty 'placeholder_id'.")
            if not isinstance(op.get("content"), str):
                raise ValueError(f"Operation #{i} (fill_placeholder) requires 'content' (string).")

        elif k == "add_slide":
            if not isinstance(op.get("after_slide_id"), int) or op["after_slide_id"] < 0:
                raise ValueError(f"Operation #{i} (add_slide) requires integer after_slide_id >= 0.")
            has_layout = isinstance(op.get("layout"), str) and op["layout"]
            has_layout_idx = isinstance(op.get("layout_idx"), int) and op["layout_idx"] >= 0
            if not (has_layout or has_layout_idx):
                raise ValueError(f"Operation #{i} (add_slide) requires 'layout' (name) or 'layout_idx' (int >= 0).")

        elif k == "delete_slide":
            if not isinstance(op.get("slide_id"), int) or op["slide_id"] < 1:
                raise ValueError(f"Operation #{i} (delete_slide) requires integer slide_id >= 1.")

        elif k == "reorder_slides":
            order = op.get("order")
            if not isinstance(order, list) or not order:
                raise ValueError(f"Operation #{i} (reorder_slides) requires non-empty 'order' list.")
            if not all(isinstance(x, int) and x >= 1 for x in order):
                raise ValueError(f"Operation #{i} (reorder_slides) 'order' must be a list of integers >= 1.")

        elif k == "duplicate_slide":
            if not isinstance(op.get("slide_id"), int) or op["slide_id"] < 1:
                raise ValueError(f"Operation #{i} (duplicate_slide) requires integer slide_id >= 1.")
            if "after_slide_id" in op and not isinstance(op.get("after_slide_id"), int):
                raise ValueError(f"Operation #{i} (duplicate_slide) optional after_slide_id must be an integer.")
            if isinstance(op.get("after_slide_id"), int) and op["after_slide_id"] < 0:
                raise ValueError(f"Operation #{i} (duplicate_slide) after_slide_id must be >= 0.")

        elif k == "create_deck":
            size = op.get("slide_size")
            if size is not None:
                if not isinstance(size, dict):
                    raise ValueError(f"Operation #{i} (create_deck) optional 'slide_size' must be an object.")
                w = size.get("width_in")
                h = size.get("height_in")
                if not isinstance(w, (int, float)) or not isinstance(h, (int, float)) or w <= 0 or h <= 0:
                    raise ValueError(f"Operation #{i} (create_deck) slide_size.width_in/height_in must be > 0.")

        elif k == "set_slide_size":
            w = op.get("width_in")
            h = op.get("height_in")
            if not isinstance(w, (int, float)) or not isinstance(h, (int, float)) or w <= 0 or h <= 0:
                raise ValueError(f"Operation #{i} (set_slide_size) requires width_in/height_in > 0.")

        elif k == "add_title":
            if not isinstance(op.get("slide_id"), int) or op["slide_id"] < 1:
                raise ValueError(f"Operation #{i} (add_title) requires integer slide_id >= 1.")
            if not isinstance(op.get("text"), str):
                raise ValueError(f"Operation #{i} (add_title) requires 'text' (string).")

        elif k == "add_textbox":
            if not isinstance(op.get("slide_id"), int) or op["slide_id"] < 1:
                raise ValueError(f"Operation #{i} (add_textbox) requires integer slide_id >= 1.")
            if not isinstance(op.get("text"), str):
                raise ValueError(f"Operation #{i} (add_textbox) requires 'text' (string).")
            for key in ("x", "y", "w", "h"):
                val = op.get(key)
                if not isinstance(val, (int, float)) or val < 0:
                    raise ValueError(f"Operation #{i} (add_textbox) requires '{key}' >= 0 (inches).")

        elif k == "add_shape":
            if not isinstance(op.get("slide_id"), int) or op["slide_id"] < 1:
                raise ValueError(f"Operation #{i} (add_shape) requires integer slide_id >= 1.")
            for key in ("x", "y", "w", "h"):
                val = op.get(key)
                if not isinstance(val, (int, float)) or val < 0:
                    raise ValueError(f"Operation #{i} (add_shape) requires '{key}' >= 0 (inches).")

        elif k == "add_image":
            if not isinstance(op.get("slide_id"), int) or op["slide_id"] < 1:
                raise ValueError(f"Operation #{i} (add_image) requires integer slide_id >= 1.")
            if not isinstance(op.get("path"), str) or not op["path"]:
                raise ValueError(f"Operation #{i} (add_image) requires non-empty 'path'.")
            for key in ("x", "y", "w", "h"):
                val = op.get(key)
                if not isinstance(val, (int, float)) or val < 0:
                    raise ValueError(f"Operation #{i} (add_image) requires '{key}' >= 0 (inches).")

        elif k == "set_slide_bg":
            if not isinstance(op.get("slide_id"), int) or op["slide_id"] < 1:
                raise ValueError(f"Operation #{i} (set_slide_bg) requires integer slide_id >= 1.")
            if not isinstance(op.get("color"), str) or not op["color"]:
                raise ValueError(f"Operation #{i} (set_slide_bg) requires 'color' (hex string).")

        elif k == "add_notes":
            if not isinstance(op.get("slide_id"), int) or op["slide_id"] < 1:
                raise ValueError(f"Operation #{i} (add_notes) requires integer slide_id >= 1.")
            if not isinstance(op.get("text"), str):
                raise ValueError(f"Operation #{i} (add_notes) requires 'text' (string).")

    return plan


def classify_plan(plan: dict[str, Any]) -> dict[str, Any]:
    ops = plan.get("operations", [])
    kinds = [op.get("op") for op in ops if isinstance(op, dict)]
    is_patch_supported = all(k in PATCH_OPS for k in kinds)
    has_structural_ops = any(k in {"add_slide", "delete_slide", "reorder_slides", "duplicate_slide"} for k in kinds)
    create_ops = [k for k in kinds if k in CREATE_OPS]
    is_create = bool(create_ops)
    return {
        "ops": kinds,
        "is_patch_supported": is_patch_supported,
        "has_structural_ops": has_structural_ops,
        "is_create": is_create,
        "create_ops": create_ops,
    }
