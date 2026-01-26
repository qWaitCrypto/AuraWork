#!/usr/bin/env python3
"""
PPTX OOXML helpers (patch path).

These helpers work on an extracted PPTX directory (unzipped package root).
"""
from __future__ import annotations

import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rels": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}

REL_NS = {"rels": NS["rels"]}

for _prefix, _uri in NS.items():
    if _prefix != "xml":
        ET.register_namespace(_prefix, _uri)


@dataclass(frozen=True, slots=True)
class SlideRef:
    idx_1based: int
    rid: str
    part_uri: str  # e.g. "ppt/slides/slide1.xml"


def _read_xml(path: Path) -> ET.Element:
    return ET.parse(path).getroot()


def _write_xml(path: Path, root: ET.Element) -> None:
    path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))


def list_slide_refs(pkg_root: Path) -> list[SlideRef]:
    pres_xml = pkg_root / "ppt" / "presentation.xml"
    rels_xml = pkg_root / "ppt" / "_rels" / "presentation.xml.rels"
    pres_root = _read_xml(pres_xml)
    rels_root = _read_xml(rels_xml)

    rid_to_target: dict[str, str] = {}
    for rel in rels_root.findall("rels:Relationship", REL_NS):
        rid = rel.attrib.get("Id")
        target = rel.attrib.get("Target")
        if rid and target:
            rid_to_target[rid] = target

    out: list[SlideRef] = []
    sld_ids = pres_root.find("p:sldIdLst", NS)
    if sld_ids is None:
        return out

    i = 0
    for sld in sld_ids.findall("p:sldId", NS):
        i += 1
        rid = sld.attrib.get(f"{{{NS['r']}}}id") or sld.attrib.get("r:id") or ""
        target = rid_to_target.get(rid, "")
        if target.startswith("/"):
            target = target.lstrip("/")
        # target is relative to ppt/ folder
        part_uri = f"ppt/{target}" if target else ""
        out.append(SlideRef(idx_1based=i, rid=rid, part_uri=part_uri))
    return out


def get_slide_part(pkg_root: Path, slide_id: int) -> str:
    refs = list_slide_refs(pkg_root)
    if slide_id < 1 or slide_id > len(refs):
        raise ValueError(f"slide_id out of range: {slide_id} (slides={len(refs)})")
    part = refs[slide_id - 1].part_uri
    if not part:
        raise ValueError(f"Could not resolve slide part for slide_id={slide_id}")
    return part


def _iter_shapes(slide_root: ET.Element) -> Iterable[ET.Element]:
    # Text can appear in p:sp shapes; still scan generic containers and filter by cNvPr.
    for sp in slide_root.findall(".//p:sp", NS):
        yield sp


def _shape_matches_id_or_name(shape: ET.Element, shape_id: str) -> bool:
    cnv = shape.find(".//p:cNvPr", NS)
    if cnv is None:
        return False
    cid = cnv.attrib.get("id")
    name = cnv.attrib.get("name")

    # Accept "10" to match id="10", or match by name.
    if shape_id.isdigit() and cid == shape_id:
        return True
    if name and name == shape_id:
        return True
    return False


def iter_text_nodes(slide_root: ET.Element, *, shape_id: str | None = None) -> list[ET.Element]:
    if shape_id is None:
        return list(slide_root.findall(".//a:t", NS))
    for shape in _iter_shapes(slide_root):
        if _shape_matches_id_or_name(shape, shape_id):
            return list(shape.findall(".//a:t", NS))
    return []

def _locate_text_position(pos: int, lengths: list[int]) -> tuple[int, int]:
    """
    Map a 0-based character offset into a concatenated string to (node_index, offset_in_node).

    lengths are the lengths of each node's .text (None treated as "").
    """
    running = 0
    for i, ln in enumerate(lengths):
        if pos < running + ln:
            return i, pos - running
        running += ln
    # Fallback (should not happen for valid positions).
    last_i = max(0, len(lengths) - 1)
    last_len = lengths[last_i] if lengths else 0
    return last_i, max(0, last_len - 1)


