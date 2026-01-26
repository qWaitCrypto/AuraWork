from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True, slots=True)
class PromptTemplateContext:
    now: datetime


_TOKEN_RE = re.compile(r"\{\{\s*([A-Z_]+)(?::([^}]+))?\s*\}\}")


def _format_tz_offset(now: datetime) -> str:
    offset = now.utcoffset()
    if offset is None:
        return "UTC"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hh = total // 3600
    mm = (total % 3600) // 60
    return f"UTC{sign}{hh:02d}:{mm:02d}"


def render_prompt_template(text: str, *, now: datetime | None = None, vars: Mapping[str, str] | None = None) -> str:
    """
    Render lightweight prompt templates.

    Supported tokens:
    - {{NOW}} or {{NOW:<strftime>}}
    - {{TODAY}} or {{TODAY:<strftime>}}
    - {{TZ}} (UTC offset like UTC+08:00)
    - {{UNIX_MS}} (milliseconds since epoch)
    """

    ctx = PromptTemplateContext(now=(now or datetime.now().astimezone()))
    custom_vars: Mapping[str, str] = vars or {}

    def _replace(m: re.Match[str]) -> str:
        name = (m.group(1) or "").strip().upper()
        fmt = m.group(2)

        if name in custom_vars:
            return str(custom_vars.get(name) or "")

        if name == "NOW":
            if isinstance(fmt, str) and fmt.strip():
                try:
                    return ctx.now.strftime(fmt.strip())
                except Exception:
                    return ctx.now.isoformat(timespec="seconds")
            return ctx.now.isoformat(timespec="seconds")

        if name == "TODAY":
            d = ctx.now.date()
            if isinstance(fmt, str) and fmt.strip():
                try:
                    return d.strftime(fmt.strip())
                except Exception:
                    return d.isoformat()
            return d.isoformat()

        if name == "TZ":
            return _format_tz_offset(ctx.now)

        if name == "UNIX_MS":
            try:
                return str(int(ctx.now.timestamp() * 1000))
            except Exception:
                return "0"

        # Unknown token: keep as-is.
        return m.group(0)

    return _TOKEN_RE.sub(_replace, text)
