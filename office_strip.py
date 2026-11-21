"""Take the comments out of an Office export before it is imported on the target.

Google writes a file's comments into its .docx/.xlsx/.pptx export, and importing that file recreates them as Drive
comments credited to their original (source-domain) authors -- which the target user can then neither delete nor
match (a. -> b., 21 Nov 2026: every commented file held each comment twice, and comments.delete answered 403).
Bitport's comment pass posts the faithful copies itself, so the export goes up without its own.
"""
from __future__ import annotations

import io
import re
import zipfile

_PART = re.compile(r"^(word/(comments[^/]*|people)\.xml|xl/comments\d*\.xml|xl/threadedComments/[^/]+|"
                   r"xl/persons/[^/]+|xl/drawings/vmlDrawing\d*\.vml|ppt/comments/[^/]+|ppt/(commentAuthors|authors)\.xml)$")
_REL = re.compile(r'<Relationship\b[^>]*\bTarget="([^"]+)"[^>]*/>')
_OVERRIDE = re.compile(r'<Override\b[^>]*\bPartName="/([^"]+)"[^>]*/>')
# References to a removed part: Word's comment anchors, a worksheet's VML drawing (Google's xlsx uses it only for
# comment boxes), a slide's comment list.
_REFS = re.compile(r"<w:commentRange(?:Start|End)\b[^>]*/>|<w:commentReference\b[^>]*/>|<legacyDrawing\b[^>]*/>")


def _resolve(rels_name: str, target: str) -> str:
    """A relationship's Target as a part name ("word/_rels/document.xml.rels" + "comments.xml" -> "word/comments.xml")."""
    if target.startswith("/"):
        return target[1:]
    base = rels_name.split("_rels/")[0]
    parts = (base + target).split("/")
    out: list[str] = []
    for p in parts:
        if p == "..":
            if out:
                out.pop()
        elif p and p != ".":
            out.append(p)
    return "/".join(out)


def strip_comments(data: bytes) -> bytes:
    """The same file without its comment parts and the references to them; anything else is returned as given."""
    try:
        src = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return data
    drop = {n for n in src.namelist() if _PART.match(n)}
    if not drop:
        return data
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            name = info.filename
            if name in drop:
                continue
            body = src.read(name)
            if name.endswith(".rels"):
                text = body.decode("utf-8")
                text = _REL.sub(lambda m: "" if _resolve(name, m.group(1)) in drop else m.group(0), text)
                body = text.encode("utf-8")
            elif name == "[Content_Types].xml":
                body = _OVERRIDE.sub(lambda m: "" if m.group(1) in drop else m.group(0), body.decode("utf-8")).encode("utf-8")
            elif name.endswith(".xml") and (name.startswith(("word/", "xl/worksheets/", "ppt/slides/"))):
                body = _REFS.sub("", body.decode("utf-8")).encode("utf-8")
            dst.writestr(info, body)
    return out.getvalue()