def _replace_across_text_nodes(nodes: list[ET.Element], *, find: str, replace: str) -> int:
    """
    Replace `find` with `replace` across adjacent <a:t> nodes (best-effort).

    This handles the common PPTX case where a token is split across multiple runs.
    It preserves run structure by editing existing nodes and clearing consumed nodes.
    """
    if not find:
        return 0

    replaced = 0
    search_start = 0
    while True:
        texts = [(n.text or "") for n in nodes]
        combined = "".join(texts)
        idx = combined.find(find, search_start)
        if idx == -1:
            break

        lengths = [len(t) for t in texts]
        start_i, start_off = _locate_text_position(idx, lengths)
        end_pos = idx + len(find) - 1
        end_i, end_off = _locate_text_position(end_pos, lengths)

        if start_i == end_i:
            t = texts[start_i]
            nodes[start_i].text = t[:start_off] + replace + t[end_off + 1 :]
        else:
            start_t = texts[start_i]
            end_t = texts[end_i]
            nodes[start_i].text = start_t[:start_off] + replace
            for mid in nodes[start_i + 1 : end_i]:
                mid.text = ""
            nodes[end_i].text = end_t[end_off + 1 :]

        replaced += 1
        search_start = idx + len(replace)
    return replaced


def replace_text_in_slide(slide_path: Path, *, find: str, replace: str, shape_id: str | None = None) -> int:
    root = _read_xml(slide_path)

    # Replace within each paragraph to avoid spanning across unrelated containers.
    paragraphs: list[ET.Element]
    if shape_id is None:
        paragraphs = list(root.findall(".//a:p", NS))
    else:
        paragraphs = []
        for shape in _iter_shapes(root):
            if _shape_matches_id_or_name(shape, shape_id):
                paragraphs = list(shape.findall(".//a:p", NS))
                break

    total_replacements = 0
    for p in paragraphs:
        nodes = list(p.findall(".//a:t", NS))
        if not nodes:
            continue
        total_replacements += _replace_across_text_nodes(nodes, find=find, replace=replace)

    if total_replacements:
        _write_xml(slide_path, root)
    return total_replacements


def _placeholder_matches(ph: ET.Element, placeholder_id: str) -> bool:
    ptype = ph.attrib.get("type", "")
    pidx = ph.attrib.get("idx", "")
    if placeholder_id.isdigit() and pidx == placeholder_id:
        return True
    if placeholder_id == "title" and ptype in {"title", "ctrTitle"}:
        return True
    return ptype == placeholder_id


def fill_placeholder(slide_path: Path, *, placeholder_id: str, content: str) -> bool:
    root = _read_xml(slide_path)
    # placeholder marker lives at p:ph under nvPr
    targets: list[ET.Element] = []
    for sp in _iter_shapes(root):
        ph = sp.find(".//p:ph", NS)
        if ph is None:
            continue
        if _placeholder_matches(ph, placeholder_id):
            targets.append(sp)
    if not targets:
        return False

    changed = False
    for sp in targets:
        text_nodes = list(sp.findall(".//a:t", NS))
        if text_nodes:
            text_nodes[0].text = content
            for extra in text_nodes[1:]:
                extra.text = ""
            changed = True
        else:
            # No text run exists; best-effort create a minimal txBody with a single run.
            tx = sp.find("p:txBody", NS)
            if tx is None:
                tx = ET.SubElement(sp, f"{{{NS['p']}}}txBody")
            ap = tx.find("a:p", NS)
            if ap is None:
                ap = ET.SubElement(tx, f"{{{NS['a']}}}p")
            ar = ET.SubElement(ap, f"{{{NS['a']}}}r")
            at = ET.SubElement(ar, f"{{{NS['a']}}}t")
            at.text = content
            changed = True

    if changed:
        _write_xml(slide_path, root)
    return changed


