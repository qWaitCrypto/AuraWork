---
name: aura-xlsx
description: Microsoft Excel / XLSX (.xlsx) template-safe filling, editing, and validation for office spreadsheets, including set-cells/ranges, fill named ranges, append rows, and formula error checks. Use when the user mentions Excel/XLSX spreadsheets, template filling, data entry, financial models, or formulas.
---

# Aura XLSX (Tutorial-style)

## Overview
Use this skill for **.xlsx only**. It edits an existing workbook by applying a constrained `plan.json`, then validates structure (Gate A) and (when formulas exist) validates formula results via recalculation (Gate B).

Core idea: **closed-loop** and **template-safe**.
- Prefer value-only OOXML patching when possible (minimizes risk of dropping charts/pivots/controls).
- Always Gate A validate the package after edits.
- If formulas exist, Gate B is required to claim correctness.

## 🎯 Decision tree (start here)
- Create a new `.xlsx` from scratch → **Flow 0**
- Read / inspect structure / check formula presence → **Flow 1**
- Fill an existing template (recommended) → **Flow 2**
- Need structural edits (rows/cols/table append) → **Flow 3**
- Need to certify formula correctness → **Flow 4**

## MANDATORY (must follow when editing)
- Create an `artifacts/` folder and write all JSON artifacts there.
- Do not claim “formula correctness” if Gate B is skipped/unavailable.
- Preserve existing templates: do not impose new formatting/style conventions unless the user explicitly asks.
- Batch changes: keep each `plan.json` small (3–10 related ops).

## ✅ Quickstart (recommended for subagents: one command for the full loop)
`python "$SKILL_ROOT/scripts/run.py" input.xlsx plan.json --artifacts-dir artifacts`

Note: commands use `$SKILL_ROOT` (provided by Aura when the skill is loaded).

It writes a unique run folder under `artifacts/`:
- `output.xlsx`
- `plan.json`, `analysis.json`, `apply_report.json`, `gate_a.json`, `gate_b.json`, `report.json`

Read the printed JSON summary:
- `ok: true` means all required gates passed (`overall_status: success`)
- `overall_status: skipped_gate_b` means formulas exist but Gate B was skipped (do not claim formula correctness)

---

## 📖 Flow 0: Create a new workbook (from scratch)
Use this when there is no template and you want a clean `.xlsx`.

Minimal example:
```json
{
  "meta": {"id": "new-xlsx"},
  "operations": [
    {"op": "create_workbook", "sheets": ["Dashboard", "Data"], "active_sheet": "Dashboard"},
    {"op": "define_named_ranges", "names": {"ClientName": "Dashboard!$B$2", "ReportDate": "Dashboard!$B$3"}},
    {"op": "set_cells", "sheet": "Dashboard", "cells": [{"cell": "A1", "value": "XLSX Showcase"}]}
  ]
}
```

Run (no input file; use `-`):
`python "$SKILL_ROOT/scripts/run.py" - plan.json --artifacts-dir artifacts`

---

## 📖 Flow 1: Read/analyze
Analyze structure & risk:
`python "$SKILL_ROOT/scripts/analyze.py" input.xlsx --out artifacts/xlsx_analysis.json`

(Optional) Extract a range for inspection (cached values):
`python "$SKILL_ROOT/scripts/xlsx_cli.py" extract input.xlsx --sheet "Sheet1" --range "A1:D20" --format csv --out artifacts/sheet1.csv`

---

## 📖 Flow 2: Closed-loop editing (recommended)
### Write `plan.json`
Minimal example:
```json
{
  "meta": {"id": "task-001", "notes": "Fill Q1 template"},
  "operations": [
    {"op": "fill_named_ranges", "values": {"ClientName": "ACME"}},
    {"op": "set_cells", "sheet": "Summary", "cells": [{"cell": "B5", "value": 1250000}]}
  ],
  "constraints": {"require_zero_formula_errors": true}
}
```

Recommended (one-shot runner):
`python "$SKILL_ROOT/scripts/run.py" input.xlsx plan.json --artifacts-dir artifacts`

Advanced (debugging only; step-by-step artifacts):
- `python "$SKILL_ROOT/scripts/analyze.py" input.xlsx --out artifacts/xlsx_analysis.json`
- `python "$SKILL_ROOT/scripts/apply.py" input.xlsx plan.json output.xlsx --mode auto --out artifacts/xlsx_apply_report.json`
- `python "$SKILL_ROOT/scripts/validate.py" output.xlsx --apply-report artifacts/xlsx_apply_report.json --out artifacts/xlsx_gate_a.json`
- `python "$SKILL_ROOT/scripts/recalc.py" output.xlsx --out artifacts/xlsx_gate_b.json`
- `python "$SKILL_ROOT/scripts/report.py" plan.json --analysis artifacts/xlsx_analysis.json --input input.xlsx --output output.xlsx --apply-report artifacts/xlsx_apply_report.json --gate-a artifacts/xlsx_gate_a.json --gate-b artifacts/xlsx_gate_b.json --out artifacts/xlsx_report.json`

