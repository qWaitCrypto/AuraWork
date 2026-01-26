---
name: aura-pdf
description: PDF office operations via a plan-driven pipeline (analyze → plan → apply → Gate A validate → report), including text/table extraction, merge/split, forms/watermark, encryption/decryption, OCR (if available), image extraction, rotation, and metadata. Use for PDFs, scanned documents, OCR, merging/splitting, text/table extraction, form filling, watermarking, and encryption.
---

# Aura PDF (Tutorial-style)

## Overview
Use this skill for **.pdf only**. It performs constrained PDF operations using a plan-driven workflow and emits a unified `report.json`.

Core idea: **closed-loop** and **explicit artifacts**.
- Write a small `plan.json` (closed set of ops)
- Execute the operation(s) (prefer the one-shot runner)
- Validate parseability (Gate A for PDF is a basic parse check + basic signals)
- Emit a single `report.json` that summarises what happened

## 🎯 Decision tree (start here)
- Read / inspect (scanned? encrypted? forms?) → **Flow 0**
- Extract text/tables (text-based PDF) → **Flow 1**
- Extract text from scanned PDF (OCR) → **Flow 1b**
- Merge PDFs into one → **Flow 2**
- Split a PDF by pages → **Flow 3**
- Fill a PDF form / apply watermark / rotate pages → **Flow 4**
- Encrypt / decrypt → **Flow 5**
- Extract images → **Flow 6**
- Get metadata → **Flow 7**

## MANDATORY (must follow during execution)
- Write all JSON/text artifacts under `artifacts/` (per-run folders are preferred).
- Do not claim success until `report.json` exists and `overall_status == "success"`.
- Keep each `plan.json` small (1–5 ops; prefer 1 op unless you really need a chain).
- Do not install dependencies at runtime; treat missing capabilities as environment-blocked and report it.

---

## ✅ Quickstart (recommended for subagents: one command for the full loop)
`python "$SKILL_ROOT/scripts/run.py" input.pdf plan.json --artifacts-dir artifacts`

Note: commands use `$SKILL_ROOT` (provided by Aura when the skill is loaded).

It writes a unique run folder under `artifacts/`:
- `output.pdf` (only if the plan produces a PDF)
- `plan.json`, `analysis.json`, `apply_report.json`, `gate_a.json`, `report.json`
- plus any op artifacts (text/tables/images/ocr/metadata) declared by the plan

Read the printed JSON summary and only treat it as done when `ok: true`.

---

## 📖 Flow 0: Read/analyze (start here)
Analyze structure & risk signals:
`python "$SKILL_ROOT/scripts/analyze.py" input.pdf --out artifacts/pdf_analysis.json`

Use the analysis to decide:
- If `is_encrypted: true` → consider **Flow 5** (decrypt) first.
- If `is_scanned: true` or `extract_text` returns empty → use **Flow 1b** (OCR).

---

## 📖 Flow 1: Extract text/tables
### Extract text
```json
{
  "operations": [
    {"op": "extract_text", "output": "artifacts/pdf_text.txt"}
  ]
}
```

Run (recommended):
`python "$SKILL_ROOT/scripts/run.py" input.pdf plan.json --artifacts-dir artifacts`

### Extract tables (best-effort)
```json
{
  "operations": [
    {"op": "extract_tables", "output": "artifacts/pdf_tables.json"}
  ]
}
```

Run (recommended):
`python "$SKILL_ROOT/scripts/run.py" input.pdf plan.json --artifacts-dir artifacts`

---

## 📖 Flow 1b: OCR extraction (scanned PDFs)
Use this when `analyze.py` reports `is_scanned: true` or `extract_text` returns empty.

```json
{
  "operations": [
    {
      "op": "ocr_extract",
      "output": "artifacts/ocr_text.txt",
      "pages": "all",
      "lang": "chi_sim+eng",
      "dpi": 200
    }
  ]
}
```

Run (recommended):
`python "$SKILL_ROOT/scripts/run.py" scanned.pdf plan.json --artifacts-dir artifacts`

Notes:
- OCR is environment-dependent (needs OCR runtime). If unavailable, treat as blocked.
- Always scope pages for large PDFs (e.g. `"1-3"`), then expand.

---

## 📖 Flow 2: Merge PDFs
```json
{
  "operations": [
    {"op": "merge", "files": ["a.pdf", "b.pdf"], "output": "merged.pdf"}
  ]
}
```

