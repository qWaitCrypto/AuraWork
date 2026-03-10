from __future__ import annotations

from aura.runtime.subagents.approver import _DECISION_RE, _decision_from_obj, _extract_best_approval_json, _normalize_decision


def test_normalize_decision_accepts_expected_values_only() -> None:
    assert _normalize_decision(" allow ") == "allow"
    assert _normalize_decision("require_user") == "require_user"
    assert _normalize_decision("DENY") == "deny"
    assert _normalize_decision("blocked") is None
    assert _normalize_decision(None) is None


def test_decision_from_obj_supports_direct_and_nested_fields() -> None:
    assert _decision_from_obj({"decision": "allow"}) == "allow"
    assert _decision_from_obj({"output": {"decision": "deny"}}) == "deny"
    assert _decision_from_obj({"output": {"decision": "invalid"}}) is None


def test_extract_best_approval_json_prefers_scored_decision_object() -> None:
    text = """
noise before
{"foo": "bar"}
{"output": {"decision": "deny"}, "reason": "dangerous"}
{"decision": "allow", "reason": "safe enough", "safety_notes": ["checked"]}
noise after
"""
    best = _extract_best_approval_json(text)
    assert isinstance(best, dict)
    assert _decision_from_obj(best) == "allow"


def test_decision_regex_fallback_matches_embedded_json_fragment() -> None:
    match = _DECISION_RE.search('assistant: "decision":"require_user"')
    assert match is not None
    assert match.group(1).lower() == "require_user"
