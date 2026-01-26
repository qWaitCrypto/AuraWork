---
name: aura-pptx
description: Microsoft PowerPoint / PPTX (.pptx) template-safe filling and editing for office slides, including text replace, placeholder fill, adding/deleting/reordering slides, plus OOXML structure validation (Gate A). Use when the user mentions PowerPoint/PPTX slides/decks/template filling.
---

# Aura PPTX (Tutorial-style)

## Overview
Use this skill for **.pptx only** (PresentationML). It edits PPTX by applying a constrained `plan.json` to the OOXML package, then validates the result (Gate A) and emits a unified `report.json`.

Core idea: **closed-loop** and **template-safe**.
- Analyze first (slides + package risk)
- Apply minimal edits (prefer placeholder/text edits)
- Validate structure (XSD + packaging consistency)
- Report with artifacts and status

## 🎯 Decision tree (start here)
- Create a new deck from scratch → **Flow 0**
- Read / inspect slides / check risks → **Flow 1**
- Fill an existing slide template (recommended) → **Flow 2**
- Reorder/delete slides → **Flow 3**
- Need to add new slides → **Flow 4** (may require `python-pptx` available in the environment)

## MANDATORY (must follow when editing)
- Write all JSON artifacts under `artifacts/` (per-run folders are preferred).
- Do not claim success until `report.json` exists and `overall_status == "success"`.
- Batch changes: keep each `plan.json` small (3–10 related ops).
- For create-from-scratch flows, read `docs/design_guide.md` before writing `plan.json`.

---

## ✅ Quickstart (recommended for subagents: one command for the full loop)
`python "$SKILL_ROOT/scripts/run.py" input.pptx plan.json --artifacts-dir artifacts`

Note: commands use `$SKILL_ROOT` (provided by Aura when the skill is loaded).

Create from scratch:
`python "$SKILL_ROOT/scripts/run.py" - plan.json --artifacts-dir artifacts`

It writes a unique run folder under `artifacts/`:
- `output.pptx`
- `plan.json`, `analysis.json`, `apply_report.json`, `gate_a.json`, `report.json`

Read the printed JSON summary and only treat it as done when `ok: true`.

---

## 📖 Flow 1: Read/analyze (structure + risk)
Analyze slide/package structure & risk:
`python "$SKILL_ROOT/scripts/analyze.py" input.pptx --out artifacts/pptx_analysis.json`

Optional (recommended for planning): extract a slide/shape inventory:
`python "$SKILL_ROOT/scripts/inventory.py" input.pptx --out artifacts/pptx_inventory.json`

Return:
- `artifacts/pptx_analysis.json`
- `artifacts/pptx_inventory.json` (if used)

---

## 📖 Flow 2: Closed-loop editing (recommended)
### Step 1) Analyze
`python "$SKILL_ROOT/scripts/analyze.py" input.pptx --out artifacts/pptx_analysis.json`

### Step 2) Write `plan.json`
Create a plan using the closed set of PPTX ops.

Minimal example (fill title placeholder + replace a template token on slide 2):
```json
{
  "meta": {"id": "task-001", "notes": "Fill PPTX template"},
  "operations": [
    {"op": "fill_placeholder", "slide_id": 1, "placeholder_id": "title", "content": "Q1 2026 Report"},
    {"op": "replace_text", "slide_id": 2, "find": "{{client_name}}", "replace": "ACME Corp"}
  ]
}
```

### Step 3) Apply
`python "$SKILL_ROOT/scripts/apply.py" input.pptx plan.json output.pptx --mode auto --out artifacts/pptx_apply_report.json`

Notes:
- Patch mode edits slide XML text runs; it is template-safe and minimal.
  It can match tokens split across adjacent runs (within a paragraph), but very fragmented text may still require placeholder-based filling.
- Structural ops (e.g. `add_slide`) may require `python-pptx` to be available.

