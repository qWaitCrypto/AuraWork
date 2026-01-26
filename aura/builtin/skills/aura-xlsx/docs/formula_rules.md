# Formula rules (aura_xlsx)

## Core rule: formulas over hardcoded results
If a cell is a derived value, **do not compute it in Python and write the number**. Prefer:
- Keep the existing Excel formulas untouched, and only fill **input/assumption** cells
- Or (when you must create/change formulas) write an Excel formula string (e.g. `"=SUM(B2:B9)"`)

Why: hardcoded results break spreadsheet dynamics; inputs change but totals stay stale.

## Plan examples (wrong vs correct)
❌ WRONG (hardcode a derived total):
```json
{
  "operations": [
    {"op": "set_cells", "sheet": "Summary", "cells": [{"cell": "B10", "value": 5000}]}
  ]
}
```

✅ CORRECT (keep it dynamic with a formula):
```json
{
  "operations": [
    {"op": "set_cells", "sheet": "Summary", "cells": [{"cell": "B10", "value": "=SUM(B2:B9)"}]}
  ]
}
```

## Engine behavior (patch vs openpyxl)
- Patch mode is value-only and **will not create formulas** (formula strings become plain text).
- Auto mode routes to `openpyxl` if the plan writes formula strings (`value` starts with `=`).
- Patch mode also refuses to overwrite existing formula cells (safety guard).

## Gate B: formula correctness
If formulas exist, run Gate B:
`python "$SKILL_ROOT/scripts/recalc.py" output.xlsx --out artifacts/xlsx_gate_b.json`

Interpretation:
- `skipped: true` → cannot certify formula correctness
- `ok: false` → fix formula/input issues; do not ship as “correct”

