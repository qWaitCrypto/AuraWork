#!/usr/bin/env python3
"""
Plan loading/validation/classification for aura_xlsx.

We intentionally keep this simple and strict.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

VALUE_ONLY_OPS = {"set_cells", "set_range", "fill_named_ranges"}
STYLE_OPS = {"merge_cells", "set_style", "set_row_height", "set_column_width"}
CREATE_OPS = {"create_workbook", "add_sheet", "rename_sheet", "remove_sheet", "define_named_ranges", "freeze_panes"}
SUPPORTED_OPS = VALUE_ONLY_OPS | {"append_table_rows", "insert_rows", "insert_cols", "copy_range"} | STYLE_OPS | CREATE_OPS

def _is_formula_value(value: Any) -> bool:
    return isinstance(value, str) and value.lstrip().startswith("=")


def _plan_writes_formulas(plan: Dict[str, Any]) -> bool:
    for op in plan.get("operations", []):
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        if kind == "set_cells":
            cells = op.get("cells", [])
            if isinstance(cells, list):
                for item in cells:
                    if isinstance(item, dict) and _is_formula_value(item.get("value")):
                        return True
        elif kind == "set_range":
            values = op.get("values", [])
            if isinstance(values, list) and any(_is_formula_value(v) for v in values):
                return True
        elif kind == "fill_named_ranges":
            values = op.get("values", {})
            if isinstance(values, dict) and any(_is_formula_value(v) for v in values.values()):
                return True
    return False


def load_and_validate_plan(plan_path: Path) -> Dict[str, Any]:
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
        # Basic required fields by op
        if k in {"set_cells", "set_range", "insert_rows", "insert_cols", "copy_range", "append_table_rows", "freeze_panes"}:
            if "sheet" not in op:
                raise ValueError(f"Operation #{i} ({k}) requires 'sheet'.")
        if k == "create_workbook":
            sheets = op.get("sheets")
            if sheets is not None:
                if not isinstance(sheets, list) or not sheets or not all(isinstance(s, str) and s.strip() for s in sheets):
                    raise ValueError(f"Operation #{i} (create_workbook) requires non-empty 'sheets' list of strings when provided.")
        if k == "add_sheet":
            if not isinstance(op.get("name"), str) or not op.get("name").strip():
                raise ValueError(f"Operation #{i} (add_sheet) requires non-empty 'name'.")
        if k == "rename_sheet":
            if not isinstance(op.get("from"), str) or not op.get("from").strip():
                raise ValueError(f"Operation #{i} (rename_sheet) requires 'from'.")
            if not isinstance(op.get("to"), str) or not op.get("to").strip():
                raise ValueError(f"Operation #{i} (rename_sheet) requires 'to'.")
        if k == "remove_sheet":
            if not isinstance(op.get("name"), str) or not op.get("name").strip():
                raise ValueError(f"Operation #{i} (remove_sheet) requires non-empty 'name'.")
        if k == "define_named_ranges":
            names = op.get("names")
            if not isinstance(names, dict) or not names:
                raise ValueError(f"Operation #{i} (define_named_ranges) requires non-empty 'names' object.")
        if k == "set_cells":
            if "cells" not in op or not isinstance(op["cells"], list):
                raise ValueError(f"Operation #{i} (set_cells) requires 'cells' list.")
        if k == "set_range":
            if "range" not in op or "values" not in op:
                raise ValueError(f"Operation #{i} (set_range) requires 'range' and 'values'.")
        if k == "fill_named_ranges":
            if "values" not in op or not isinstance(op["values"], dict):
                raise ValueError(f"Operation #{i} (fill_named_ranges) requires 'values' object.")
        if k == "insert_rows":
            if "idx" not in op:
                raise ValueError(f"Operation #{i} (insert_rows) requires 'idx'.")
        if k == "insert_cols":
            if "idx" not in op:
                raise ValueError(f"Operation #{i} (insert_cols) requires 'idx'.")
        if k == "copy_range":
            if "src" not in op or "dst" not in op:
                raise ValueError(f"Operation #{i} (copy_range) requires 'src' and 'dst'.")
        if k == "append_table_rows":
            if "table" not in op or "rows" not in op:
                raise ValueError(f"Operation #{i} (append_table_rows) requires 'table' and 'rows'.")
    return plan

def classify_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    ops = plan.get("operations", [])
    kinds = [op.get("op") for op in ops]
    has_formula_writes = _plan_writes_formulas(plan)
    is_value_only = all(k in VALUE_ONLY_OPS for k in kinds) and not has_formula_writes
    return {
        "ops": kinds,
        "is_value_only": is_value_only,
        "has_structural_ops": not all(k in VALUE_ONLY_OPS for k in kinds),
        "has_formula_writes": has_formula_writes,
    }
