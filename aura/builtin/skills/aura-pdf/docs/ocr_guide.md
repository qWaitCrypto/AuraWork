## OCR guide (scan PDFs)

Use OCR only when the PDF is scan-based:
- `analyze.py` reports `is_scanned: true`, or
- `extract_text` produces empty/near-empty output.

### Recommended workflow
1) Run analysis:
`python "$SKILL_ROOT/scripts/analyze.py" input.pdf --out artifacts/pdf_analysis.json`

2) Run OCR on a small page subset first:
```json
{
  "operations": [
    {"op": "ocr_extract", "pages": "1-3", "output": "artifacts/ocr_text.txt", "lang": "chi_sim+eng", "dpi": 200}
  ]
}
```

3) If the result looks correct, expand pages (or set `"pages": "all"`).

### Practical notes
- OCR is slower than text extraction; always scope pages for large documents.
- OCR quality depends on DPI and language selection (`lang`).
- If OCR dependencies or runtime are unavailable, treat the task as **environment-blocked** (do not hallucinate content).

