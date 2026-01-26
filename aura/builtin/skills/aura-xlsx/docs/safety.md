# Safety & Preservation Notes

## Risk parts
Workbooks may contain features that openpyxl may not fully preserve across save:
- Charts (`xl/charts/`)
- Pivot caches/tables (`xl/pivotCache/`, `xl/pivotTables/`)
- Drawings/controls (`xl/drawings/`, `xl/ctrlProps/`, `xl/activeX/`)

If these parts are present, prefer `patch` mode for value-only updates.
Structural edits on risk workbooks should be minimized and must produce a part diff report.

## Formula correctness
Formula correctness is validated via LibreOffice recalculation + scanning cached results.
If you cannot run LibreOffice, you cannot certify zero formula errors; do not claim correctness.
