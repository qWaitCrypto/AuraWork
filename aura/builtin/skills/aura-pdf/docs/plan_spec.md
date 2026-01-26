# aura_pdf — plan.json spec (PDF ops)

This skill applies a constrained `plan.json` for PDF operations via `scripts/apply.py`.

## Top-level shape

```json
{
  "meta": {"id": "task-001", "notes": "Split pages 1-5"},
  "operations": [
    {"op": "split", "pages": "1-5", "output": "part1.pdf"}
  ]
}
```

- `meta` is optional (but recommended for audit/reporting).
- `operations` is required.
- `constraints` is optional (currently informational for PDF).

## Supported operations (PDF)

Pages spec
- Use 1-based page numbers.
- Supported forms: `"all"`, `"1-5"`, `"1,3,10-12"`.

### 1) `merge`
Merge multiple PDFs into one output file.

```json
{"op":"merge","files":["part1.pdf","part2.pdf"],"output":"merged.pdf"}
```

### 2) `split`
Extract a page range into a new PDF.

```json
{"op":"split","pages":"1-5","output":"part1.pdf"}
```

Supported `pages` forms:
- `"1-5"`
- `"1,3,10-12"`

### 3) `fill_form`
Fill an AcroForm PDF form (output is provided via CLI `--output` or optional `output` field).

```json
{"op":"fill_form","fields":{"name":"John Doe","date":"2026-01-21"}}
```

Optional:
```json
{"op":"fill_form","fields":{"name":"John Doe"},"output":"filled.pdf"}
```

### 4) `add_watermark` (overlay PDF)
Overlay a watermark PDF (first page) onto each page of the input (output via CLI `--output` or optional `output`).

```json
{"op":"add_watermark","watermark_file":"watermark.pdf"}
```

Optional:
```json
{"op":"add_watermark","watermark_file":"watermark.pdf","pages":"1-3","output":"watermarked.pdf"}
```

### 5) `extract_text`
Extract text from the PDF into a plain text file.

```json
{"op":"extract_text","output":"artifacts/pdf_text.txt"}
```

Optional:
```json
{"op":"extract_text","output":"artifacts/pdf_text.txt","pages":"1-3"}
```

### 6) `extract_tables` (best-effort)
Extract tables into JSON.

```json
{"op":"extract_tables","output":"artifacts/pdf_tables.json"}
```

Optional:
```json
{"op":"extract_tables","output":"artifacts/pdf_tables.json","pages":"1-3"}
```

### 7) `ocr_extract` (OCR text extraction; environment-dependent)
Extract text from scanned PDFs via OCR.

```json
{
  "op": "ocr_extract",
  "output": "artifacts/ocr_text.txt",
  "pages": "all",
  "lang": "chi_sim+eng",
  "dpi": 200
}
```

### 8) `encrypt`
Encrypt a PDF and write an output file.

```json
{
  "op": "encrypt",
  "user_password": "user123",
  "owner_password": "owner456",
  "output": "encrypted.pdf"
}
```

### 9) `decrypt`
Decrypt a PDF with a password and write an output file.

```json
{
  "op": "decrypt",
  "password": "mypassword",
  "output": "decrypted.pdf"
}
```

### 10) `extract_images` (best-effort)
Extract embedded images from the PDF to a folder.

```json
{
  "op": "extract_images",
  "output_dir": "artifacts/images",
  "pages": "all",
  "format": "png"
}
```

Notes:
- `format` is best-effort; if conversion is not possible, the original extracted bytes are written.

### 11) `rotate`
Rotate a subset of pages and write an output PDF.

```json
{
  "op": "rotate",
  "pages": "1,3,5",
  "angle": 90,
  "output": "rotated.pdf"
}
```

### 12) `get_metadata`
Extract PDF metadata into JSON.

```json
{
  "op": "get_metadata",
  "output": "artifacts/pdf_metadata.json"
}
```

## References
- Shared plan spec: `references/aura_plan_spec.md`
- Shared report schema: `references/aura_report_schema.md`
