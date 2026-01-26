## CLI reference (optional)

This project primarily uses Python scripts for PDF operations. If your runtime environment includes PDF CLI tools, they can be useful for quick inspection.

### Quick inspection
```bash
pdftotext -layout input.pdf output.txt
pdfinfo input.pdf
```

### Image extraction
```bash
pdfimages -all input.pdf artifacts/images/img
```

### Merge / split (example with qpdf)
```bash
qpdf --empty --pages a.pdf b.pdf -- merged.pdf
qpdf input.pdf --pages . 1-5 -- part1.pdf
```

Notes:
- Treat these commands as optional utilities; the skill’s primary interface is `plan.json` + `scripts/run.py`.

