# Third-Party Notices

This bundle contains XML Schema (XSD) files implementing schemas for Office Open XML / Open Packaging Conventions.

## ECMA-376 / ISO/IEC 29500 schemas

Most schemas in `ooxml/schemas/ooxml/` and `ooxml/schemas/opc/` correspond to the namespaces used by Office Open XML:
- `http://schemas.openxmlformats.org/...`

Canonical specification families:
- ECMA-376 (Office Open XML File Formats)
- ISO/IEC 29500 (Office Open XML File Formats)

You should retain attribution to the relevant standards organizations when redistributing schemas derived from those standards.

## Markup Compatibility (MCE) schema

`ooxml/schemas/mce/mc.xsd` includes an internal comment indicating it was derived from docx4j's minimal MCE schema.

docx4j is published under the Apache License 2.0 (ASLv2). If you replace this file with an upstream version,
ensure you also carry forward any required notices.

## Microsoft extension schemas

`ooxml/schemas/microsoft/*.xsd` define schemas in `http://schemas.microsoft.com/...` namespaces.
These are used by some Office documents. If you plan to redistribute or validate against these, confirm the applicable terms.

