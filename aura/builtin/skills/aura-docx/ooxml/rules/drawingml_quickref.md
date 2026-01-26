# DrawingML / VML quick reference

DrawingML is used for:
- shapes, charts, pictures, and drawings embedded in Word/Excel/PowerPoint

Schemas:
- DrawingML main: `schemas/ooxml/dml-main.xsd`
- Pictures: `schemas/ooxml/dml-picture.xsd`
- WordprocessingDrawing: `schemas/ooxml/dml-wordprocessingDrawing.xsd`
- SpreadsheetDrawing: `schemas/ooxml/dml-spreadsheetDrawing.xsd`
- Charts: `schemas/ooxml/dml-chart.xsd`

VML schemas exist primarily for legacy compatibility:
- `schemas/ooxml/vml-*.xsd`

Practical guidance:
- Prefer preserving drawing-related parts unless you explicitly support editing them.
- For "fill in a template" workflows, focus on writing values/text and keep drawing parts untouched.

