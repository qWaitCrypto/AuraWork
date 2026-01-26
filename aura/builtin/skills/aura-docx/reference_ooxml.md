# OOXML Reference (DOCX) — Practical Notes

This reference is intentionally brief and focused on the **few OOXML parts most relevant to office workflows**.

## 1) DOCX package layout (common parts)
A `.docx` file is a ZIP archive containing (at minimum):

- `[Content_Types].xml` — content type declarations
- `_rels/.rels` — package-level relationships
- `word/document.xml` — the main document body
- `word/_rels/document.xml.rels` — relationships from the main document

Optional but common:
- `word/styles.xml` — named styles
- `word/numbering.xml` — list/numbering definitions
- `word/comments.xml` — review comments
- `word/header*.xml` / `word/footer*.xml` — headers/footers

## 2) Namespaces
WordprocessingML elements live under the `w` namespace. In scripts, keep a stable namespace map.
Do not hardcode prefixes; match by namespace URI.

## 3) Tracked changes (revisions)
Tracked changes are represented as elements wrapping content, commonly:

- `w:ins` — inserted content
- `w:del` — deleted content

Each revision typically carries metadata such as:
- an id (e.g., `w:id`)
- author and date (attributes may vary by producer)

Practical guidance:
- Prefer **minimal spans** (wrap only the exact changed text/runs).
- Avoid wrapping an entire paragraph unless the paragraph truly changed as a whole.
- Preserve run-level formatting when possible; split runs only where necessary.

### Minimal patterns (examples)

Insert:
```xml
<w:ins w:id="123" w:author="AI" w:date="2026-01-21T00:00:00Z">
  <w:r><w:t>Inserted text</w:t></w:r>
</w:ins>
```

Delete:
```xml
<w:del w:id="124" w:author="AI" w:date="2026-01-21T00:00:00Z">
  <w:r><w:delText>Deleted text</w:delText></w:r>
</w:del>
```

Notes:
- Real-world DOCX often uses additional revision metadata and rsid attributes. Prefer preserving existing attributes when editing.
- If the text you need to redline is split across multiple runs, a safe approach is to redline only the smallest run(s) you can confidently target.

## 4) Comments
Comments are stored in `word/comments.xml` and referenced from `word/document.xml` via a range:

- `w:commentRangeStart` (with an id)
- `w:commentRangeEnd` (same id)
- `w:commentReference` (same id, placed at the end of the commented range)

Consistency requirements:
- Every referenced comment id should exist in `comments.xml`
- Every `commentRangeStart` should have a matching `commentRangeEnd`
- References should not be orphaned

### Packaging requirements (when adding comments)
- `[Content_Types].xml` should include:
  - `Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"`
- `word/_rels/document.xml.rels` should include a relationship:
  - `Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"`
  - `Target="comments.xml"`

## 5) Relationships
Relationships are declared in `.rels` files. For internal targets:
- ensure the target file exists in the package after edits
- avoid breaking rId references used by hyperlinks, images, headers, footers, and comments

## 6) Editing discipline
- Keep changes local and small.
- Validate early and often.
- If you are unsure whether an edit affects relationships or numbering, validate before packing.
