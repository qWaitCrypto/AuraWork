# Formula checklist (aura_xlsx)

Use this checklist when the workbook contains formulas (finance models, dashboards, KPI sheets).

## Before edits
- Run `scripts/analyze.py` and confirm whether formulas exist.
- Identify input/assumption cells (often blue) vs formula cells (often black).
- Prefer plans that only fill inputs and avoid touching formula cells.

## After edits (required for “correctness” claims)
- Gate A (structure): `scripts/validate.py` must return `ok: true`.
- Gate B (formulas): `scripts/recalc.py`
  - If `required: true` and `skipped: true` → report “not certified” (do not claim correctness).
  - If `ok: false` → inspect `error_cells` and fix.

## Common fixes
- `#REF!`: check broken references (deleted/moved cells, renamed sheets).
- `#DIV/0!`: check denominator inputs; handle allowed zeros per model rules.
- `#VALUE!`: check data types (don’t write numbers as strings).

