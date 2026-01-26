# aura_docx — plan.json spec (DOCX ops)

This skill applies a constrained `plan.json` to an existing `.docx` package via `scripts/apply.py`.

## Top-level shape

```json
{
  "meta": {
    "id": "task-001",
    "notes": "Human-readable note"
  },
  "operations": [
    {"op": "replace_text", "find": "A", "replace": "B", "scope": "all"}
  ]
}
```

- `meta` is optional (but strongly recommended for audit/reporting).
- `operations` is required.

## Anchors

Many ops reference a location using `anchor`:

- `heading:Introduction` — first paragraph styled as a heading whose visible text matches
- `bookmark:MyBookmark` — paragraph containing a `w:bookmarkStart` named `MyBookmark`
- `paragraph_id:p12` — the 12th `<w:p>` found in `word/document.xml` traversal order
- `after:p12` — same as `paragraph_id`, but semantically “insert after”

Recommendation:
- Prefer `heading:...` or `bookmark:...` anchors when possible.
- Use `paragraph_id:pN` only when you must (positional anchors are fragile).

## Supported operations (DOCX)

### 1) `replace_text`

```json
{"op":"replace_text","find":"{{x}}","replace":"ACME","scope":"all"}
```

- `scope`: `all | first | heading | paragraph_id`
- If `scope` is `paragraph_id`, also provide:
  - `"paragraph_id": "p12"` (recommended), or
  - `"anchor": "paragraph_id:p12"` (compatible)

Notes:
- This is a text-run level replacement. It preserves most run formatting unless the match spans across runs.

### 2) `fill_placeholder`

Alias of `replace_text` (same fields).

### 3) `insert_paragraph`

```json
{"op":"insert_paragraph","anchor":"heading:Summary","position":"after","text":"...","style":"Normal"}
```

- `position`: `before | after`
- `style` is optional (Word style name).

### 4) `add_comment`

```json
{"op":"add_comment","anchor":"paragraph_id:p12","text":"Please review","author":"AI"}
```

Notes:
- Adds `word/comments.xml` if missing and links it via relationships/content-types when possible.

### 5) `tracked_change`

```json
{"op":"tracked_change","type":"delete","anchor":"paragraph_id:p12","old_text":"30 days","new_text":"60 days","author":"AI"}
```

- `type`: `delete | insert`

Limitations:
- Best-effort only. Reliable when `old_text` appears inside a single text run.

---

## Creation Operations (use with `docx_create.py` or `--mode create`)

These ops can create a document from scratch. Use when no template exists.

### 6) `add_heading`

```json
{"op":"add_heading","text":"Services Agreement","level":1,"alignment":"center"}
```

- `level`: 0-9 (0 = Title, 1 = Heading 1, etc.)
- `alignment`: `left | center | right | justify` (optional)

### 7) `add_paragraph`

```json
{"op":"add_paragraph","text":"This agreement is entered into by both parties.","style":"Normal","bold":false,"alignment":"left"}
```

### 8) `add_line_break`

```json
{"op":"add_line_break"}
```

### 9) `create_table`

```json
{
  "op":"create_table",
  "rows":3,
  "cols":4,
  "data":[
    ["Party A (Client)","Name:","",""],
    ["","Address:","",""],
    ["","Contact:","Phone:",""]
  ],
  "header_row":true,
  "col_widths":[4,3,3,5]
}
```

- `data`: 2D array of cell texts
- `header_row`: if true, first row is bold
- `col_widths`: list of widths in cm

### 10) `merge_table_cells`

```json
{"op":"merge_table_cells","table":-1,"start_row":0,"start_col":0,"end_row":2,"end_col":0}
```

- `table`: table index (-1 = last table)

### 11) `style_table_cell`

```json
{"op":"style_table_cell","table":-1,"row":0,"col":0,"bg_color":"DDDDDD","bold":true,"alignment":"center"}
```

### 12) `set_table_borders`

```json
{"op":"set_table_borders","table":-1,"style":"single","size":4,"color":"000000"}
```

### 13) `add_page_break`

```json
{"op":"add_page_break"}
```
