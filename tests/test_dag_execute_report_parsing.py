from __future__ import annotations

from aura.runtime.tools.dag_execute import DAGExecuteNextTool


def test_parse_report_reads_top_level_report() -> None:
    parsed = DAGExecuteNextTool._parse_report({"report": {"status": "completed", "items": [1]}})
    assert parsed == {"status": "completed", "items": [1]}


def test_parse_report_falls_back_to_data_report() -> None:
    parsed = DAGExecuteNextTool._parse_report({"data": {"report": {"status": "failed", "error": "x"}}})
    assert parsed == {"status": "failed", "error": "x"}


def test_parse_report_prefers_top_level_report_over_data_report() -> None:
    parsed = DAGExecuteNextTool._parse_report(
        {
            "report": {"status": "completed"},
            "data": {"report": {"status": "failed"}},
        }
    )
    assert parsed == {"status": "completed"}
