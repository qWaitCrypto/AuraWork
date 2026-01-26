# aura_docx — python-docx guide (creating new documents)

Use this guide when you need to **create a brand-new `.docx`** (not just edit a template).

Precondition: `python-docx` must already be available in the runtime environment.

## Minimal example (create a new DOCX)

1) Create a file `make_docx.py`:

```python
from docx import Document

doc = Document()
doc.add_heading("Report", level=1)
doc.add_paragraph("Hello from aura_docx.")

table = doc.add_table(rows=2, cols=2)
table.cell(0, 0).text = "Key"
table.cell(0, 1).text = "Value"
table.cell(1, 0).text = "Client"
table.cell(1, 1).text = "ACME"

doc.save("output.docx")
```

2) Run:

`python make_docx.py`

## Recommended validation loop

Even for newly-created docs, validate the package:

`python "$SKILL_ROOT/scripts/validate.py" output.docx --out artifacts/docx_gate_a.json`

If you later apply a `plan.json`, follow the closed-loop editing flow in `SKILL.md`.
