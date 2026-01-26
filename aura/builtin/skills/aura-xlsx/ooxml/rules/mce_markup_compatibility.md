# MCE (Markup Compatibility and Extensibility) quick reference

MCE enables forward/backward compatibility across Office versions.

Key constructs:
- `mc:Ignorable="w14 w15 wp14"` on a root element declares namespaces that older consumers may ignore.
- `mc:AlternateContent` provides multiple representations:
  - one or more `mc:Choice Requires="..."`
  - optional `mc:Fallback`

Schema:
- `schemas/mce/mc.xsd`

Practical guidance:
- Preserve `mc:Ignorable` when round-tripping documents.
- When editing, avoid deleting `mc:AlternateContent` blocks unless you are certain you preserve a valid representation.

