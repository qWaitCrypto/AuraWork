#!/usr/bin/env python3
"""
Apply a DOCX plan.json (aura_plan_spec) to a DOCX package.

This is a minimal, package-preserving OOXML patcher intended for office workflows.
"""
from __future__ import annotations
import logging

import argparse
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import xml.etree.ElementTree as ET

from utilities import NS


logger = logging.getLogger(__name__)

REL_NS = {"rels": "http://schemas.openxmlformats.org/package/2006/relationships"}

# Register stable prefixes to avoid ns0/ns1 output when we serialize touched parts.
for _prefix, _uri in NS.items():
    if _prefix != "xml":
        ET.register_namespace(_prefix, _uri)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_part(zf: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        return zf.read(name)
    except KeyError:
        return None


def _write_xml(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _ensure_content_type_override(ct_root: ET.Element, part_name: str, content_type: str) -> None:
    ns = {"ct": "http://schemas.openxmlformats.org/package/2006/content-types"}
    # part_name must be like "/word/comments.xml"
    for ov in ct_root.findall("ct:Override", ns):
        if ov.attrib.get("PartName") == part_name:
            ov.attrib["ContentType"] = content_type
            return
    ov = ET.SubElement(ct_root, "{%s}Override" % ns["ct"])
    ov.set("PartName", part_name)
    ov.set("ContentType", content_type)


def _ensure_document_rels_comment_rel(rels_root: ET.Element) -> str:
    """
    Ensure comments.xml relationship exists and return its rId.
    """
    rtype = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
    for rel in rels_root.findall("rels:Relationship", REL_NS):
        if rel.attrib.get("Type") == rtype and rel.attrib.get("Target") == "comments.xml":
            return rel.attrib.get("Id", "rIdComments")

    # choose next rIdN
    existing = []
    for rel in rels_root.findall("rels:Relationship", REL_NS):
        rid = rel.attrib.get("Id", "")
        if rid.startswith("rId"):
            try:
                existing.append(int(rid[3:]))
            except Exception:
                logger.warning("Suppressed exception in _ensure_document_rels_comment_rel.", exc_info=True)
    next_id = max(existing, default=0) + 1
    rid = f"rId{next_id}"

    new_rel = ET.SubElement(rels_root, "{%s}Relationship" % REL_NS["rels"])
    new_rel.set("Id", rid)
    new_rel.set("Type", rtype)
    new_rel.set("Target", "comments.xml")
    return rid


def _load_xml_or_raise(xml_bytes: bytes, part: str) -> ET.Element:
    try:
        return ET.fromstring(xml_bytes)
    except Exception as e:
        raise ValueError(f"Failed to parse XML part: {part}: {e}") from e


def _iter_paragraphs(document_root: ET.Element) -> List[ET.Element]:
    return list(document_root.findall(".//w:p", NS))


def _paragraph_text(p: ET.Element) -> str:
    texts = []
    for t in p.findall(".//w:t", NS):
        if t.text:
            texts.append(t.text)
    return "".join(texts)


def _paragraph_style(p: ET.Element) -> str | None:
    ppr = p.find("w:pPr", NS)
    if ppr is None:
        return None
    pstyle = ppr.find("w:pStyle", NS)
    if pstyle is None:
        return None
    return pstyle.attrib.get(f"{{{NS['w']}}}val") or pstyle.attrib.get("w:val") or pstyle.attrib.get("val")


def _is_heading_paragraph(p: ET.Element) -> bool:
    style = _paragraph_style(p)
    return bool(style and style.lower().startswith("heading"))


def _find_paragraph_by_heading_text(document_root: ET.Element, heading_text: str) -> ET.Element | None:
    for p in _iter_paragraphs(document_root):
        if _is_heading_paragraph(p) and _paragraph_text(p).strip() == heading_text.strip():
            return p
    return None


def _find_paragraph_by_id(document_root: ET.Element, paragraph_id: str) -> ET.Element | None:
    # paragraph ids are generated as p1, p2, ... over document order.
    if not paragraph_id.startswith("p"):
        return None
    try:
        idx = int(paragraph_id[1:]) - 1
    except Exception:
        return None
    paras = _iter_paragraphs(document_root)
    if 0 <= idx < len(paras):
        return paras[idx]
    return None


def _find_paragraph_by_anchor(document_root: ET.Element, anchor: str) -> ET.Element | None:
    # anchor: heading:X | bookmark:X | paragraph_id:X | after:X
    if anchor.startswith("heading:"):
        return _find_paragraph_by_heading_text(document_root, anchor.split(":", 1)[1])
    if anchor.startswith("paragraph_id:"):
        return _find_paragraph_by_id(document_root, anchor.split(":", 1)[1])
    if anchor.startswith("after:"):
        return _find_paragraph_by_id(document_root, anchor.split(":", 1)[1])
    if anchor.startswith("bookmark:"):
        name = anchor.split(":", 1)[1]
        # ElementTree has no parent pointers; find paragraph containing the bookmarkStart.
        for p in _iter_paragraphs(document_root):
            for bm in p.findall(".//w:bookmarkStart", NS):
                if bm.attrib.get(f"{{{NS['w']}}}name") == name or bm.attrib.get("w:name") == name:
                    return p
        return None
    return None


def _make_paragraph(text: str, style: str | None = None) -> ET.Element:
    p = ET.Element("{%s}p" % NS["w"])
    if style:
        ppr = ET.SubElement(p, "{%s}pPr" % NS["w"])
        pstyle = ET.SubElement(ppr, "{%s}pStyle" % NS["w"])
        pstyle.set("{%s}val" % NS["w"], style)
    r = ET.SubElement(p, "{%s}r" % NS["w"])
    t = ET.SubElement(r, "{%s}t" % NS["w"])
    t.text = text
    return p


def _insert_paragraph(document_root: ET.Element, *, anchor: str, position: str, text: str, style: str | None) -> bool:
    body = document_root.find("w:body", NS)
    if body is None:
        return False

    target = _find_paragraph_by_anchor(document_root, anchor)
    if target is None:
        return False

    new_p = _make_paragraph(text, style)

    # ElementTree has no parent pointers; insert by scanning body children.
    children = list(body)
    for i, el in enumerate(children):
        if el is target:
            insert_at = i if position == "before" else i + 1
            body.insert(insert_at, new_p)
            return True
    return False


def _replace_text_in_paragraph(*, paragraph: ET.Element, find: str, replace: str, first_only: bool) -> int:
    hits = 0
    for t in paragraph.findall(".//w:t", NS):
        if not t.text or find not in t.text:
            continue
        if first_only:
            t.text = t.text.replace(find, replace, 1)
            return 1
        count = t.text.count(find)
        t.text = t.text.replace(find, replace)
        hits += count
    return hits


def _replace_text_in_document(
    document_root: ET.Element,
    *,
    find: str,
    replace: str,
    scope: str,
    paragraph_id: str | None = None,
) -> int:
    hits = 0
    if scope == "paragraph_id":
        if not paragraph_id:
            return 0
        paragraph = _find_paragraph_by_id(document_root, paragraph_id)
        if paragraph is None:
            return 0
        return _replace_text_in_paragraph(paragraph=paragraph, find=find, replace=replace, first_only=False)

    for p in _iter_paragraphs(document_root):
        if scope == "heading" and not _is_heading_paragraph(p):
            continue
        replaced = _replace_text_in_paragraph(
            paragraph=p,
            find=find,
            replace=replace,
            first_only=(scope == "first"),
        )
        hits += replaced
        if scope == "first" and replaced:
            return hits
    return hits


def _ensure_comments_part(existing: bytes | None) -> ET.Element:
    if existing:
        return _load_xml_or_raise(existing, "word/comments.xml")
    # Minimal comments root
    root = ET.Element("{%s}comments" % NS["w"])
    return root


def _next_comment_id(comments_root: ET.Element) -> int:
    ids = []
    for c in comments_root.findall("w:comment", NS):
        cid = c.attrib.get(f"{{{NS['w']}}}id") or c.attrib.get("w:id") or c.attrib.get("id")
        if cid is None:
            continue
        try:
            ids.append(int(str(cid)))
        except Exception:
            logger.warning("Suppressed exception in _next_comment_id.", exc_info=True)
    return max(ids, default=-1) + 1


def _add_comment_to_paragraph(
    document_root: ET.Element,
    comments_root: ET.Element,
    *,
    paragraph: ET.Element,
    text: str,
    author: str,
) -> int:
    cid = _next_comment_id(comments_root)

    # Add comment record
    c = ET.SubElement(comments_root, "{%s}comment" % NS["w"])
    c.set("{%s}id" % NS["w"], str(cid))
    c.set("{%s}author" % NS["w"], author)
    c.set("{%s}date" % NS["w"], _utc_now_iso())
    p = ET.SubElement(c, "{%s}p" % NS["w"])
    r = ET.SubElement(p, "{%s}r" % NS["w"])
    t = ET.SubElement(r, "{%s}t" % NS["w"])
    t.text = text

    # Wrap paragraph with comment range markers
    start = ET.Element("{%s}commentRangeStart" % NS["w"])
    start.set("{%s}id" % NS["w"], str(cid))
    end = ET.Element("{%s}commentRangeEnd" % NS["w"])
    end.set("{%s}id" % NS["w"], str(cid))
    ref_run = ET.Element("{%s}r" % NS["w"])
    ref = ET.SubElement(ref_run, "{%s}commentReference" % NS["w"])
    ref.set("{%s}id" % NS["w"], str(cid))

    # Insert at paragraph boundaries.
    # IMPORTANT: w:pPr (if present) must stay the first child of w:p to satisfy the schema.
    insert_at = 0
    if len(paragraph) > 0 and paragraph[0].tag == "{%s}pPr" % NS["w"]:
        insert_at = 1
    paragraph.insert(insert_at, start)
    paragraph.append(end)
    paragraph.append(ref_run)

    return cid


def _apply_tracked_change(
    document_root: ET.Element,
    *,
    change_type: str,
    paragraph: ET.Element,
    old_text: str,
    new_text: str,
    author: str,
) -> bool:
    """
    Best-effort tracked change:
    - delete: wrap old_text in <w:del> and insert new_text in <w:ins>
    - insert: wrap new_text in <w:ins>
    This is conservative and only works when old_text is contained in a single <w:t>.
    """
    now = _utc_now_iso()
    rev_id = str(int(datetime.now(timezone.utc).timestamp() * 1000))

    for t in paragraph.findall(".//w:t", NS):
        if not t.text or old_text not in t.text:
            continue
        if change_type == "insert":
            # Insert new text after the current run.
            ins = ET.Element("{%s}ins" % NS["w"])
            ins.set("{%s}id" % NS["w"], rev_id)
            ins.set("{%s}author" % NS["w"], author)
            ins.set("{%s}date" % NS["w"], now)
            r = ET.SubElement(ins, "{%s}r" % NS["w"])
            tt = ET.SubElement(r, "{%s}t" % NS["w"])
            tt.text = new_text
            paragraph.append(ins)
            return True

        # delete/replace: split the text into pre/old/post (first occurrence)
        pre, rest = t.text.split(old_text, 1)
        post = rest
        t.text = pre

        del_el = ET.Element("{%s}del" % NS["w"])
        del_el.set("{%s}id" % NS["w"], rev_id)
        del_el.set("{%s}author" % NS["w"], author)
        del_el.set("{%s}date" % NS["w"], now)
        dr = ET.SubElement(del_el, "{%s}r" % NS["w"])
        dt = ET.SubElement(dr, "{%s}delText" % NS["w"])
        dt.text = old_text

        ins_el = ET.Element("{%s}ins" % NS["w"])
        ins_el.set("{%s}id" % NS["w"], str(int(rev_id) + 1))
        ins_el.set("{%s}author" % NS["w"], author)
        ins_el.set("{%s}date" % NS["w"], now)
        ir = ET.SubElement(ins_el, "{%s}r" % NS["w"])
        it = ET.SubElement(ir, "{%s}t" % NS["w"])
        it.text = new_text

        # Insert del/ins after the run containing pre text
        paragraph.append(del_el)
        paragraph.append(ins_el)

        # Add post as a new run if needed
        if post:
            rr = ET.SubElement(paragraph, "{%s}r" % NS["w"])
            tt2 = ET.SubElement(rr, "{%s}t" % NS["w"])
            tt2.text = post
        return True

    return False


def _new_relationships_root() -> ET.Element:
    return ET.Element("{%s}Relationships" % REL_NS["rels"])


def _apply_patch_plan(docx_in: Path, plan: dict[str, Any], docx_out: Path) -> dict[str, Any]:
    touched: list[str] = []
    op_results: list[dict[str, Any]] = []
    has_tracked_changes = False
    has_comments = False

    with zipfile.ZipFile(docx_in, "r") as zin:
        # Load required parts
        document_xml = _read_part(zin, "word/document.xml")
        if not document_xml:
            raise ValueError("Input is missing word/document.xml")
        doc_root = _load_xml_or_raise(document_xml, "word/document.xml")

        comments_xml = _read_part(zin, "word/comments.xml")
        ct_xml = _read_part(zin, "[Content_Types].xml")
        doc_rels_xml = _read_part(zin, "word/_rels/document.xml.rels")

        comments_root: ET.Element | None = None
        ct_root: ET.Element | None = None
        doc_rels_root: ET.Element | None = None

        def _get_comments_root() -> ET.Element:
            nonlocal comments_root
            if comments_root is None:
                comments_root = _ensure_comments_part(comments_xml)
            return comments_root

        def _get_ct_root() -> ET.Element | None:
            nonlocal ct_root
            if ct_root is None and ct_xml:
                ct_root = _load_xml_or_raise(ct_xml, "[Content_Types].xml")
            return ct_root

        def _get_doc_rels_root(*, create_if_missing: bool) -> ET.Element | None:
            nonlocal doc_rels_root
            if doc_rels_root is None:
                if doc_rels_xml:
                    doc_rels_root = _load_xml_or_raise(doc_rels_xml, "word/_rels/document.xml.rels")
                elif create_if_missing:
                    doc_rels_root = _new_relationships_root()
            return doc_rels_root

        # Apply operations
        for op in plan.get("operations", []):
            kind = op.get("op")
            if kind in ("replace_text", "fill_placeholder"):
                find = op.get("find", "")
                replace = op.get("replace", "")
                scope = op.get("scope", "all")
                if not isinstance(find, str) or find == "":
                    raise ValueError("replace_text requires non-empty string 'find'")
                if not isinstance(replace, str):
                    replace = str(replace)
                if scope not in ("all", "first", "heading", "paragraph_id"):
                    scope = "all"
                paragraph_id: str | None = None
                if scope == "paragraph_id":
                    pid = op.get("paragraph_id")
                    if isinstance(pid, str) and pid:
                        paragraph_id = pid
                    anchor = op.get("anchor")
                    if paragraph_id is None and isinstance(anchor, str) and anchor:
                        if anchor.startswith("paragraph_id:"):
                            paragraph_id = anchor.split(":", 1)[1]
                        elif anchor.startswith("p"):
                            paragraph_id = anchor
                hits = _replace_text_in_document(
                    doc_root,
                    find=find,
                    replace=replace,
                    scope=scope,
                    paragraph_id=paragraph_id,
                )
                if scope == "paragraph_id" and not hits:
                    op_results.append(
                        {
                            "op": kind,
                            "ok": False,
                            "scope": scope,
                            "paragraph_id": paragraph_id or "",
                            "error": "paragraph_not_found_or_no_match",
                        }
                    )
                else:
                    op_results.append({"op": kind, "find": find, "replacements": hits, "scope": scope})
                if hits:
                    touched.append("word/document.xml")
                continue

            if kind == "insert_paragraph":
                anchor = op.get("anchor", "")
                position = op.get("position", "after")
                text = op.get("text", "")
                style = op.get("style")
                if not isinstance(anchor, str) or not isinstance(text, str):
                    raise ValueError("insert_paragraph requires 'anchor' and 'text' strings")
                if position not in ("before", "after"):
                    position = "after"
                ok = _insert_paragraph(doc_root, anchor=anchor, position=position, text=text, style=style if isinstance(style, str) else None)
                op_results.append({"op": kind, "ok": ok, "anchor": anchor})
                if ok:
                    touched.append("word/document.xml")
                continue

            if kind == "add_comment":
                anchor = op.get("anchor", "")
                text = op.get("text", "")
                author = op.get("author", "AI Assistant")
                if not isinstance(anchor, str) or not isinstance(text, str):
                    raise ValueError("add_comment requires 'anchor' and 'text' strings")
                paragraph = _find_paragraph_by_anchor(doc_root, anchor)
                if paragraph is None:
                    op_results.append({"op": kind, "ok": False, "anchor": anchor, "error": "anchor_not_found"})
                    continue
                cid = _add_comment_to_paragraph(
                    doc_root,
                    _get_comments_root(),
                    paragraph=paragraph,
                    text=text,
                    author=str(author),
                )
                op_results.append({"op": kind, "ok": True, "anchor": anchor, "comment_id": cid})
                touched.extend(["word/document.xml", "word/comments.xml"])
                # Ensure content types + relationship for comments
                ct_root2 = _get_ct_root()
                if ct_root2 is not None:
                    _ensure_content_type_override(
                        ct_root2,
                        "/word/comments.xml",
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
                    )
                    touched.append("[Content_Types].xml")
                rels_root = _get_doc_rels_root(create_if_missing=True)
                if rels_root is not None:
                    _ensure_document_rels_comment_rel(rels_root)
                    touched.append("word/_rels/document.xml.rels")
                continue

            if kind == "tracked_change":
                change_type = op.get("type", "")
                anchor = op.get("anchor", "")
                old_text = op.get("old_text", "")
                new_text = op.get("new_text", "")
                author = op.get("author", "AI Assistant")
                if change_type not in ("delete", "insert"):
                    raise ValueError("tracked_change.type must be 'delete' or 'insert'")
                if not isinstance(anchor, str) or not isinstance(old_text, str) or not isinstance(new_text, str):
                    raise ValueError("tracked_change requires 'anchor', 'old_text', 'new_text' strings")
                paragraph = _find_paragraph_by_anchor(doc_root, anchor)
                if paragraph is None:
                    op_results.append({"op": kind, "ok": False, "anchor": anchor, "error": "anchor_not_found"})
                    continue
                ok = _apply_tracked_change(
                    doc_root,
                    change_type=change_type,
                    paragraph=paragraph,
                    old_text=old_text,
                    new_text=new_text,
                    author=str(author),
                )
                op_results.append({"op": kind, "ok": ok, "anchor": anchor})
                if ok:
                    touched.append("word/document.xml")
                continue

            raise ValueError(f"Unsupported DOCX op: {kind}")

        has_tracked_changes = bool(doc_root.findall(".//w:ins", NS) or doc_root.findall(".//w:del", NS))
        has_comments = bool(comments_xml) or bool(doc_root.findall(".//w:commentReference", NS))
        if comments_root is not None:
            has_comments = has_comments or bool(comments_root.findall(".//w:comment", NS))

        # Prepare updated parts (only write the ones we actually touched)
        new_parts: dict[str, bytes] = {}
        if "word/document.xml" in touched:
            new_parts["word/document.xml"] = _write_xml(doc_root)
        if "word/comments.xml" in touched:
            new_parts["word/comments.xml"] = _write_xml(_get_comments_root())
        if "[Content_Types].xml" in touched:
            ct_root2 = _get_ct_root()
            if ct_root2 is not None:
                new_parts["[Content_Types].xml"] = _write_xml(ct_root2)
        if "word/_rels/document.xml.rels" in touched:
            rels_root = _get_doc_rels_root(create_if_missing=True)
            if rels_root is not None:
                new_parts["word/_rels/document.xml.rels"] = _write_xml(rels_root)

        # Write output package, replacing only touched parts
        docx_out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(docx_out, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            written = set()
            for item in zin.infolist():
                name = item.filename
                if item.is_dir():
                    zout.writestr(item, b"")
                    written.add(name)
                    continue
                payload = new_parts.get(name)
                if payload is None:
                    payload = zin.read(name)
                zout.writestr(item, payload)
                written.add(name)

            # Add brand-new parts that were not present in the input package.
            for name, payload in new_parts.items():
                if name not in written:
                    zout.writestr(name, payload)

    # De-duplicate touched list, keep stable order
    touched_unique: list[str] = []
    for p in touched:
        if p not in touched_unique:
            touched_unique.append(p)

    change_summary = "; ".join([f"{r.get('op')}: {r.get('replacements', r.get('ok'))}" for r in op_results if isinstance(r, dict)])

    return {
        "engine": "patch",
        "touched_parts": touched_unique,
        "operations": op_results,
        "risk_summary": {
            "has_charts": False,
            "has_pivots": False,
            "has_controls": False,
            "has_macros": False,
            "has_formulas": False,
            "has_external_links": False,
            "risk_level": "medium" if (has_tracked_changes or has_comments) else "low",
        },
        "docx_features": {
            "has_tracked_changes": has_tracked_changes,
            "has_comments": has_comments,
        },
        "change_summary": change_summary,
    }
import docx_create

def _is_creation_plan(plan: dict[str, Any]) -> bool:
    create_ops = {
        "add_heading", "add_paragraph", "add_line_break", "create_table",
        "merge_table_cells", "style_table_cell", "set_table_borders", "add_page_break"
    }
    for op in plan.get("operations", []):
        if op.get("op") in create_ops:
            return True
    return False


def apply_plan(in_docx: Path, plan: Dict[str, Any], out_docx: Path, mode: str = "auto") -> Dict[str, Any]:
    
    # Decide execution mode
    use_creation_engine = False
    
    if mode == "create":
        use_creation_engine = True
    elif mode == "auto":
        # If input doesn't exist, we must create
        if not in_docx.exists():
            use_creation_engine = True
        # If plan has creation ops, we switch to creation engine (python-docx)
        elif _is_creation_plan(plan):
            use_creation_engine = True

    if use_creation_engine:
        # Delegate to docx_create engine
        try:
            return docx_create.apply_create_plan(in_docx if in_docx.exists() else None, plan, out_docx)
        except Exception as e:
            # Fallback or error if python-docx not available/failed
            raise RuntimeError(f"Creation engine failed: {e}") from e

    # --- OOXML Patch Engine (Default for editing existing docs) ---
    
    if not in_docx.exists():
        raise FileNotFoundError(f"Input file not found: {in_docx}. Use --mode create or add creation ops to plan to start from scratch.")
    return _apply_patch_plan(in_docx, plan, out_docx)


def main() -> None:
    ap = argparse.ArgumentParser(description="Apply aura_docx plan.json to a DOCX file.")
    ap.add_argument("in_docx", type=Path)
    ap.add_argument("plan_json", type=Path)
    ap.add_argument("out_docx", type=Path)
    ap.add_argument("--mode", choices=["auto", "patch", "create"], default="auto")
    ap.add_argument("--out", type=Path, default=None, help="Write apply_report.json")
    args = ap.parse_args()

    plan = json.loads(args.plan_json.read_text(encoding="utf-8"))
    rep = apply_plan(args.in_docx, plan, args.out_docx, mode=args.mode)

    payload = json.dumps(rep, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
