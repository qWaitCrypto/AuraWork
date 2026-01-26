# aura_pptx — plan.json spec (PPTX ops)

This skill applies a constrained `plan.json` to an existing `.pptx` deck via `scripts/apply.py`.

For **create-from-scratch**, use the same plan file and call:
`python "$SKILL_ROOT/scripts/run.py" - plan.json --artifacts-dir artifacts`

## Top-level shape

```json
{
  "meta": {"id": "task-001", "notes": "Fill the Q1 deck"},
  "operations": [
    {"op": "fill_placeholder", "slide_id": 1, "placeholder_id": "title", "content": "Q1 2026"},
    {"op": "replace_text", "slide_id": 2, "find": "{{client_name}}", "replace": "ACME"}
  ]
}
```

- `meta` is optional (but recommended for audit/reporting).
- `operations` is required.
- `constraints` is optional (currently informational for PPTX).

## Supported operations (PPTX)

### 0) `create_deck`
Create a new deck. Optionally set slide size (in inches).
```json
{"op":"create_deck","slide_size":{"width_in":13.333,"height_in":7.5}}
```

### 0b) `set_slide_size`
```json
{"op":"set_slide_size","width_in":13.333,"height_in":7.5}
```

### 0c) `add_title`
```json
{"op":"add_title","slide_id":1,"text":"Q1 Overview","font_size":36,"bold":true,"align":"left"}
```

### 0d) `add_textbox`
```json
{"op":"add_textbox","slide_id":1,"text":"Key points...","x":0.8,"y":1.8,"w":11.8,"h":2.5,"font_size":20,"align":"left"}
```

### 0e) `add_shape`
```json
{"op":"add_shape","slide_id":1,"shape":"roundRect","x":0.8,"y":4.6,"w":5.5,"h":1.2,"fill":"F2F4F8","line":"CCD0D5","text":"Status: OK"}
```

### 0f) `add_image`
```json
{"op":"add_image","slide_id":2,"path":"artifacts/chart.png","x":0.7,"y":1.5,"w":12.0,"h":5.5}
```

### 0g) `set_slide_bg`
```json
{"op":"set_slide_bg","slide_id":3,"color":"0B1220"}
```

### 0h) `add_notes`
```json
{"op":"add_notes","slide_id":3,"text":"Presenter notes go here."}
```

### 1) `replace_text`
Replace text within one slide. `shape_id` is optional; if omitted, the replacement applies to all shapes on that slide.

```json
{"op":"replace_text","slide_id":1,"shape_id":"10","find":"{{title}}","replace":"Q1 Report"}
```

Notes:
- Replacements are best-effort. The patch engine can match tokens split across adjacent OOXML text runs (within a paragraph),
  but extremely fragmented text/complex shapes may still require placeholder-based filling.

### 2) `fill_placeholder`
Fill a placeholder on a slide by placeholder type or idx.

```json
{"op":"fill_placeholder","slide_id":1,"placeholder_id":"title","content":"Annual Summary"}
```

`placeholder_id` can be:
- `"title"` (matches `title` or `ctrTitle`)
- a concrete placeholder type (e.g. `"subTitle"`, `"body"`)
- a numeric idx (e.g. `"1"`)

### 3) `delete_slide`
Remove a slide from the slide list (the slide part may remain in the package as an unused part).

```json
{"op":"delete_slide","slide_id":4}
```

### 4) `reorder_slides`
Reorder slide list by 1-based indices.

```json
{"op":"reorder_slides","order":[1,3,2]}
```

Rules:
- `order` must be a permutation of `1..N` (N = current slide count).

### 5) `duplicate_slide` (patch mode; best-effort)
Duplicate an existing slide and insert it into the deck.

```json
{"op":"duplicate_slide","slide_id":2}
```

Optionally control where it is inserted:
```json
{"op":"duplicate_slide","slide_id":2,"after_slide_id":4}
```

Notes:
- `slide_id` / `after_slide_id` are **1-based** (same convention as other PPTX ops).
- This duplicates the slide OOXML part and copies slide-level relationships.
  Duplicated slides may share targets (e.g. images/notes) — acceptable for template filling.

### 6) `add_slide` (optional)
Add a new slide after a slide id (may require `python-pptx` available in the environment).

```json
{"op":"add_slide","after_slide_id":3,"layout":"Title and Content"}
```

Or use a layout index:
```json
{"op":"add_slide","after_slide_id":0,"layout_idx":0}
```

## References
- Shared plan spec: `references/aura_plan_spec.md`
- Shared report schema: `references/aura_report_schema.md`
