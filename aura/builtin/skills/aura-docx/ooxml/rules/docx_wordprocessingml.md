# DOCX (WordprocessingML) quick reference

A DOCX file is an OPC package. The main document content lives in:

- `word/document.xml`  (schema: `schemas/ooxml/wml.xsd`)

Common supporting parts:
- `word/styles.xml`
- `word/numbering.xml`
- `word/settings.xml`
- `word/comments.xml` (if comments exist)
- `word/footnotes.xml`, `word/endnotes.xml` (if present)

## Comments: structural invariants (high-signal checks)
A comment is typically represented by:
- `w:commentRangeStart w:id="N"`
- `w:commentRangeEnd w:id="N"`
- `w:commentReference w:id="N"`
and the actual comment body in `word/comments.xml` with the same `w:id="N"`.

If these are inconsistent, Word may drop comments or show corruption repair dialogs.

## Tracked changes (revisions)
Tracked changes are represented by elements like:
- `w:ins` (insertions)
- `w:del` (deletions)

These are structural markers; correctness still depends on matching IDs and stable nesting.

Schemas:
- WordprocessingML: `schemas/ooxml/wml.xsd`
- Shared types: `schemas/ooxml/shared-*.xsd`
- MCE: `schemas/mce/mc.xsd` (for `mc:Ignorable`, `AlternateContent` usage)