Run (recommended): `run.py` still needs an `in_pdf`. For merge, pass the first file (also used for analysis).
`python "$SKILL_ROOT/scripts/run.py" a.pdf plan.json --artifacts-dir artifacts`

---

## 📖 Flow 3: Split PDFs
```json
{
  "operations": [
    {"op": "split", "pages": "1-5", "output": "part1.pdf"}
  ]
}
```

Run (recommended):
`python "$SKILL_ROOT/scripts/run.py" input.pdf plan.json --artifacts-dir artifacts`

---

## 📖 Flow 4: Fill forms / watermark / rotate
### Fill form (AcroForm)
```json
{
  "operations": [
    {"op": "fill_form", "fields": {"name": "John Doe", "date": "2026-01-21"}}
  ]
}
```

Run (recommended):
`python "$SKILL_ROOT/scripts/run.py" input.pdf plan.json --artifacts-dir artifacts`

### Add watermark (overlay PDF)
```json
{
  "operations": [
    {"op": "add_watermark", "watermark_file": "watermark.pdf"}
  ]
}
```

Run the same as fill_form.

### Rotate pages
```json
{
  "operations": [
    {"op": "rotate", "pages": "1,3,5", "angle": 90, "output": "rotated.pdf"}
  ]
}
```

---

## 📖 Flow 5: Encrypt / decrypt
### Encrypt
```json
{
  "operations": [
    {"op": "encrypt", "user_password": "user123", "owner_password": "owner456", "output": "encrypted.pdf"}
  ]
}
```

### Decrypt
```json
{
  "operations": [
    {"op": "decrypt", "password": "mypassword", "output": "decrypted.pdf"}
  ]
}
```

Run (recommended):
`python "$SKILL_ROOT/scripts/run.py" input.pdf plan.json --artifacts-dir artifacts`

---

## 📖 Flow 6: Extract images (best-effort)
```json
{
  "operations": [
    {"op": "extract_images", "output_dir": "artifacts/images", "pages": "all"}
  ]
}
```

---

## 📖 Flow 7: Metadata
```json
{
  "operations": [
    {"op": "get_metadata", "output": "artifacts/pdf_metadata.json"}
  ]
}
```

Run (recommended):
`python "$SKILL_ROOT/scripts/run.py" input.pdf plan.json --artifacts-dir artifacts`

---

## 📋 Quick Reference (task → op)
| Task | plan.json op |
|------|--------------|
| Extract text | `extract_text` |
| Extract tables | `extract_tables` |
| OCR (scanned PDFs) | `ocr_extract` |
| Merge PDFs | `merge` |
| Split PDFs | `split` |
| Fill forms | `fill_form` |
| Add watermark | `add_watermark` |
| Encrypt/decrypt | `encrypt` / `decrypt` |
| Extract images | `extract_images` |
| Rotate pages | `rotate` |
| Metadata | `get_metadata` |

## ⚠️ Best practices
- Always prefer extraction first for scanned/unknown PDFs (it reveals whether the PDF is text-based).
- Keep merge/split plans as single-op for reliability.
- Treat table extraction as best-effort (verify outputs manually if important).
- If the PDF is encrypted, do not guess passwords; request the password or treat as blocked.

## 📚 References (read as needed)
- `docs/plan_spec.md` — plan.json schema (PDF ops + skill-specific fields)
- `docs/python_examples.md` — pypdf/pdfplumber best-effort snippets
- `docs/ocr_guide.md` — OCR workflow and pitfalls
- `docs/cli_reference.md` — optional CLI utilities (if available)
- Shared report schema: `references/aura_report_schema.md`

## Output expectations
- Return the output `.pdf` (if the op produces one).
- Return `artifacts/.../report.json` (the run folder path is printed by `run.py`).
- If extracting: also return the extraction artifact path declared in the plan.

## Advanced (only use when debugging)
Run the loop step-by-step (debug / inspection):
`FINAL.pdf` = the produced output PDF (or the original input for extract-only plans).
`python "$SKILL_ROOT/scripts/analyze.py" input.pdf --out artifacts/pdf_analysis.json`
`python "$SKILL_ROOT/scripts/apply.py" --plan plan.json --input input.pdf --out artifacts/pdf_apply_report.json`
`python "$SKILL_ROOT/scripts/validate.py" FINAL.pdf --out artifacts/pdf_gate_a.json`
`python "$SKILL_ROOT/scripts/report.py" plan.json --input input.pdf --output FINAL.pdf --apply-report artifacts/pdf_apply_report.json --gate-a artifacts/pdf_gate_a.json --out artifacts/pdf_report.json`