def delete_slide(pkg_root: Path, *, slide_id: int) -> dict[str, str]:
    pres_xml = pkg_root / "ppt" / "presentation.xml"
    rels_xml = pkg_root / "ppt" / "_rels" / "presentation.xml.rels"
    pres_root = _read_xml(pres_xml)
    rels_root = _read_xml(rels_xml)

    sld_ids = pres_root.find("p:sldIdLst", NS)
    if sld_ids is None:
        raise ValueError("ppt/presentation.xml missing p:sldIdLst")

    sld_list = list(sld_ids.findall("p:sldId", NS))
    if slide_id < 1 or slide_id > len(sld_list):
        raise ValueError(f"slide_id out of range: {slide_id} (slides={len(sld_list)})")

    victim = sld_list[slide_id - 1]
    rid = victim.attrib.get(f"{{{NS['r']}}}id") or victim.attrib.get("r:id") or ""
    sld_ids.remove(victim)

    removed_rel_target = ""
    for rel in list(rels_root.findall("rels:Relationship", REL_NS)):
        if rel.attrib.get("Id") == rid:
            removed_rel_target = rel.attrib.get("Target", "")
            rels_root.remove(rel)
            break

    _write_xml(pres_xml, pres_root)
    _write_xml(rels_xml, rels_root)

    return {"rid": rid, "target": removed_rel_target}


def reorder_slides(pkg_root: Path, *, order: list[int]) -> None:
    pres_xml = pkg_root / "ppt" / "presentation.xml"
    pres_root = _read_xml(pres_xml)

    sld_ids = pres_root.find("p:sldIdLst", NS)
    if sld_ids is None:
        raise ValueError("ppt/presentation.xml missing p:sldIdLst")

    sld_list = list(sld_ids.findall("p:sldId", NS))
    n = len(sld_list)
    if sorted(order) != list(range(1, n + 1)):
        raise ValueError(f"reorder_slides.order must be a permutation of 1..{n}")

    # Rebuild sldIdLst in the requested order.
    for sld in sld_list:
        sld_ids.remove(sld)
    for idx in order:
        sld_ids.append(sld_list[idx - 1])

    _write_xml(pres_xml, pres_root)