---

## 📖 Flow 3: Structural and styling changes (complex spreadsheets)
Supports structural edits and advanced styling via `openpyxl`.

Available ops (see `docs/plan_spec.md`):
- `insert_rows`, `insert_cols`, `append_table_rows`, `copy_range`
- `merge_cells`, `set_style` (font/fill/border/align), `set_column_width`

Example:
```json
{
  "operations": [
    {"op": "merge_cells", "sheet": "Sheet1", "range": "A1:C1"},
    {"op": "set_style", "sheet": "Sheet1", "range": "A1:C1", "style": {"font": {"bold": true, "size": 14}, "fill": {"color": "FFFF00"}}}
  ]
}
```

Usage (same as Flow 2):
`python "$SKILL_ROOT/scripts/apply.py" input.xlsx plan.json output.xlsx --mode auto`

---

## 📖 Flow 4: Formula correctness (Gate B)
Gate B is required when formulas exist and `constraints.require_zero_formula_errors` is true.

- If Gate B returns `skipped: true`, do **not** claim correctness.
- If Gate B returns `ok: false`, treat as `failed_gate_b`.

### Gate B output format (core fields)
Gate B output includes `required/skipped/ok` and, when recalculation is available, returns formula statistics and error cells:
```json
{
  "required": true,
  "skipped": false,
  "ok": true,
  "formula_count": 150,
  "formula_errors": 0,
  "error_cells": []
}
```

Failure example:
```json
{
  "required": true,
  "skipped": false,
  "ok": false,
  "formula_count": 150,
  "formula_errors": 2,
  "error_cells": [
    {"sheet": "Sheet1", "cell": "E10", "error_type": "#REF!"},
    {"sheet": "Sheet1", "cell": "F15", "error_type": "#DIV/0!"}
  ]
}
```

Skipped example (LibreOffice not available):
```json
{
  "required": true,
  "skipped": true,
  "skip_reason": "LibreOffice not available",
  "ok": null,
  "warning": "⚠️ Gate B skipped; formula results are not certified."
}
```

Common error triage (quick pointers)
- `#REF!`: referenced cells were deleted/moved → check formula ranges and table names
- `#DIV/0!`: division by zero → check inputs/assumptions and whether a zero denominator is allowed
- `#VALUE!`: type mismatch → check cell types/formats (e.g., numbers written as strings)

---

## ⚠️ Best practices
### Core rule: formula-first
**Always use Excel formulas; do not compute in Python and hard-code results.**

❌ WRONG (hard-coded computed values; the model loses dynamic behavior)
```json
{
  "operations": [
    {"op": "set_cells", "sheet": "Summary", "cells": [{"cell": "B10", "value": 5000}]}
  ]
}
```

✅ CORRECT (write/keep formulas and let Excel calculate; `--mode auto` detects formula writes and switches to `openpyxl`, or you can explicitly use `--mode openpyxl`)
```json
{
  "operations": [
    {"op": "set_cells", "sheet": "Summary", "cells": [{"cell": "B10", "value": "=SUM(B2:B9)"}]}
  ]
}
```

- Prefer value-only patching for workbooks with charts/pivots/controls.
- Avoid overwriting formula cells in patch mode.
- Validate early and often: Gate A after every batch.

## 📊 Financial model conventions (optional)
If the task is a finance model (budget/forecast/DCF) and the workbook already follows conventions, preserve them:
- Blue = inputs, Black = formulas, Green = cross-sheet links, Yellow = key assumptions
- Prefer assumptions in dedicated cells and reference them (`=B5*(1+$B$6)`) instead of hardcoding constants (`=B5*1.05`)
- If you hardcode an input from an external source, record provenance in an adjacent note cell (source / as-of date / reference / link).

## 📚 References (read as needed)
- `docs/plan_spec.md` — plan.json schema (XLSX ops)
- `docs/safety.md` — preservation notes (charts/pivots/controls + formula caveats)
- `docs/formula_rules.md` — “do not hardcode” and formula construction rules (recommended)
- `docs/financial_model_guide.md` — financial model conventions (optional)
- `docs/formula_checklist.md` — formula verification checklist (optional)

## Output expectations
- Return the final `.xlsx`.
- Return `artifacts/.../report.json` (the run folder path is printed by `run.py`).
- Summarize: operations, touched parts, Gate A/B status, and any risks.

## Output expectations
- Return the final `.xlsx`.
- Return `artifacts/xlsx_report.json`.
- Summarize: operations, touched parts, Gate A/Gate B status, and any risks.