### Step 4) Gate A validation (XSD + consistency; key parts + touched parts)
`python "$SKILL_ROOT/scripts/validate.py" output.pptx --apply-report artifacts/pptx_apply_report.json --out artifacts/pptx_gate_a.json`

### Step 5) Build unified report
`python "$SKILL_ROOT/scripts/report.py" plan.json --analysis artifacts/pptx_analysis.json --input input.pptx --output output.pptx --apply-report artifacts/pptx_apply_report.json --gate-a artifacts/pptx_gate_a.json --out artifacts/pptx_report.json`

Return:
- `output.pptx`
- `artifacts/pptx_report.json`

---

## 📖 Flow 3: Reorder/delete slides
Use this for simple deck restructuring.

Example: delete slide 4, then reorder slides:
```json
{
  "operations": [
    {"op": "delete_slide", "slide_id": 4},
    {"op": "reorder_slides", "order": [1, 3, 2]}
  ]
}
```

Template-style duplication:
```json
{
  "operations": [
    {"op": "duplicate_slide", "slide_id": 2},
    {"op": "reorder_slides", "order": [1, 2, 3]}
  ]
}
```

---

## 📖 Flow 4: Add slides (may require python-pptx)
If you must add slides, prefer using a template deck and duplicating slides manually.

If the environment provides `python-pptx`, you can use:
```json
{
  "operations": [
    {"op": "add_slide", "after_slide_id": 3, "layout": "Title and Content"}
  ]
}
```

Or choose a layout by index:
```json
{
  "operations": [
    {"op": "add_slide", "after_slide_id": 0, "layout_idx": 0}
  ]
}
```

---

## 📖 Flow 5: Thumbnail self-check (optional)
Generate slide thumbnails for visual validation (if the environment provides a PDF renderer).

`python "$SKILL_ROOT/scripts/thumbnail.py" output.pptx --out-dir artifacts/pptx_thumbs`

Return:
- `artifacts/pptx_thumbs/slide-1.png` (etc.)

---

## ⚠️ Best practices
- Prefer `fill_placeholder` for template decks; it is more stable than ad-hoc text edits.
- Keep replacements unique (use `{{tokens}}`).
- Validate after every batch (Gate A).

## 📚 References (read as needed)
- `docs/plan_spec.md` — plan.json schema (PPTX ops)
- `docs/design_guide.md` — layout, typography, and palette guidance
- `ooxml/rules/opc_packaging.md` — packaging consistency rules (Gate A)

## Output expectations
- Return the final `.pptx`.
- Return `artifacts/.../report.json` (the run folder path is printed by `run.py`).
- Summarize: operations, touched parts, Gate A status, and any risks.
## 📖 Flow 0: Create from scratch (create-from-scratch)
Use this when there is no input deck or when you need a fully new design.
This flow requires `python-pptx` to be available in the environment.

Step 1) Write `plan.json` with create ops:
```json
{
  "meta": {"id": "pptx-create-001", "notes": "Create a clean pitch deck"},
  "operations": [
    {"op": "create_deck", "slide_size": {"width_in": 13.333, "height_in": 7.5}},
    {"op": "add_slide", "after_slide_id": 0, "layout": "Title Slide"},
    {"op": "add_title", "slide_id": 1, "text": "Product Strategy 2026", "font_size": 40, "bold": true},
    {"op": "add_textbox", "slide_id": 1, "text": "Confidential", "x": 0.8, "y": 5.8, "w": 4.0, "h": 0.6, "font_size": 16},
    {"op": "add_slide", "after_slide_id": 1, "layout": "Title and Content"},
    {"op": "add_title", "slide_id": 2, "text": "Key Themes", "font_size": 32, "bold": true}
  ]
}
```

Step 2) Run:
`python "$SKILL_ROOT/scripts/run.py" - plan.json --artifacts-dir artifacts`

Return:
- `output.pptx`
- `artifacts/.../report.json`
