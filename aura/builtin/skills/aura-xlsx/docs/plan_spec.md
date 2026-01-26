# aura_xlsx — plan.json spec (XLSX ops)

This skill applies a constrained `plan.json` to an existing `.xlsx` workbook via `scripts/apply.py`.

For **create-from-scratch**, use the same plan file and call:
`python "$SKILL_ROOT/scripts/run.py" - plan.json --artifacts-dir artifacts`

## Top-level shape

```json
{
  "meta": {"id": "task-001", "notes": "Fill the Q1 template"},
  "operations": [
    {"op": "fill_named_ranges", "values": {"ClientName": "ACME"}},
    {"op": "set_cells", "sheet": "Summary", "cells": [{"cell": "B5", "value": 123}]}
  ],
  "constraints": {"require_zero_formula_errors": true}
}
```

- `meta` is optional (but recommended for audit/reporting).
- `operations` is required.
- `constraints` is optional (XLSX uses it to decide Gate B expectations).

## Supported operations (XLSX)

### 0) `create_workbook`
Create a new workbook and optionally declare sheet names.
```json
{"op":"create_workbook","sheets":["Dashboard","Data"],"active_sheet":"Dashboard"}
```

### 0b) `add_sheet`
```json
{"op":"add_sheet","name":"Dashboard","index":0}
```

### 0c) `rename_sheet`
```json
{"op":"rename_sheet","from":"Sheet","to":"Dashboard"}
```

### 0d) `remove_sheet`
```json
{"op":"remove_sheet","name":"Sheet2"}
```

### 0e) `define_named_ranges`
```json
{"op":"define_named_ranges","names":{"ClientName":"Dashboard!$B$2","ReportDate":"Dashboard!$B$3"}}
```

### 0f) `freeze_panes`
```json
{"op":"freeze_panes","sheet":"Dashboard","cell":"A6"}
```

### 1) `set_cells`
```json
{"op":"set_cells","sheet":"Sheet1","cells":[{"cell":"B2","value":123},{"cell":"C2","value":"ACME"}]}
```

### 2) `set_range`
```json
{"op":"set_range","sheet":"Sheet1","range":"B2:D2","values":[1,2,3],"by_row":true}
```

### 3) `fill_named_ranges`
```json
{"op":"fill_named_ranges","values":{"ClientName":"ACME","StartDate":"2026-01-01"}}
```

### 4) `append_table_rows`
```json
{
  "op":"append_table_rows",
  "sheet":"Sheet1",
  "table":{"header_row":5,"start_col":1,"end_col":8},
  "rows":[{"A":"Item1","B":2,"C":9.9}],
  "style_from":"last_row",
  "copy_formulas":true
}
```

### 5) `insert_rows` / `insert_cols`
```json
{"op":"insert_rows","sheet":"Sheet1","idx":10,"amount":2,"carry_style":"above"}
```

### 6) `copy_range`
```json
{"op":"copy_range","sheet":"Sheet1","src":"A1:D1","dst":"A2:D2"}
```

### 7) `merge_cells`
Merge a range of cells into one.
```json
{"op":"merge_cells","sheet":"Sheet1","range":"A1:G1"}
```

### 8) `set_style`
Set font, fill, alignment, border, or number format for a cell or range.
```json
{
  "op":"set_style",
  "sheet":"Sheet1",
  "range":"A1:G1",
  "style":{
    "font":{"bold":true,"size":16,"color":"0000CC"},
    "fill":{"type":"solid","color":"FFFF00"},
    "alignment":{"horizontal":"center","vertical":"center"},
    "border":{
      "left":{"style":"thin","color":"000000"},
      "right":{"style":"thin","color":"000000"},
      "top":{"style":"thin","color":"000000"},
      "bottom":{"style":"thin","color":"000000"}
    },
    "number_format":"#,##0.00"
  }
}
```

### 9) `set_row_height`
```json
{"op":"set_row_height","sheet":"Sheet1","row":1,"height":30}
```

### 10) `set_column_width`
```json
{"op":"set_column_width","sheet":"Sheet1","column":"A","width":20}
```

## constraints (optional)

Common fields:
- `require_zero_formula_errors` (bool, default true): if true and workbook has formulas, Gate B is required.
- `no_structural_changes` (bool): if true, avoid row/col inserts and table appends.
- `no_style_changes` (bool): avoid style mutations (this skill defaults to value-only patch when possible).
