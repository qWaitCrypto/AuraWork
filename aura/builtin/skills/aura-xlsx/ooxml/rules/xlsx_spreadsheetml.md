# XLSX (SpreadsheetML) quick reference

An XLSX file is an OPC package. The workbook and worksheets are typically:

- `xl/workbook.xml` (relationships to sheets)
- `xl/worksheets/sheetN.xml` (cell grid)
- `xl/sharedStrings.xml` (shared string table, if used)
- `xl/styles.xml` (number formats, fonts, fills, borders, cell XFs)
- `xl/theme/theme1.xml` (optional)

Schema:
- SpreadsheetML: `schemas/ooxml/sml.xsd`

## Cell values and types (worksheet XML)

Cells are `<c r="A1" t="...">...</c>` with:
- `r` = cell reference (A1)
- `t` = type (optional; common values: `s`, `b`, `inlineStr`, `str`, `e`)
- `v` = value element (for most scalar cases)
- `is` = inline string (when `t="inlineStr"`)
- `f` = formula element

Typical patterns:
- number: `<c r="B2"><v>123</v></c>`
- boolean: `<c r="B2" t="b"><v>1</v></c>`
- shared string: `<c r="B2" t="s"><v>5</v></c>` where 5 indexes `sharedStrings.xml`
- inline string: `<c r="B2" t="inlineStr"><is><t>hello</t></is></c>`

## Important reality: formulas are not automatically recalculated
OOXML stores the last calculated result; editing inputs does not guarantee cached results are updated.
For production workflows, use an actual spreadsheet engine (Excel/LibreOffice) to recalculate and scan for errors.

