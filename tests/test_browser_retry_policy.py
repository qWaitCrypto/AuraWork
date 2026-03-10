from __future__ import annotations

from aura.runtime.tools.browser import _effective_step_timeout_s, _is_retryable_open_stderr, _should_retry_open_step


def test_effective_timeout_uses_open_default_when_not_explicit(monkeypatch) -> None:
    monkeypatch.setenv("AURA_BROWSER_OPEN_TIMEOUT_S", "50")
    got = _effective_step_timeout_s(
        step_argv=["open", "https://example.com"],
        timeout_s=30.0,
        timeout_explicit=False,
    )
    assert got == 50.0


def test_effective_timeout_keeps_explicit_timeout_for_open() -> None:
    got = _effective_step_timeout_s(
        step_argv=["open", "https://example.com"],
        timeout_s=12.0,
        timeout_explicit=True,
    )
    assert got == 12.0


def test_retryable_open_stderr_detects_transient_network_errors() -> None:
    msg = "page.goto: net::ERR_CONNECTION_RESET at https://apnews.com/hub/ap-top-news"
    assert _is_retryable_open_stderr(msg) is True


def test_should_retry_open_step_only_once() -> None:
    kwargs = {
        "step_argv": ["open", "https://apnews.com/"],
        "timed_out": True,
        "exit_code": -9,
        "stderr_text": "",
        "max_attempts": 2,
    }
    assert _should_retry_open_step(attempt=1, **kwargs) is True
    assert _should_retry_open_step(attempt=2, **kwargs) is False


def test_should_not_retry_non_open_step() -> None:
    assert (
        _should_retry_open_step(
            step_argv=["snapshot"],
            timed_out=True,
            exit_code=-9,
            stderr_text="timeout",
            attempt=1,
            max_attempts=2,
        )
        is False
    )
