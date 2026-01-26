# OPC packaging quick reference (OOXML containers)

OPC (Open Packaging Conventions) defines the ZIP container rules shared by DOCX/XLSX/PPTX.

Key concepts:
- A package is a ZIP file containing **parts**.
- Each part has a **content type** declared in `[Content_Types].xml`.
- Parts reference other parts via **relationships** stored in `.rels` files.

## Required/typical files

- `[Content_Types].xml` (root)
- `_rels/.rels` (package-level relationships)
- `<part>/_rels/<partname>.rels` (part-level relationships)

## Content types
`[Content_Types].xml` contains:
- `<Default Extension="xml" ContentType="application/xml"/>`
- `<Override PartName="/word/document.xml" ContentType="..."/>`

Rule of thumb:
- Use `Default` for common extensions (xml, rels, png, jpeg).
- Use `Override` for specific OOXML parts (document.xml, workbook.xml, etc.).

Schemas:
- `schemas/opc/opc-contentTypes.xsd`

## Relationships
Relationships are XML files whose root is `<Relationships>` and contain one or more `<Relationship>` elements.

Common relationship targets:
- DOCX: `/word/document.xml`, `/word/styles.xml`, `/word/numbering.xml`, `/word/comments.xml`
- XLSX: `/xl/workbook.xml`, `/xl/worksheets/sheet1.xml`, `/xl/sharedStrings.xml`, `/xl/styles.xml`

Schemas:
- `schemas/opc/opc-relationships.xsd`

