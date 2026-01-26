# Financial model guide (aura_xlsx)

Use this when the workbook is a finance model (budget/forecast/DCF) and already follows modeling conventions.

## Conventions to preserve (don’t “reformat”)
This skill is designed to **preserve** templates; avoid style changes unless the user explicitly asks.

### Color coding (common convention)
| Visual cue | Meaning |
|---|---|
| Blue text | hardcoded inputs |
| Black text | formulas |
| Green text | cross-sheet links |
| Yellow fill | key assumptions |

### Number formats (common convention)
These are Excel formats you should preserve (don’t convert numbers to strings):
- Currency: `$#,##0`
- Percent: `0.0%`
- Negative: show as `(123)` rather than `-123`

## Assumptions and references
- Put assumptions in dedicated cells and reference them:
  - ✅ `=B5*(1+$B$6)`
  - ❌ `=B5*1.05`
- Prefer named ranges for key assumptions when the workbook already has them.

## Provenance notes for hardcoded inputs (recommended)
If you must hardcode an **input** from an external source (e.g., a report, email, invoice, system export), add a short provenance note in an adjacent cell so the workbook stays auditable.

Suggested note shape (use your own words):
- source: where the number comes from
- as-of: date/time of the source
- reference: page/row/record id
- link: URL/path if applicable

Example (as plain text in a neighbor cell):
`Source: FY2026 budget v2 | As-of: 2026-01-20 | Ref: Page 4, Table 2`
