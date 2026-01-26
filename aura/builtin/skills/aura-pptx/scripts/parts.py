#!/usr/bin/env python3
"""
Package part inspection for PPTX (zip-based).

- list_parts: enumerate zip entries and count "risk parts"
- diff_parts: compare before/after part counts (helps detect feature drops)
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any


RISK_PREFIXES = [
    "ppt/charts/",
    "ppt/embeddings/",
    "ppt/activeX/",
    "ppt/controlProps/",
    "ppt/media/",
]


def list_parts(pptx_path: Path) -> dict[str, Any]:
    risk = {p: 0 for p in RISK_PREFIXES}
    with zipfile.ZipFile(pptx_path, "r") as zf:
        names = zf.namelist()
        for n in names:
            for pref in RISK_PREFIXES:
                if n.startswith(pref):
                    risk[pref] += 1
        # macros are file-level, not a folder prefix
        if "ppt/vbaProject.bin" in set(names):
            risk["ppt/vbaProject.bin"] = 1

    risk_present = {k: v for k, v in risk.items() if v}
    return {"parts_count": len(names), "risk_parts": risk_present, "parts_sample": names[:50]}


def diff_parts(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    b = before.get("risk_parts", {})
    a = after.get("risk_parts", {})
    keys = set(b.keys()) | set(a.keys())
    delta = {k: int(a.get(k, 0)) - int(b.get(k, 0)) for k in sorted(keys)}
    return {
        "parts_count_before": before.get("parts_count"),
        "parts_count_after": after.get("parts_count"),
        "risk_delta": {k: v for k, v in delta.items() if v != 0},
    }