def duplicate_slide(pkg_root: Path, *, slide_id: int, after_slide_id: int | None = None) -> dict[str, str | int]:
    """
    Duplicate a slide part and insert it into the slide list.

    Notes:
    - This is a best-effort OOXML-level duplication.
    - Slide-level relationships are copied as-is, so duplicated slides may share targets
      (e.g. notesSlides/comments/media). This is acceptable for template-style filling.
    """
    pres_xml = pkg_root / "ppt" / "presentation.xml"
    rels_xml = pkg_root / "ppt" / "_rels" / "presentation.xml.rels"
    ct_xml = pkg_root / "[Content_Types].xml"

    pres_root = _read_xml(pres_xml)
    rels_root = _read_xml(rels_xml)
    ct_root = _read_xml(ct_xml)

    sld_ids = pres_root.find("p:sldIdLst", NS)
    if sld_ids is None:
        raise ValueError("ppt/presentation.xml missing p:sldIdLst")

    sld_list = list(sld_ids.findall("p:sldId", NS))
    if slide_id < 1 or slide_id > len(sld_list):
        raise ValueError(f"slide_id out of range: {slide_id} (slides={len(sld_list)})")

    after = slide_id if after_slide_id is None else int(after_slide_id)
    if after < 0 or after > len(sld_list):
        raise ValueError(f"after_slide_id out of range: {after} (slides={len(sld_list)})")

    victim = sld_list[slide_id - 1]
    victim_rid = victim.attrib.get(f"{{{NS['r']}}}id") or victim.attrib.get("r:id") or ""
    if not victim_rid:
        raise ValueError(f"Slide {slide_id} missing r:id")

    rel_type = ""
    rel_target = ""
    for rel in rels_root.findall("rels:Relationship", REL_NS):
        if rel.attrib.get("Id") == victim_rid:
            rel_type = rel.attrib.get("Type", "")
            rel_target = rel.attrib.get("Target", "")
            break
    if not rel_target:
        raise ValueError(f"Could not resolve slide target for rId={victim_rid}")

    if rel_target.startswith("/"):
        rel_target = rel_target.lstrip("/")

    # Determine new slide part number (max slideN + 1).
    slides_dir = pkg_root / "ppt" / "slides"
    slides_dir.mkdir(parents=True, exist_ok=True)
    existing_nums: list[int] = []
    for p in slides_dir.glob("slide*.xml"):
        m = re.match(r"slide(\d+)\.xml$", p.name)
        if m:
            existing_nums.append(int(m.group(1)))
    new_num = (max(existing_nums) + 1) if existing_nums else 1

    src_slide_path = pkg_root / "ppt" / rel_target
    if not src_slide_path.exists():
        raise ValueError(f"Source slide part missing: {src_slide_path}")

    new_rel_target = f"slides/slide{new_num}.xml"
    new_slide_uri = f"ppt/{new_rel_target}"
    dst_slide_path = pkg_root / "ppt" / new_rel_target
    dst_slide_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src_slide_path, dst_slide_path)

    # Copy slide relationships part (if present).
    src_slide_rels = pkg_root / "ppt" / "slides" / "_rels" / f"{src_slide_path.name}.rels"
    if src_slide_rels.exists():
        dst_slide_rels = pkg_root / "ppt" / "slides" / "_rels" / f"{dst_slide_path.name}.rels"
        dst_slide_rels.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_slide_rels, dst_slide_rels)

    # Add content type override for the new slide part.
    slide_ct = ""
    slide_part_name = f"/ppt/{rel_target}"
    for ov in ct_root.findall("ct:Override", NS):
        if ov.attrib.get("PartName") == slide_part_name:
            slide_ct = ov.attrib.get("ContentType", "")
            break
    if not slide_ct:
        for ov in ct_root.findall("ct:Override", NS):
            pn = ov.attrib.get("PartName", "")
            if pn.startswith("/ppt/slides/slide") and pn.endswith(".xml"):
                slide_ct = ov.attrib.get("ContentType", "")
                break
    if not slide_ct:
        slide_ct = "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"

    new_part_name = f"/ppt/slides/slide{new_num}.xml"
    has_override = any(
        ov.attrib.get("PartName") == new_part_name for ov in ct_root.findall("ct:Override", NS)
    )
    if not has_override:
        ET.SubElement(ct_root, f"{{{NS['ct']}}}Override", {"PartName": new_part_name, "ContentType": slide_ct})

    # Allocate a new relationship Id in presentation.xml.rels.
    max_rid = 0
    for rel in rels_root.findall("rels:Relationship", REL_NS):
        rid = rel.attrib.get("Id", "")
        m = re.match(r"rId(\d+)$", rid)
        if m:
            max_rid = max(max_rid, int(m.group(1)))
    new_rid = f"rId{max_rid + 1}"
    ET.SubElement(
        rels_root,
        f"{{{NS['rels']}}}Relationship",
        {"Id": new_rid, "Type": rel_type, "Target": new_rel_target},
    )

    # Allocate a new slide id (numeric `id` attribute).
    max_sid = 0
    for sld in sld_list:
        try:
            max_sid = max(max_sid, int(sld.attrib.get("id", "0")))
        except Exception:
            continue
    new_sid = str(max_sid + 1)

    new_sld = ET.Element(f"{{{NS['p']}}}sldId", {"id": new_sid})
    new_sld.attrib[f"{{{NS['r']}}}id"] = new_rid

    insert_at = after
    if insert_at >= len(sld_list):
        sld_ids.append(new_sld)
    else:
        sld_ids.insert(insert_at, new_sld)

    _write_xml(pres_xml, pres_root)
    _write_xml(rels_xml, rels_root)
    _write_xml(ct_xml, ct_root)

    return {
        "source_slide_id": slide_id,
        "after_slide_id": after,
        "new_slide_id": after + 1,
        "new_rid": new_rid,
        "new_part_uri": new_slide_uri,
    }
