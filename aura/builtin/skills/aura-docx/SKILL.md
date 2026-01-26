---
name: aura-docx
description: Microsoft Word / DOCX (.docx) analysis and editing via a plan-driven pipeline (analyze → plan → apply → Gate A validate → report). Use for Word/DOCX documents, contracts, reports, template filling, tracked changes, and comments when you must preserve package structure.
---

# Aura DOCX (Tutorial-style)

## Overview
Use this skill for **.docx only** (WordprocessingML). It edits DOCX by applying a constrained `plan.json` to the OOXML package, then validates the result (Gate A) and emits a unified `report.json`.

Core idea: **closed-loop** and **package-safe**.
- Analyze first (structure + risk)
- Apply only the needed changes (minimal touched parts)
- Validate structure (XSD + consistency)
- Report with artifacts and status

## 🎯 Decision tree (start here)
- Read / extract / inspect → **Flow 1**
- Create new `.docx` → **Flow 2**
- Edit existing `.docx` (template-safe, recommended) → **Flow 3**
- Strict audit (tracked changes / legal) → **Flow 4**
- Visual verify (render pages) → **Flow 5**

## MANDATORY (must follow when editing)
- Create an `artifacts/` folder and write all JSON artifacts there.
- Do not claim success until `report.json` exists and `overall_status == "success"`.
- Batch changes: keep each `plan.json` small (3–10 related ops).

## ✅ Quickstart (recommended for subagents: one command for the full loop)
`python "$SKILL_ROOT/scripts/run.py" input.docx plan.json --artifacts-dir artifacts`

Note: commands use `$SKILL_ROOT` (provided by Aura when the skill is loaded).

It writes a unique run folder under `artifacts/`:
- `output.docx`
- `plan.json`, `analysis.json`, `apply_report.json`, `gate_a.json`, `report.json`

Read the printed JSON summary and only treat it as done when `ok: true`.

---

## 📖 Flow 1: Read/analyze (structure + risk + optional text extraction)
1) Analyze structure & risk (optionally extract text in the same run):
`python "$SKILL_ROOT/scripts/analyze.py" input.docx --out artifacts/docx_analysis.json --text-out artifacts/docx_text.txt`

Return:
- `artifacts/docx_analysis.json`
- `artifacts/docx_text.txt` (optional, but recommended for summarization/search)

---

## 📖 Flow 2: Create a new document (from scratch)
Use this to create a complex DOCX from scratch without a template.

### Step 1) Write `plan.json` (Creation Ops)
Use the enhanced creation ops (see `docs/plan_spec.md`):
- `add_heading`, `add_paragraph` (with styles/alignment)
- `create_table`, `merge_table_cells`, `style_table_cell`
- `add_page_break`, `add_line_break`

Example:
```json
{
  "operations": [
    {"op": "add_heading", "text": "Services Agreement", "level": 1, "alignment": "center"},
    {"op": "add_paragraph", "text": "This agreement is made between...", "style": "Normal"},
    {"op": "create_table", "rows": 3, "cols": 2, "data": [["Party A", "Party B"], ["...", "..."]], "header_row": true},
    {"op": "style_table_cell", "table": -1, "row": 0, "col": 0, "bg_color": "E0E0E0", "bold": true}
  ]
}
```

### Step 2) Apply (Creation Mode)
Recommended (one-shot runner; start-from-scratch with `-`):
`python "$SKILL_ROOT/scripts/run.py" - plan.json --artifacts-dir artifacts`

Notes:
- Passing `-` as the input means “no template; create a new doc”.
- If you do have a template, pass it instead of `-`.


---

## 📖 Flow 3: Closed-loop editing (recommended)
### Write `plan.json`
Create a plan using the closed set of DOCX ops. Keep it small and precise.

Minimal example:
```json
{
  "meta": {"id": "task-001", "notes": "Fill template placeholders"},
  "operations": [
    {"op": "replace_text", "find": "{{client_name}}", "replace": "ACME Corp", "scope": "all"},
    {"op": "insert_paragraph", "anchor": "after:p1", "position": "after", "text": "New paragraph.", "style": "Normal"},
    {"op": "add_comment", "anchor": "paragraph_id:p1", "text": "Please review this section.", "author": "AI Assistant"}
  ]
}
```

Recommended (one-shot runner):
`python "$SKILL_ROOT/scripts/run.py" input.docx plan.json --artifacts-dir artifacts`

Advanced (debugging only; step-by-step artifacts):
- `python "$SKILL_ROOT/scripts/analyze.py" input.docx --out artifacts/docx_analysis.json`
- `python "$SKILL_ROOT/scripts/apply.py" input.docx plan.json output.docx --out artifacts/docx_apply_report.json`
- `python "$SKILL_ROOT/scripts/validate.py" output.docx --apply-report artifacts/docx_apply_report.json --out artifacts/docx_gate_a.json`
- `python "$SKILL_ROOT/scripts/report.py" plan.json --analysis artifacts/docx_analysis.json --input input.docx --output output.docx --apply-report artifacts/docx_apply_report.json --gate-a artifacts/docx_gate_a.json --out artifacts/docx_report.json`

---

## 📖 Flow 4: Redlining (strict mode: tracked changes/comments)
Use this for legal/contract/government docs where the change history matters.

Core rules:
- Mark **minimal spans** only (avoid “replace the whole paragraph”).
- Preserve formatting: do not flatten runs if you can avoid it.
- Batch small: 3–10 related redlines per plan, validate after each batch.

Supported via `apply.py`:
- `tracked_change` (`type`: `delete | insert`) — best-effort, only reliable when `old_text` is contained in a single text run.
- `add_comment` — anchors comments to a paragraph/range.

If the strict redline cannot be expressed safely with the plan ops:
- Use the manual OOXML workflow: `unpack.py` → edit XML → `pack.py` → `validate.py`.
- MANDATORY: read `reference_ooxml.md` fully before editing raw XML.

---

## 📖 Flow 5: Render-to-images validation (visual verification)
Use this when you need a human-auditable snapshot (before/after pages).

Run:
`python "$SKILL_ROOT/scripts/doc_to_images.py" input.docx --outdir artifacts/input_pages`
`python "$SKILL_ROOT/scripts/doc_to_images.py" output.docx --outdir artifacts/output_pages`

---

## ⚠️ Best practices
- Batching: group related edits; avoid mixing unrelated changes in one plan.
- Minimal edits: prefer `replace_text` scoped to a region; avoid broad “all” if risk is high.
- Choose anchors carefully:
  - ✅ Prefer `heading:Title` (stable)
  - ⚠️ `paragraph_id:pN` is positional; it changes if the document structure changes
- Gate A scope: key parts + `touched_parts` from `apply_report.json`.

Examples:
- ❌ Non-unique find + global replace: `{"op":"replace_text","find":"ACME","replace":"...","scope":"all"}`
- ✅ Unique placeholder replace: `{"op":"replace_text","find":"{{client_name}}","replace":"...","scope":"all"}`

---

## 📚 References (read as needed)
- `docs/plan_spec.md` — plan.json schema and DOCX ops
- `reference_ooxml.md` — OOXML / redlining practical notes

## Output expectations
- Return the modified `.docx`.
- Return `artifacts/.../report.json` (the run folder path is printed by `run.py`).
- Summarize: operations, touched parts, Gate A status, and any risks.
