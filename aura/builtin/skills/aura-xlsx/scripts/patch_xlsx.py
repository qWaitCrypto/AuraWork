#!/usr/bin/env python3
"""
Value-only OOXML patcher for XLSX.

Goal: preserve advanced workbook features by avoiding openpyxl save, which can drop parts.
This patcher:
- edits worksheet XML cells by coordinate (A1 notation)
- writes strings as inlineStr, numbers as <v> with no type
- supports only value-only ops: set_cells, set_range, fill_named_ranges

Limitations:
- does not preserve rich text runs inside a cell; writes plain text
- does not update number formats or styles
- does not support structural edits (rows/cols insertion) or formula rewrites
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

import xml.etree.ElementTree as ET

NS = {
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

def _read_xml_from_zip(zf: zipfile.ZipFile, name: str) -> ET.ElementTree:
    data = zf.read(name)
    return ET.ElementTree(ET.fromstring(data))

def _write_xml_bytes(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)

def _sheet_name_to_part(zf: zipfile.ZipFile, sheet_name: str) -> str:
    """
    Map sheet display name -> worksheet part path (e.g., xl/worksheets/sheet1.xml).
    """
    wb = _read_xml_from_zip(zf, "xl/workbook.xml").getroot()
    # Map r:id to target via workbook rels
    rels = _read_xml_from_zip(zf, "xl/_rels/workbook.xml.rels").getroot()
    rid_to_target: Dict[str, str] = {}
    for rel in rels.findall(".//{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"):
        rid = rel.attrib.get("Id")
        target = rel.attrib.get("Target")
        if rid and target:
            normalized = target.lstrip("/")
            if not normalized.startswith("xl/"):
                normalized = "xl/" + normalized.lstrip("/")
            rid_to_target[rid] = normalized

    for sh in wb.findall(".//s:sheet", NS):
        name = sh.attrib.get("name")
        rid = sh.attrib.get("{%s}id" % NS["r"])
        if name == sheet_name and rid in rid_to_target:
            return rid_to_target[rid]
    raise ValueError(f"Sheet not found in workbook.xml: {sheet_name}")

def _ensure_cell(row_el: ET.Element, coord: str) -> ET.Element:
    # Find or create <c r="A1">
    for c in row_el.findall("s:c", NS):
        if c.attrib.get("r") == coord:
            return c
    c = ET.SubElement(row_el, "{%s}c" % NS["s"])
    c.set("r", coord)
    return c

def _ensure_row(sheet_root: ET.Element, row_idx: int) -> ET.Element:
    sheetData = sheet_root.find("s:sheetData", NS)
    if sheetData is None:
        sheetData = ET.SubElement(sheet_root, "{%s}sheetData" % NS["s"])
    # Find row by r attr
    for row in sheetData.findall("s:row", NS):
        if row.attrib.get("r") == str(row_idx):
            return row
    # Create row, keep sorted insertion minimal: append then Excel will reorder; acceptable.
    row = ET.SubElement(sheetData, "{%s}row" % NS["s"])
    row.set("r", str(row_idx))
    return row

def _set_cell_value(cell_el: ET.Element, value: Any) -> None:
    # Remove children we control
    for child in list(cell_el):
        cell_el.remove(child)
    if value is None:
        # blank cell: remove type and value
        cell_el.attrib.pop("t", None)
        return
    if isinstance(value, bool):
        cell_el.attrib["t"] = "b"
        v = ET.SubElement(cell_el, "{%s}v" % NS["s"])
        v.text = "1" if value else "0"
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        cell_el.attrib.pop("t", None)
        v = ET.SubElement(cell_el, "{%s}v" % NS["s"])
        v.text = str(value)
        return
    # default: string
    cell_el.attrib["t"] = "inlineStr"
    is_el = ET.SubElement(cell_el, "{%s}is" % NS["s"])
    t_el = ET.SubElement(is_el, "{%s}t" % NS["s"])
    t_el.text = str(value)

A1_RE = re.compile(r"^([A-Z]+)([0-9]+)$")

def _assert_no_formula(cell_el: ET.Element, *, sheet: str, coord: str) -> None:
    if cell_el.find("s:f", NS) is not None:
        raise ValueError(
            f"Refusing to overwrite formula cell {sheet}!{coord} in patch mode. "
            "Use --mode openpyxl for formula rewrites."
        )

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

def _parse_range(rng: str) -> Tuple[int, int, int, int]:
    # "B2:D10"
    a, b = rng.split(":")
    ma = A1_RE.match(a)
    mb = A1_RE.match(b)
    if not ma or not mb:
        raise ValueError(f"Invalid range: {rng}")
    c1, r1 = ma.group(1), int(ma.group(2))
    c2, r2 = mb.group(1), int(mb.group(2))
    min_c = min(_col_to_int(c1), _col_to_int(c2))
    max_c = max(_col_to_int(c1), _col_to_int(c2))
    min_r = min(r1, r2)
    max_r = max(r1, r2)
    return min_c, min_r, max_c, max_r

def apply_value_only_patch(in_xlsx: Path, plan: Dict[str, Any], out_xlsx: Path) -> List[str]:
    out_xlsx.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(in_xlsx, "r") as zin:
        # Prepare in-memory modified parts
        modified: Dict[str, bytes] = {}

        def load_sheet(sheet_name: str) -> ET.Element:
            part = _sheet_name_to_part(zin, sheet_name)
            if part in modified:
                root = ET.fromstring(modified[part])
                return root
            tree = _read_xml_from_zip(zin, part)
            return tree.getroot()

        def save_sheet(sheet_name: str, root: ET.Element) -> None:
            part = _sheet_name_to_part(zin, sheet_name)
            modified[part] = _write_xml_bytes(root)

        # Apply operations
        for op in plan.get("operations", []):
            kind = op.get("op")
            if kind == "set_cells":
                sheet = op["sheet"]
                root = load_sheet(sheet)
                for item in op.get("cells", []):
                    coord = item["cell"]
                    m = A1_RE.match(coord)
                    if not m:
                        raise ValueError(f"Invalid cell coordinate: {coord}")
                    col, row = m.group(1), int(m.group(2))
                    row_el = _ensure_row(root, row)
                    cell_el = _ensure_cell(row_el, coord)
                    _assert_no_formula(cell_el, sheet=sheet, coord=coord)
                    _set_cell_value(cell_el, item.get("value"))
                save_sheet(sheet, root)

            elif kind == "set_range":
                sheet = op["sheet"]
                rng = op["range"]
                values = op.get("values", [])
                by_row = bool(op.get("by_row", True))
                min_c, min_r, max_c, max_r = _parse_range(rng)
                idx = 0
                root = load_sheet(sheet)
                if by_row:
                    for r in range(min_r, max_r + 1):
                        for c in range(min_c, max_c + 1):
                            if idx >= len(values):
                                break
                            coord = f"{_int_to_col(c)}{r}"
                            row_el = _ensure_row(root, r)
                            cell_el = _ensure_cell(row_el, coord)
                            _assert_no_formula(cell_el, sheet=sheet, coord=coord)
                            _set_cell_value(cell_el, values[idx])
                            idx += 1
                else:
                    for c in range(min_c, max_c + 1):
                        for r in range(min_r, max_r + 1):
                            if idx >= len(values):
                                break
                            coord = f"{_int_to_col(c)}{r}"
                            row_el = _ensure_row(root, r)
                            cell_el = _ensure_cell(row_el, coord)
                            _assert_no_formula(cell_el, sheet=sheet, coord=coord)
                            _set_cell_value(cell_el, values[idx])
                            idx += 1
                save_sheet(sheet, root)

            elif kind == "fill_named_ranges":
                # For named ranges, we need to resolve them. This requires parsing definedNames in workbook.xml.
                # We implement a minimal resolver for direct destinations like 'Sheet1'!$B$2 or 'Sheet1'!$B$2:$D$2.
                vals = op.get("values", {})
                wb = _read_xml_from_zip(zin, "xl/workbook.xml").getroot()
                dn_parent = wb.find("s:definedNames", NS)
                if dn_parent is None:
                    raise ValueError("Workbook has no defined names but plan requested fill_named_ranges.")
                name_to_text = {}
                for dn in dn_parent.findall("s:definedName", NS):
                    nm = dn.attrib.get("name")
                    if nm and dn.text:
                        name_to_text[nm] = dn.text.strip()

                for name, value in vals.items():
                    if name not in name_to_text:
                        raise ValueError(f"Defined name not found: {name}")
                    expr = name_to_text[name]
                    # Only support a single destination like Sheet1!$B$2 or Sheet1!$B$2:$D$2
                    # Strip external workbook refs and commas by taking first token.
                    expr = expr.split(",")[0]
                    if "!" not in expr:
                        raise ValueError(f"Unsupported definedName expression: {name}={expr}")
                    sheet_part, addr = expr.split("!", 1)
                    sheet_name = sheet_part.strip("'")
                    addr = addr.replace("$", "")
                    root = load_sheet(sheet_name)

                    if ":" in addr:
                        min_c, min_r, max_c, max_r = _parse_range(addr)
                        for r in range(min_r, max_r + 1):
                            for c in range(min_c, max_c + 1):
                                coord = f"{_int_to_col(c)}{r}"
                                row_el = _ensure_row(root, r)
                                cell_el = _ensure_cell(row_el, coord)
                                _assert_no_formula(cell_el, sheet=sheet_name, coord=coord)
                                _set_cell_value(cell_el, value)
                    else:
                        m = A1_RE.match(addr)
                        if not m:
                            raise ValueError(f"Unsupported named range address: {name} -> {addr}")
                        col, row = m.group(1), int(m.group(2))
                        coord = addr
                        row_el = _ensure_row(root, row)
                        cell_el = _ensure_cell(row_el, coord)
                        _assert_no_formula(cell_el, sheet=sheet_name, coord=coord)
                        _set_cell_value(cell_el, value)

                    save_sheet(sheet_name, root)

            else:
                raise ValueError(f"Patch mode does not support op: {kind}")

        # Write output zip: copy all entries, replace modified ones
        with zipfile.ZipFile(out_xlsx, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                name = item.filename
                if name in modified:
                    zout.writestr(name, modified[name])
                else:
                    zout.writestr(name, zin.read(name))

    return sorted(list(modified.keys()))
