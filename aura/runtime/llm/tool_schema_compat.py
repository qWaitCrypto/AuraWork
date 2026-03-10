from __future__ import annotations
import logging

from copy import deepcopy
from typing import Any

from .types import ModelProfile, ProviderKind, ToolSpec


logger = logging.getLogger(__name__)

def adapt_tool_specs_for_profile(*, tools: list[ToolSpec], profile: ModelProfile) -> list[ToolSpec]:
    """
    Adapt declared tool parameter schemas for provider compatibility.

    Aura keeps strict schemas for runtime validation, but some providers/gateways reject
    common JSON Schema features (e.g. `oneOf`/`const`/type unions) at request time.
    """

    if profile.provider_kind is not ProviderKind.GEMINI:
        return tools

    def _strip_unsupported(obj: Any) -> None:
        if isinstance(obj, dict):
            # Remove higher-order schema constructs that are frequently unsupported.
            obj.pop("oneOf", None)
            obj.pop("anyOf", None)
            obj.pop("allOf", None)
            # Some gateways/proto JSON logs use snake_case. Be defensive.
            obj.pop("one_of", None)
            obj.pop("any_of", None)
            obj.pop("all_of", None)

            # Convert `const` to `enum` (best-effort).
            if "const" in obj:
                const_val = obj.pop("const")
                if "enum" not in obj:
                    obj["enum"] = [const_val]

            # Gemini gateways may reject JSON Schema type unions like ["string","null"].
            # Best-effort: pick the first non-null entry.
            t = obj.get("type")
            if isinstance(t, list):
                chosen = None
                for item in t:
                    if isinstance(item, str) and item != "null":
                        chosen = item
                        break
                if chosen is None:
                    chosen = "string"
                obj["type"] = chosen

            for v in list(obj.values()):
                _strip_unsupported(v)
            return

        if isinstance(obj, list):
            for v in obj:
                _strip_unsupported(v)

    from ..tools.apply_edits_tool import gemini_compatible_apply_edits_input_schema

    out: list[ToolSpec] = []
    for spec in tools:
        if spec.name == "project__apply_edits":
            schema = gemini_compatible_apply_edits_input_schema()
        else:
            schema = deepcopy(spec.input_schema) if isinstance(spec.input_schema, dict) else {"type": "object", "properties": {}}
            _strip_unsupported(schema)
            if spec.name == "subagent__run":
                # Simplify union types in context.files.items for Gemini schema compatibility.
                try:
                    ctx = schema.get("properties", {}).get("context")
                    files = ctx.get("properties", {}).get("files") if isinstance(ctx, dict) else None
                    if isinstance(files, dict) and isinstance(files.get("items"), dict):
                        files["items"] = {"type": "string"}
                except Exception:
                    logger.warning("Suppressed exception in adapt_tool_specs_for_profile.", exc_info=True)

        out.append(ToolSpec(name=spec.name, description=spec.description, input_schema=schema))
    return out

