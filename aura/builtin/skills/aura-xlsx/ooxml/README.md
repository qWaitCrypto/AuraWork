# OOXML Schema Bundle (OPC + Open XML vocabularies)

This package is a self-contained **schema/reference bundle** intended to support agentic workflows and tooling for working with:

- **DOCX** (WordprocessingML)
- **XLSX** (SpreadsheetML)
- **PPTX** (PresentationML)
- Shared **DrawingML / VML**
- **OPC** (Open Packaging Conventions: parts, relationships, content types)
- **MCE** (Markup Compatibility and Extensibility)

It is designed for **engineering usage**:
- quick lookup via `INDEX.yml`
- optional XML validation against XSD using the included `tools/validate_part.py`

## What this bundle is (and is not)

✅ Includes:
- XSD schemas for the key OOXML vocabularies (see `ooxml/schemas/`)
- Minimal, task-oriented quick references (see `ooxml/rules/`)
- An index mapping common tasks → relevant schemas (see `ooxml/INDEX.yml`)
- A file manifest with SHA-256 hashes (see `ooxml/MANIFEST.json`)

❌ Does *not* include:
- A spreadsheet calculation engine (formula evaluation requires Excel/LibreOffice)
- Any Office rendering, layout, or pagination logic
- The full ECMA/ISO PDF specifications

## Directory layout

- `INDEX.yml` — task → schema mapping (start here)
- `rules/` — concise implementation notes for common office-edit tasks
- `schemas/`
  - `opc/` — OPC packaging XSDs (`[Content_Types].xml`, `.rels`, etc.)
  - `ooxml/` — core OpenXML vocabularies (wml/sml/pml/dml/vml + shared types)
  - `mce/` — markup-compatibility schema (`mc:Ignorable`, `AlternateContent`)
  - `microsoft/` — Microsoft extension schemas (optional; validate only if needed)

## Quick validation example

Validate a Word main document part (`word/document.xml`) against WordprocessingML schema:

```bash
python ooxml/tools/validate_part.py \
  --xsd ooxml/schemas/ooxml/wml.xsd \
  --xml /path/to/unpacked_docx/word/document.xml
```

See `ooxml/tools/validate_part.py --help` for more.

## Notes on validation reality

XSD validation is **structural**. Many Office correctness properties are **semantic** (e.g., Excel formula results),
and must be validated with an actual calculation engine.

