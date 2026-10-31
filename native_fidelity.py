"""Compare API-visible native structures, with explicit unsupported features.

Generated identities are renamed consistently across declarations and references;
text/format/layout fields remain present. This does not certify revision history.
"""

from __future__ import annotations

import copy
from link_rewrite import rewrite_text

ID_FIELDS = {
    "objectId",
    "pageObjectId",
    "layoutObjectId",
    "masterObjectId",
    "parentObjectId",
    "listId",
    "tabId",
    "headerId",
    "footerId",
    "footnoteId",
    "inlineObjectId",
    "positionedObjectId",
    "headingId",
    "bookmarkId",
    "namedRangeId",
}
ID_MAPS = {
    "lists",
    "headers",
    "footers",
    "footnotes",
    "inlineObjects",
    "positionedObjects",
    "namedRanges",
    "bookmarks",
}
ROOT_VOLATILE = {"documentId", "presentationId", "revisionId", "suggestionsViewMode"}


def canonical_native(document, lookup=lambda _: None):
    doc = copy.deepcopy(document)
    ids = {}

    def identity(value):
        if value is None:
            return None
        return ids.setdefault(str(value), "native-id-" + str(len(ids)))

    def visit(value, path=()):
        if isinstance(value, list):
            return [visit(v, path + (i,)) for i, v in enumerate(value)]
        if not isinstance(value, dict):
            return rewrite_text(value, lookup)[0] if isinstance(value, str) else value
        out = {}
        for key in sorted(value):
            item = value[key]
            if not path and key in ROOT_VOLATILE:
                continue
            if key in ID_FIELDS:
                out[key] = identity(item)
            elif key in ID_MAPS and isinstance(item, dict):
                # References normally establish IDs before declaration maps.
                known = sorted(item, key=lambda k: (k not in ids, ids.get(k, k)))
                out[key] = {identity(k): visit(item[k], path + (key, k)) for k in known}
            elif key in ("startIndex", "endIndex") and any(
                k in value
                for k in (
                    "paragraph",
                    "textRun",
                    "table",
                    "tableOfContents",
                    "sectionBreak",
                    "autoText",
                    "inlineObjectElement",
                )
            ):
                # Derived offsets: text-run contents/style boundaries and table
                # topology remain compared. Named/style range offsets remain.
                continue
            else:
                out[key] = visit(item, path + (key,))
        return out

    return visit(doc)


IMAGE_KEYS = ("contentUri", "contentUrl")
IMAGE_MAX_BYTES = 64 * 1024 * 1024


def _fetch_image(url: str) -> bytes:
    import urllib.request
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = resp.read(IMAGE_MAX_BYTES + 1)
    if len(data) > IMAGE_MAX_BYTES:
        raise RuntimeError("Embedded image is larger than the comparison limit")
    return data


def _hash_images(value, fetch=None):
    """Every embedded image's signed URL replaced by sha256 of its bytes."""
    import hashlib
    fetch = fetch or _fetch_image
    if isinstance(value, dict):
        return {k: ("sha256:" + hashlib.sha256(fetch(v)).hexdigest()
                    if k in IMAGE_KEYS and isinstance(v, str) else _hash_images(v, fetch))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_hash_images(v, fetch) for v in value]
    return value


def compare_native(auth, source_user, target_user, sid, tid, mime, lookup, fetch=None):
    if mime.endswith(".document"):
        source = (
            auth.api("source", "docs", source_user)
            .documents()
            .get(documentId=sid, includeTabsContent=True)
            .execute()
        )
        target = (
            auth.api("target", "docs", target_user)
            .documents()
            .get(documentId=tid, includeTabsContent=True)
            .execute()
        )
        scope = (
            "all readable Docs tabs, text-run styles, document styles and structures"
        )
    elif mime.endswith(".presentation"):
        source = (
            auth.api("source", "slides", source_user)
            .presentations()
            .get(presentationId=sid)
            .execute()
        )
        target = (
            auth.api("target", "slides", target_user)
            .presentations()
            .get(presentationId=tid)
            .execute()
        )
        scope = "readable Slides pages, masters, layouts, notes and element properties"
    else:
        raise RuntimeError("Native feature reader not implemented for " + mime)

    # A signed image URL is not an identity: each side's differs on every read. Each
    # image is fetched (the URL acts as the requester, for a short while) and compared
    # by its bytes' hash -- Docs call it contentUri, Slides contentUrl.
    source, target = _hash_images(source, fetch), _hash_images(target, fetch)
    a, b = canonical_native(source, lookup), canonical_native(target)
    return (
        [] if a == b else ["native document content, formatting or structure differs"]
    ), scope


# -- Forms ---------------------------------------------------------------------------------------
# What native_api.copy_form cannot carry (no API creates it), named rather than hidden.
FORM_NOT_CARRIED = "fileUploadQuestion"


def _form_shape(form: dict) -> dict:
    """A Form as content: ids the target mints anew (items, questions, sections) dropped, a
    branch's target section renamed by its position, and an image's signed contentUri (new on
    every read) left out. Whitespace in the title is normalised, as copy_form writes it."""
    sections = {}
    for item in form.get("items", []):
        if "pageBreakItem" in item:
            sections[item.get("itemId")] = f"section {len(sections) + 1}"

    def clean(value):
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if k in ("itemId", "questionId", "contentUri"):
                    continue
                out[k] = sections.get(v, v) if k == "goToSectionId" else clean(v)
            return out
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    info = form.get("info") or {}
    quiz = ((form.get("settings") or {}).get("quizSettings") or {}).get("isQuiz", False)
    return {"title": " ".join((info.get("title") or "").split()), "description": info.get("description") or "",
            "quiz": bool(quiz), "items": [clean(i) for i in form.get("items", [])]}


def compare_form(src_forms, tgt_forms, source_id: str, target_id: str) -> list[str]:
    """Differences between a source Form and its rebuilt copy, by content; [] when the same.
    Past responses and the responses-Sheet link are outside a Form's body and not compared."""
    a = _form_shape(src_forms.forms().get(formId=source_id).execute())
    b = _form_shape(tgt_forms.forms().get(formId=target_id).execute())
    diffs = []
    for key in ("title", "description", "quiz"):
        if a[key] != b[key]:
            diffs.append(f"Form {key} differs")
    uncarried = [i for i in a["items"] if FORM_NOT_CARRIED in ((i.get("questionItem") or {}).get("question") or {})]
    if uncarried:
        diffs.append(f"{len(uncarried)} file-upload question(s) not carried: no API can create one")
    wanted = [i for i in a["items"] if i not in uncarried]
    if len(wanted) != len(b["items"]):
        diffs.append(f"Form has {len(b['items'])} item(s) on the target, {len(wanted)} expected")
    for n, (x, y) in enumerate(zip(wanted, b["items"]), 1):
        if x != y:
            diffs.append(f"Form item {n} ({x.get('title') or 'untitled'}) differs")
    return diffs
