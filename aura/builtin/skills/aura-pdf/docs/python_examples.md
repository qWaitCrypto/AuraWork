## Python examples (reference)

This file is optional. Prefer using `scripts/run.py` with a `plan.json` for subagent execution.

### Read basic info with pypdf
```python
from pypdf import PdfReader

reader = PdfReader("input.pdf")
print("pages:", len(reader.pages))
print("encrypted:", reader.is_encrypted)
print("metadata:", dict(reader.metadata or {}))
```

### Extract text (best-effort)
```python
from pypdf import PdfReader

reader = PdfReader("input.pdf")
for i, page in enumerate(reader.pages, start=1):
    print(f"--- page {i} ---")
    print(page.extract_text() or "")
```

### Merge / split with pypdf
```python
from pypdf import PdfReader, PdfWriter

# split: pages 1-3
reader = PdfReader("input.pdf")
writer = PdfWriter()
for i in range(3):
    writer.add_page(reader.pages[i])
with open("part.pdf", "wb") as f:
    writer.write(f)
```

### Extract images with pypdf (when supported by the file)
```python
from pypdf import PdfReader

reader = PdfReader("input.pdf")
for page_no, page in enumerate(reader.pages, start=1):
    for img_no, img in enumerate(getattr(page, "images", []), start=1):
        ext = getattr(img, "extension", "bin").lstrip(".")
        with open(f"page_{page_no:04d}_{img_no:03d}.{ext}", "wb") as f:
            f.write(img.data)
```

