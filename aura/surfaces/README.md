# `aura.surfaces` status

This package is currently a placeholder for future multi-surface adapters.

- `web_surface.py.stub` and `cloud_surface.py.stub` are design stubs only.
- The production runtime currently uses `aura.runtime.surface` + event bus wiring directly.
- Do not import these `.stub` files at runtime.

When real surface adapters are implemented, they should replace the `.stub` files with
normal Python modules and include tests plus integration docs.
