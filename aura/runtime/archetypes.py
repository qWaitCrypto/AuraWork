from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SideEffectProfile(StrEnum):
    READ = "read"
    WRITE = "write"
    WRITE_DELETE = "write/delete"
    NET_READ = "net-read"


class Archetype(StrEnum):
    FILE_OPS_WORKER = "FileOpsWorker"
    DOC_WORKER = "DocWorker"
    SHEET_WORKER = "SheetWorker"
    WEB_READ_WORKER = "WebReadWorker"
    VERIFIER_WORKER = "VerifierWorker"


@dataclass(frozen=True, slots=True)
class ArchetypeSpec:
    archetype: Archetype
    side_effect_profile: SideEffectProfile
    tool_whitelist: frozenset[str]


# Tool names below intentionally match the design document (§8.1) vocabulary.
# The runtime tool registry may use different names; mapping/bridging is handled elsewhere.
ARCHETYPES: dict[Archetype, ArchetypeSpec] = {
    Archetype.FILE_OPS_WORKER: ArchetypeSpec(
        archetype=Archetype.FILE_OPS_WORKER,
        side_effect_profile=SideEffectProfile.WRITE_DELETE,
        tool_whitelist=frozenset(
            {
                "file_scan",
                "file_move",
                "file_rename",
                "file_copy",
                "file_delete",
                "hash_compute",
                "index_generate",
            }
        ),
    ),
    Archetype.DOC_WORKER: ArchetypeSpec(
        archetype=Archetype.DOC_WORKER,
        side_effect_profile=SideEffectProfile.WRITE,
        tool_whitelist=frozenset(
            {
                "template_render",
                "text_generate",
                "diff_compute",
                "doc_write",
            }
        ),
    ),
    Archetype.SHEET_WORKER: ArchetypeSpec(
        archetype=Archetype.SHEET_WORKER,
        side_effect_profile=SideEffectProfile.WRITE,
        tool_whitelist=frozenset(
            {
                "vision_extract",
                "ocr_extract",
                "field_map",
                "validate",
                "sheet_write",
            }
        ),
    ),
    Archetype.WEB_READ_WORKER: ArchetypeSpec(
        archetype=Archetype.WEB_READ_WORKER,
        side_effect_profile=SideEffectProfile.NET_READ,
        tool_whitelist=frozenset(
            {
                "web_fetch",
                "screenshot",
                "content_extract",
                "cache_save",
            }
        ),
    ),
    Archetype.VERIFIER_WORKER: ArchetypeSpec(
        archetype=Archetype.VERIFIER_WORKER,
        side_effect_profile=SideEffectProfile.READ,
        tool_whitelist=frozenset(
            {
                "file_read",
                "diff_compute",
                "validate",
                "report_generate",
            }
        ),
    ),
}


def get_archetype_spec(archetype: Archetype) -> ArchetypeSpec:
    try:
        return ARCHETYPES[archetype]
    except KeyError as e:
        raise KeyError(f"Unknown archetype: {archetype}") from e


def validate_allowed_tools_subset(*, archetype: Archetype, allowed_tools_subset: list[str] | None) -> None:
    """Validate that a planner-provided subset is within the archetype whitelist."""

    if allowed_tools_subset is None:
        return
    whitelist = get_archetype_spec(archetype).tool_whitelist
    for tool in allowed_tools_subset:
        if tool not in whitelist:
            raise ValueError(
                f"Tool '{tool}' is not allowed for archetype {archetype.value}. "
                f"Allowed: {', '.join(sorted(whitelist))}"
            )
