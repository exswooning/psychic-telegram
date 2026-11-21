"""Compare API-visible native structures, with explicit unsupported features.

Generated identities are renamed consistently across declarations and references;
text/format/layout fields remain present. This does not certify revision history.
"""

from __future__ import annotations

import os
import difflib
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


def _pruned(value):
    """A style with its empty parts removed: Docs reports an unset colour as {} on one run and leaves the key
    out on another, and the two mean the same thing."""
    if isinstance(value, dict):
        out = {k: _pruned(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in ({}, None)}
    return value


def _merge_runs(document):
    """Each Docs paragraph's text runs, merged where what a reader sees is the same.

    A run's look is its paragraph's named style with the run's own style over it. Docs keeps a style equal to
    that default as "unset" ({}), so a copy whose rewritten link id was written by replaceAllText reads as three
    runs where its source has one -- same text, same look (a. -> b., 17 Nov 2026: every rewritten Doc failed
    its check). Runs whose effective styles are equal become one, on both sides alike."""
    if not isinstance(document, dict) or "namedStyles" not in document and "tabs" not in document:
        return document
    import copy

    def named_styles(doc):
        return {s.get("namedStyleType"): s.get("textStyle") or {}
                for s in ((doc.get("namedStyles") or {}).get("styles") or [])}

    def fix(node, named):
        if isinstance(node, dict):
            if isinstance(node.get("documentTab"), dict):
                named = named_styles(node["documentTab"]) or named
            para = node.get("paragraph")
            if isinstance(para, dict) and isinstance(para.get("elements"), list):
                base = named.get((para.get("paragraphStyle") or {}).get("namedStyleType") or "NORMAL_TEXT", {})
                merged = []
                for el in para["elements"]:
                    run = el.get("textRun")
                    eff = _pruned({**base, **(run.get("textStyle") or {})}) if run is not None else None
                    prev = merged[-1] if merged else None
                    if (run is not None and prev is not None and prev.get("textRun") is not None
                            and prev["_eff"] == eff and not run.get("suggestedInsertionIds")
                            and not run.get("suggestedDeletionIds")):
                        prev["textRun"]["content"] = prev["textRun"].get("content", "") + run.get("content", "")
                        if "endIndex" in el:
                            prev["endIndex"] = el["endIndex"]
                        continue
                    el = copy.deepcopy(el)
                    if run is not None:
                        el["textRun"]["textStyle"] = eff
                    el["_eff"] = eff
                    merged.append(el)
                for el in merged:
                    el.pop("_eff", None)
                para["elements"] = merged
            for v in node.values():
                fix(v, named)
        elif isinstance(node, list):
            for v in node:
                fix(v, named)

    doc = copy.deepcopy(document)
    fix(doc, named_styles(doc))
    return doc


def canonical_native(document, lookup=lambda _: None):
    # visit builds every list/dict anew and never mutates the input. A preliminary
    # deepcopy kept a second full document resident for no comparison benefit.
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
            if key == "useCustomHeaderFooterMargins":
                # Derived by Docs and unwritable ("Unallowed field"); an imported Doc reads True. The margins it
                # describes are compared themselves (a. -> b., 21 Nov 2026).
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

    return visit(document)


IMAGE_KEYS = ("contentUri", "contentUrl")
IMAGE_MAX_BYTES = 64 * 1024 * 1024


def _fetch_image(url: str, deadline: float | None = None) -> bytes:
    """urlopen's timeout is per socket read, so a slow-dripping image never timed out and a Doc with
    many images ran far past its budget (education@, live 7 Nov 2026: one Doc 11+ min inside ONE
    counted call). Read in chunks and stop at the caller's monotonic deadline."""
    import time
    import urllib.request
    from resilience import RetryBudgetExceeded
    data = b""
    with urllib.request.urlopen(url, timeout=30) as resp:
        while True:
            if deadline is not None and time.monotonic() > deadline:
                raise RetryBudgetExceeded("this file's time budget ran out while reading an embedded image; "
                                          "recorded as unverified", None)
            chunk = resp.read1(1 << 16)      # read(n) would wait until all n bytes arrived
            if not chunk:
                break
            data += chunk
            if len(data) > IMAGE_MAX_BYTES:
                raise RuntimeError("Embedded image is larger than the comparison limit")
    return data


def _image_urls(value, out: dict) -> dict:
    """Every distinct embedded-image URL in a document, first seen first."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k in IMAGE_KEYS and isinstance(v, str):
                out.setdefault(v, None)
            else:
                _image_urls(v, out)
    elif isinstance(value, list):
        for v in value:
            _image_urls(v, out)
    return out


def _hash_images(value, fetch=None, workers: int | None = None):
    """Every embedded image's signed URL replaced by sha256 of its bytes. Each distinct URL is fetched
    once, IMAGE_FETCH_WORKERS (8) at a time: one after another, an image-heavy Doc could not be read
    inside its 300 s budget and stayed UNVERIFIED on every check (education@, live 7 Nov 2026)."""
    import hashlib
    fetch = fetch or _fetch_image
    urls = list(_image_urls(value, {}))
    if not urls:
        return value
    workers = max(1, min(len(urls), workers or int(os.getenv("IMAGE_FETCH_WORKERS", "8") or 8)))
    if workers == 1:
        digests = {u: "sha256:" + hashlib.sha256(fetch(u)).hexdigest() for u in urls}
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="verify-image") as pool:
            digests = dict(zip(urls, pool.map(lambda u: "sha256:" + hashlib.sha256(fetch(u)).hexdigest(), urls)))

    def swap(v):
        if isinstance(v, dict):
            return {k: (digests[x] if k in IMAGE_KEYS and isinstance(x, str) else swap(x)) for k, x in v.items()}
        if isinstance(v, list):
            return [swap(x) for x in v]
        return v
    return swap(value)


def _side(side: str, endpoint: str, read):
    """Run one read and, if it fails, say which tenant and endpoint it was: a 404 on the source is
    MISSING_SOURCE and one on the target MISSING_TARGET, a 503 is Google's (verify_resilience.classify)."""
    try:
        return read()
    except Exception as exc:
        exc.bitport_side, exc.bitport_endpoint = side, endpoint
        raise


def compare_native(auth, source_user, target_user, sid, tid, mime, lookup, fetch=None, pace=lambda: None):
    if mime.endswith(".document"):
        endpoint = "docs.googleapis.com/v1/documents.get"
        source = _side("source", endpoint, lambda: (
            pace() or auth.api("source", "docs", source_user)
            .documents().get(documentId=sid, includeTabsContent=True).execute()))
        target = _side("target", endpoint, lambda: (
            pace() or auth.api("target", "docs", target_user)
            .documents().get(documentId=tid, includeTabsContent=True).execute()))
        scope = (
            "all readable Docs tabs, text-run styles, document styles and structures"
        )
    elif mime.endswith(".presentation"):
        endpoint = "slides.googleapis.com/v1/presentations.get"
        source = _side("source", endpoint, lambda: (
            pace() or auth.api("source", "slides", source_user)
            .presentations().get(presentationId=sid).execute()))
        target = _side("target", endpoint, lambda: (
            pace() or auth.api("target", "slides", target_user)
            .presentations().get(presentationId=tid).execute()))
        scope = "readable Slides pages, masters, layouts, notes and element properties"
    else:
        raise RuntimeError("Native feature reader not implemented for " + mime)

    # A signed image URL is not an identity: each side's differs on every read. Each
    # image is fetched (the URL acts as the requester, for a short while) and compared
    # by its bytes' hash -- Docs call it contentUri, Slides contentUrl.
    raw_source, raw_target = source, target
    source, target = _hash_images(source, fetch), _hash_images(target, fetch)
    if mime.endswith(".document"):
        source, target = _merge_runs(source), _merge_runs(target)
    a, b = canonical_native(source, lookup), canonical_native(target)
    if a != b and mime.endswith(".presentation"):
        # A .pptx round trip (a read-only source) rebuilds the layouts and masters and spells out inherited
        # properties, while every slide renders pixel-identical (11 decks, P3.7 run 7, 21 Nov 2026). A deck is
        # then judged by what a person sees: Google's own rendering of each slide, compared pixel by pixel.
        differs = _slides_render_differently(auth, source_user, target_user, sid, tid,
                                             raw_source, raw_target, fetch or _fetch_image, pace)
        if not differs:
            return [], scope + "; slides compared as rendered (their stored structure differs)"
        return [f"slide {n} renders differently" for n in differs], scope
    if a != b and mime.endswith(".document"):
        # Every import of an Office export stores a Doc its own way, while what a person sees may be identical
        # (22 Nov 2026: RTF imports render pixel-identical with a different structure). Judged by Google's own
        # rendering, page by page, when it can be made; otherwise the structure verdict stands.
        pages = _doc_pages_render_differently(auth, source_user, target_user, sid, tid)
        if pages == []:
            return [], scope + "; pages compared as rendered (their stored structure differs)"
        if pages:
            return [f"page {n} renders differently" for n in pages], scope
    return (
        [] if a == b else ["native document content, formatting or structure differs"]
    ), scope


def _render_pdf(data: bytes) -> list:
    """A PDF's pages as images (PyMuPDF). Kept apart so a test can stand in for the renderer: PyMuPDF's native
    library segfaults inside pytest on macOS / Python 3.14 (22 Nov 2026), never in the verify's own process."""
    import io
    import pymupdf
    from PIL import Image
    doc = pymupdf.open(stream=io.BytesIO(data), filetype="pdf")
    pages = [p.get_pixmap(dpi=80) for p in doc]
    return [Image.frombytes("RGB", (p.width, p.height), p.samples) for p in pages]


def _doc_pages_render_differently(auth, source_user, target_user, sid, tid):
    """Page numbers whose rendering differs ([] when none), or None when the pages cannot be rendered here: no PDF
    renderer installed (PyMuPDF), or Google will not export the Doc as PDF (over its export ceiling)."""
    try:
        from PIL import ImageChops
        a, b = (_render_pdf(d.files().export(fileId=i, mimeType="application/pdf").execute())
                for d, i in ((auth.source_drive(source_user), sid), (auth.target_drive(target_user), tid)))
    except Exception:      # noqa: BLE001 - not renderable here: the structure verdict stands
        return None
    if len(a) != len(b):
        return list(range(1, max(len(a), len(b)) + 1))
    return [n + 1 for n, (ia, ib) in enumerate(zip(a, b))
            if ia.size != ib.size or ImageChops.difference(ia, ib).convert("L").point(lambda v: 255 if v > 16 else 0).getbbox()]


def _slides_render_differently(auth, source_user, target_user, sid, tid, source, target, fetch, pace=lambda: None):
    """Slide numbers (1-based) whose rendered thumbnails differ; every slide when the decks' slide counts differ."""
    import io
    from PIL import Image, ImageChops
    ss, ts = source.get("slides") or [], target.get("slides") or []
    if len(ss) != len(ts):
        return list(range(1, max(len(ss), len(ts)) + 1))

    def picture(side, user, pid, page):
        pace()
        url = auth.api(side, "slides", user).presentations().pages().getThumbnail(
            presentationId=pid, pageObjectId=page, thumbnailProperties_thumbnailSize="MEDIUM",
            thumbnailProperties_mimeType="PNG").execute()["contentUrl"]
        return Image.open(io.BytesIO(fetch(url))).convert("RGB")

    out = []
    for n, (sp, tp) in enumerate(zip(ss, ts), 1):
        a = picture("source", source_user, sid, sp["objectId"])
        b = picture("target", target_user, tid, tp["objectId"])
        if a.size != b.size or ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 16 else 0).getbbox():
            out.append(n)
    return out


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
    # Align by title and kind; compare the full canonical content afterward.
    # A missing image must not make every following question look changed.
    # Never collapse duplicates or broaden the operator's accepted exceptions.
    def signature(item):
        kind = next((k for k in item if k.endswith("Item")), "unknown")
        return (item.get("title") or "", kind)

    target = b["items"]
    matcher = difflib.SequenceMatcher(a=[signature(i) for i in wanted],
                                     b=[signature(i) for i in target], autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        paired = min(i2 - i1, j2 - j1) if op in ("equal", "replace") else 0
        for offset in range(paired):
            x, y = wanted[i1 + offset], target[j1 + offset]
            if x != y:
                diffs.append(f"Form item {i1 + offset + 1} ({x.get('title') or 'untitled'}) differs")
        for index in range(i1 + paired, i2):
            diffs.append(f"Form item {index + 1} ({wanted[index].get('title') or 'untitled'}) missing")
        for index in range(j1 + paired, j2):
            diffs.append(f"Form target item {index + 1} ({target[index].get('title') or 'untitled'}) extra")
    return diffs
