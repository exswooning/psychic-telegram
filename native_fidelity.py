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


def compare_native(auth, source_user, target_user, sid, tid, mime, lookup):
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

    # Signed image URLs are not stable image identities; do not ignore them
    # and then claim embedded image bytes were verified.
    def has_image_uri(value):
        if isinstance(value, dict):
            return "contentUri" in value or any(
                has_image_uri(v) for v in value.values()
            )
        return isinstance(value, list) and any(has_image_uri(v) for v in value)

    if has_image_uri(source) or has_image_uri(target):
        raise RuntimeError(
            "Embedded image bytes need dedicated authenticated hashing; native feature coverage remains incomplete"
        )
    a, b = canonical_native(source, lookup), canonical_native(target)
    return (
        [] if a == b else ["native document content, formatting or structure differs"]
    ), scope
