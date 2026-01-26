#!/usr/bin/env python3
"""
Dependency-light OOXML helpers for XLSX.

This module avoids `openpyxl` so the skill can still:
- analyze workbook structure (best-effort)
- extract sheet values (best-effort)
- scan cached formula error tokens (best-effort)

Notes:
- Values are read from the XLSX XML parts. Dates/times are *not* converted because
  that requires number format resolution (styles.xml). We return raw numbers.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import xml.etree.ElementTree as ET

S_NS: Dict[str, str] = {
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

REL_NS: Dict[str, str] = {
    "rels": "http://schemas.openxmlformats.org/package/2006/relationships",
}

ERROR_TOKENS = {"#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#N/A", "#NUM!", "#NULL!"}

A1_RE = re.compile(r"^([A-Z]+)([0-9]+)$")


def _read_xml_from_zip(zf: zipfile.ZipFile, name: str) -> ET.Element:
    return ET.fromstring(zf.read(name))


def _col_to_int(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - 64)
    return n


def _int_to_col(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def parse_a1(a1: str) -> Tuple[int, int]:
    m = A1_RE.match(a1)
    if not m:
        raise ValueError(f"Invalid A1 reference: {a1}")
    return _col_to_int(m.group(1)), int(m.group(2))


def parse_range_ref(ref: str) -> Tuple[int, int, int, int]:
    # "B2:D10" or "A1"
    if ":" in ref:
        a, b = ref.split(":", 1)
    else:
        a, b = ref, ref
    c1, r1 = parse_a1(a)
    c2, r2 = parse_a1(b)
    return min(c1, c2), min(r1, r2), max(c1, c2), max(r1, r2)


def _sheet_parts(zf: zipfile.ZipFile) -> List[Tuple[str, str]]:
    """
    Returns [(sheet_name, part_path)] where part_path is like "xl/worksheets/sheet1.xml".
    """
    wb = _read_xml_from_zip(zf, "xl/workbook.xml")
    rels = _read_xml_from_zip(zf, "xl/_rels/workbook.xml.rels")

    rid_to_target: Dict[str, str] = {}
    for rel in rels.findall(".//rels:Relationship", REL_NS):
        rid = rel.attrib.get("Id")
        target = rel.attrib.get("Target")
        if rid and target:
            # Targets may be:
            # - relative to xl/ (e.g. "worksheets/sheet1.xml")
            # - package-absolute (e.g. "/xl/worksheets/sheet1.xml")
            # Normalize to an in-zip path like "xl/worksheets/sheet1.xml".
            normalized = target.lstrip("/")
            if not normalized.startswith("xl/"):
                normalized = "xl/" + normalized.lstrip("/")
            rid_to_target[rid] = normalized

    out: List[Tuple[str, str]] = []
    for sh in wb.findall(".//s:sheet", S_NS):
        name = sh.attrib.get("name")
        rid = sh.attrib.get("{%s}id" % S_NS["r"])
        if name and rid and rid in rid_to_target:
            out.append((name, rid_to_target[rid]))
    return out


def get_sheet_part(zf: zipfile.ZipFile, sheet_name: str) -> str:
    for name, part in _sheet_parts(zf):
        if name == sheet_name:
            return part
    raise ValueError(f"Sheet not found in workbook: {sheet_name}")


def list_sheet_names(xlsx_path: Path) -> List[str]:
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        return [name for name, _ in _sheet_parts(zf)]


def load_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    try:
        root = _read_xml_from_zip(zf, "xl/sharedStrings.xml")
    except KeyError:
        return []
    out: List[str] = []
    for si in root.findall(".//s:si", S_NS):
        # <si><t>...</t></si> or rich text: <si><r><t>..</t></r>...</si>
        texts = []
        for t in si.findall(".//s:t", S_NS):
            if t.text:
                texts.append(t.text)
        out.append("".join(texts))
    return out


def _read_inline_string(cell_el: ET.Element) -> str:
    texts = []
    for t in cell_el.findall(".//s:is//s:t", S_NS):
        if t.text:
            texts.append(t.text)
    return "".join(texts)


def cell_cached_value(cell_el: ET.Element, shared_strings: List[str]) -> Any:
    """
    Best-effort cell value extraction (cached values).

    - Strings: shared strings (t="s") or inline strings (t="inlineStr")
    - Numbers: int/float when parseable
    - Booleans: True/False (t="b")
    - Errors: error token string (t="e") or '#REF!'... when found
    """
    t = cell_el.attrib.get("t")
    if t == "inlineStr":
        return _read_inline_string(cell_el)

    v_el = cell_el.find("s:v", S_NS)
    raw = v_el.text if v_el is not None else None
    if raw is None:
        return None

    if t == "s":
        try:
            return shared_strings[int(raw)]
        except Exception:
            return raw
    if t == "b":
        return raw == "1"
    if t == "e":
        return raw

    # numeric or string
    if raw in ERROR_TOKENS:
        return raw
    try:
        if any(ch in raw for ch in (".", "e", "E")):
            return float(raw)
        return int(raw)
    except Exception:
        return raw


def sheet_dimension_ref(sheet_root: ET.Element) -> Optional[str]:
    dim = sheet_root.find("s:dimension", S_NS)
    if dim is not None:
        ref = dim.attrib.get("ref")
        if ref:
            return ref
    return None


def compute_dimension_from_cells(sheet_root: ET.Element) -> Optional[str]:
    min_c: Optional[int] = None
    min_r: Optional[int] = None
    max_c: Optional[int] = None
    max_r: Optional[int] = None
    for c in sheet_root.findall(".//s:c", S_NS):
        coord = c.attrib.get("r")
        if not coord:
            continue
        try:
            col, row = parse_a1(coord)
        except ValueError:
            continue
        min_c = col if min_c is None else min(min_c, col)
        min_r = row if min_r is None else min(min_r, row)
        max_c = col if max_c is None else max(max_c, col)
        max_r = row if max_r is None else max(max_r, row)
    if min_c is None or min_r is None or max_c is None or max_r is None:
        return None
    a = f"{_int_to_col(min_c)}{min_r}"
    b = f"{_int_to_col(max_c)}{max_r}"
    return a if a == b else f"{a}:{b}"


def analyze_workbook_ooxml(xlsx_path: Path) -> Dict[str, Any]:
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        sheet_summaries = []
        merges_total = 0
        dv_total = 0
        cf_total = 0
        formula_cells_total = 0

        for sheet_name, part in _sheet_parts(zf):
            root = _read_xml_from_zip(zf, part)
            dim = sheet_dimension_ref(root) or compute_dimension_from_cells(root) or "unknown"

            fcnt = len(root.findall(".//s:f", S_NS))
            formula_cells_total += fcnt

            merges = len(root.findall(".//s:mergeCell", S_NS))
            merges_total += merges

            dv = len(root.findall(".//s:dataValidation", S_NS))
            dv_total += dv

            cf_rules = len(root.findall(".//s:cfRule", S_NS))
            cf_total += cf_rules

            sheet_summaries.append(
                {
                    "title": sheet_name,
                    "dimension": dim,
                    "formula_cells": fcnt,
                    "merged_ranges": merges,
                    "data_validations": dv,
                    "conditional_format_rules": cf_rules,
                }
            )

        # Named ranges (defined names) live in xl/workbook.xml
        named_ranges = 0
        try:
            wb = _read_xml_from_zip(zf, "xl/workbook.xml")
            dn_parent = wb.find("s:definedNames", S_NS)
            if dn_parent is not None:
                named_ranges = len(dn_parent.findall("s:definedName", S_NS))
        except Exception:
            named_ranges = 0

        return {
            "file": str(xlsx_path),
            "analysis_backend": "ooxml",
            "sheets": len(sheet_summaries),
            "named_ranges": named_ranges,
            "formula_cells_total": formula_cells_total,
            "merged_ranges_total": merges_total,
            "data_validations_total": dv_total,
            "conditional_format_rules_total": cf_total,
            "sheet_summaries": sheet_summaries,
        }


def scan_error_cells_ooxml(xlsx_path: Path, *, max_cells: int = 2_000_000) -> Tuple[int, Dict[str, int], Dict[str, List[str]]]:
    """
    Scan cached cell values for Excel error tokens.

    This is used after LibreOffice recalculation (authoritative) but also works
    as a best-effort scan on existing cached values.
    """
    total = 0
    by_type: Dict[str, int] = {}
    samples: Dict[str, List[str]] = {}

    with zipfile.ZipFile(xlsx_path, "r") as zf:
        shared = load_shared_strings(zf)
        scanned = 0

        for sheet_name, part in _sheet_parts(zf):
            root = _read_xml_from_zip(zf, part)
            for cell in root.findall(".//s:c", S_NS):
                scanned += 1
                if scanned > max_cells:
                    raise RuntimeError(f"Scan limit exceeded ({max_cells} cells). Narrow the used range or increase --max-cells.")
                coord = cell.attrib.get("r") or "?"
                v = cell_cached_value(cell, shared)
                if isinstance(v, str) and v in ERROR_TOKENS:
                    total += 1
                    by_type[v] = by_type.get(v, 0) + 1
                    samples.setdefault(v, [])
                    if len(samples[v]) < 50:
                        samples[v].append(f"{sheet_name}!{coord}")

    return total, by_type, samples


def extract_sheet_grid(
    xlsx_path: Path,
    *,
    sheet: str,
    range_ref: Optional[str] = None,
    max_cells: int = 200_000,
) -> Dict[str, Any]:
    """
    Extract a rectangular grid of cached values from a sheet.
    """
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        part = get_sheet_part(zf, sheet)
        root = _read_xml_from_zip(zf, part)
        shared = load_shared_strings(zf)

        ref = range_ref or sheet_dimension_ref(root) or compute_dimension_from_cells(root)
        if not ref:
            ref = "A1:A1"

        min_c, min_r, max_c, max_r = parse_range_ref(ref.replace("$", ""))
        rows = max_r - min_r + 1
        cols = max_c - min_c + 1
        if rows * cols > max_cells:
            raise RuntimeError(f"Requested range too large ({rows}x{cols}={rows*cols} cells). Use --range or increase --max-cells.")

        # Sparse map (row, col) -> value
        sparse: Dict[Tuple[int, int], Any] = {}
        for cell in root.findall(".//s:c", S_NS):
            coord = cell.attrib.get("r")
            if not coord:
                continue
            try:
                c, r = parse_a1(coord.replace("$", ""))
            except ValueError:
                continue
            if not (min_c <= c <= max_c and min_r <= r <= max_r):
                continue
            sparse[(r, c)] = cell_cached_value(cell, shared)

        grid: List[List[Any]] = []
        for r in range(min_r, max_r + 1):
            row_vals: List[Any] = []
            for c in range(min_c, max_c + 1):
                row_vals.append(sparse.get((r, c)))
            grid.append(row_vals)

        return {
            "file": str(xlsx_path),
            "sheet": sheet,
            "range": ref,
            "rows": rows,
            "cols": cols,
            "grid": grid,
        }
