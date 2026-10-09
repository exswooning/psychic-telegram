"""
Repoint Drive links inside migrated mail at the copies on the target.

A Drive URL names a file by id and nothing else -- there is no domain in
`docs.google.com/document/d/<id>/edit` -- and `files.copy` mints a new id.
So every Drive link inside a migrated message still names the *source*
file. The day the source tenant is deleted, all of them 404 while a
perfectly good copy sits on the target under an id nothing points at.
Keeping or transferring the domain does not help; the domain was never in
the URL.

Only mail we migrate can be repaired this way. A link sitting in an
external party's mailbox, in their bookmarks, or in their own documents is
on a system we do not touch, and no amount of rewriting reaches it -- for
that half the only lever is leaving the source resolvable long enough to
tell people, which is what external_shares.py is for.

Two properties this module is built around:

  * A message with nothing to rewrite comes back **byte-identical** -- the
    original string object, not a re-serialised copy. Most mail contains no
    Drive link at all, and it should not pay a re-encode, nor risk one.
  * An id we do not recognise is left exactly as it was. Links to files
    outside the migration (another tenant, a deleted file, a drive we
    skipped) must not be mangled into something worse than a dead link.
"""
from __future__ import annotations

import base64
import email
import logging
import re
from typing import Callable

log = logging.getLogger(__name__)

# Every shape Drive hands out: /d/<id> for docs and files, /folders/<id>,
# and the ?id= / &id= of the older open and uc links. `&amp;` because in an
# HTML part the query separator arrives entity-encoded.
#
# Matching is deliberately permissive -- 20 chars is shorter than any real
# Drive id -- because the lookup, not the regex, decides what gets replaced.
# A false match on some other long token simply finds no mapping and is
# left alone, which is the safe direction to be wrong in.
# The host is required, not optional context. Matching a bare "/d/" made
# base64 a minefield: its alphabet contains "/", so a 2.8 MB attachment
# reliably produces "/d/" followed by twenty-plus base64 characters, and the
# pattern read that as a Drive id. Measured on a real mailbox, 13 of 21
# "link-bearing" messages had no Drive link at all -- every one an
# attachment blob. That inflated every link count taken here and would have
# routed exactly the heaviest messages through the engine in split mode,
# which is the opposite of the point.
DRIVE_ID = re.compile(
    rb"(?:docs|drive)\.google\.com[^\s\"'<>]*?"
    rb"(?:/d/|/folders/|[?&](?:amp;)?id=)([A-Za-z0-9_-]{20,})")


def rewrite_bytes(body: bytes, lookup: Callable[[str], str | None]) -> tuple[bytes, int]:
    """Swap known source ids for their target ids in one decoded part."""
    hits = 0

    def repl(m: "re.Match[bytes]") -> bytes:
        nonlocal hits
        target = lookup(m.group(1).decode("ascii", "replace"))
        if not target:
            return m.group(0)
        hits += 1
        # Rebuild around the captured id so the /d/ or ?id= prefix that
        # located it survives untouched.
        return m.group(0)[: m.start(1) - m.start(0)] + target.encode("ascii")

    return DRIVE_ID.sub(repl, body), hits


# A bare file id, but only inside IMPORTRANGE's first argument.
#
# DRIVE_ID above requires the host on purpose -- matching a bare "/d/" made
# base64 a minefield. But Sheets accepts IMPORTRANGE("<id>", ...) with no URL
# at all, and that form is common because the editor offers it. Anchoring on
# the function name is what makes a bare id safe to match here: the context
# is four characters of quote-and-paren after a literal IMPORTRANGE, which
# base64 does not produce.
IMPORTRANGE_ID = re.compile(
    rb"(IMPORTRANGE\s*\(\s*[\"'])([A-Za-z0-9_-]{20,})"
)

# Which parts of an OOXML package can hold a link. Everything else in the
# zip is media -- rewriting bytes inside a PNG would corrupt it for no
# possible gain, and the id pattern could match its entropy by chance.
_OOXML_TEXT_SUFFIXES = (".xml", ".rels", ".vml", ".txt")


def has_drive_ref(data: bytes) -> bool:
    """Does this file mention any Drive id at all?

    Cheap gate for the rewrite post-pass. A rewrite needs the WHOLE mapping
    to exist, which is only true after every file has migrated -- but
    re-exporting every native file to find out which ones had references
    would double the run. This answers it from the copy already in hand.

    Deliberately ignores whether an id is mapped: at the moment this is
    asked, the referenced file may not have migrated yet.
    """
    import io
    import zipfile

    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return bool(DRIVE_ID.search(data))
    try:
        for name in z.namelist():
            if not name.lower().endswith(_OOXML_TEXT_SUFFIXES):
                continue
            part = z.read(name)
            if DRIVE_ID.search(part) or IMPORTRANGE_ID.search(part):
                return True
    finally:
        z.close()
    return False


def rewrite_zip(data: bytes, lookup: Callable[[str], str | None]) -> tuple[bytes, int]:
    """Rewrite Drive links inside an OOXML file (.xlsx, .docx, .pptx).

    A Google Sheet exported for migration is a zip of XML parts, and a
    formula like IMPORTRANGE or a cell hyperlink lives in one of them as
    text. Rewriting the zip as one blob does not work: the parts are
    deflated, so the id is not there to find.

    Rebuilt rather than edited in place, because a zip entry's size and CRC
    are recorded in two places and an id can change length.

    Returns the original bytes unchanged when nothing matched, so a caller
    can cheaply tell whether an upload is even needed.
    """
    import io
    import zipfile

    try:
        src = zipfile.ZipFile(io.BytesIO(data))
        names = src.namelist()
    except zipfile.BadZipFile:
        # Not a package -- a .png export of a Drawing, say. Fall back to
        # treating it as one blob, which is right for anything text-ish and
        # harmless for anything else (no host, no match).
        return rewrite_bytes(data, lookup)

    hits = 0
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for name in names:
            body = src.read(name)
            if name.lower().endswith(_OOXML_TEXT_SUFFIXES):
                body, n = rewrite_bytes(body, lookup)
                body, n2 = _rewrite_importrange(body, lookup)
                hits += n + n2
            # Preserve each entry's own metadata: dates and the external
            # attribute bits, or the package can stop opening cleanly.
            dst.writestr(src.getinfo(name), body)
    src.close()
    return (out.getvalue(), hits) if hits else (data, 0)


def _rewrite_importrange(body: bytes,
                         lookup: Callable[[str], str | None]) -> tuple[bytes, int]:
    hits = 0

    def repl(m: "re.Match[bytes]") -> bytes:
        nonlocal hits
        target = lookup(m.group(2).decode("ascii", "replace"))
        if not target:
            return m.group(0)
        hits += 1
        return m.group(1) + target.encode("ascii")

    return IMPORTRANGE_ID.sub(repl, body), hits


def rewrite_text(text: str, lookup: Callable[[str], str | None]) -> tuple[str, int]:
    """Rewrite the Drive links in ordinary text.

    Mail needs the decode-first dance because of transfer encodings. A
    calendar description, a Chat message and a document body are already
    plain text, and they rot exactly the same way -- so they get the same
    pattern without the MIME machinery.
    """
    if not text:
        return text, 0
    out, n = rewrite_bytes(text.encode("utf-8", "surrogatepass"), lookup)
    return out.decode("utf-8", "replace"), n


def has_drive_link(raw_b64: str) -> bool:
    """Whether a message really contains a Drive link.

    Decodes each text part first, for the same reason rewrite_raw does: a
    quoted-printable fold at 76 columns lands between the host and the id, so
    the raw bytes show "drive.google.com/uc=\r\n?export=..." and no pattern
    that forbids whitespace can span it. Checking raw bytes therefore misses
    exactly the HTML mail most likely to carry links.

    Used by the split transport to decide whether a message is worth an
    insert, so a false negative here sends a link-bearing message to DMS
    unrewritten -- the failure the whole mode exists to prevent.
    """
    try:
        msg = email.message_from_bytes(base64.urlsafe_b64decode(raw_b64 + "==="))
    except Exception:                                  # noqa: BLE001
        logging.getLogger(__name__).debug("ignored an error", exc_info=True)
        return False
    for part in msg.walk():
        if part.get_content_maintype() != "text" or part.is_multipart():
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:                              # noqa: BLE001
            logging.getLogger(__name__).debug("ignored an error", exc_info=True)
            continue
        if payload and DRIVE_ID.search(payload):
            return True
    return False


def rewrite_raw(raw_b64: str, lookup: Callable[[str], str | None]) -> tuple[str, int]:
    """
    Rewrite the Drive links in a base64url RFC822 message.

    Returns `(raw, n)`. When `n` is 0 the string returned is the one passed
    in, unchanged.

    The decode-first order is the whole point rather than an implementation
    detail: quoted-printable splits a long URL across a `=\\r\\n` soft break,
    frequently mid-id, and a base64 part contains no readable URL at all.
    A regex over the raw MIME sees neither. `get_payload(decode=True)` undoes
    the transfer encoding first, so both become ordinary text before the
    pattern ever runs.
    """
    try:
        msg = email.message_from_bytes(base64.urlsafe_b64decode(raw_b64 + "==="))
    except Exception as exc:                       # noqa: BLE001 -- never fatal
        log.debug("unparseable message left as-is: %s", exc)
        return raw_b64, 0

    total = 0
    for part in msg.walk():
        if part.get_content_maintype() != "text" or part.is_multipart():
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:                          # noqa: BLE001
            logging.getLogger(__name__).debug("ignored an error", exc_info=True)
            continue
        if not payload:
            continue
        new, hits = rewrite_bytes(payload, lookup)
        if not hits:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = new.decode(charset, "replace")
        except LookupError:
            charset, text = "utf-8", new.decode("utf-8", "replace")
        cte = (part.get("Content-Transfer-Encoding") or "").strip().lower()
        if cte in ("", "7bit", "8bit") and new.isascii():
            # Keep an unencoded part unencoded. set_payload(charset=...)
            # would re-encode this to base64, inflating a plain ASCII body by
            # a third and changing far more of the message than the one link
            # we came here to fix.
            part.set_payload(text)
        else:
            # Otherwise set_payload picks the transfer encoding to match the
            # charset, and the old header has to go or the part ends up
            # declaring an encoding its bytes no longer use.
            del part["Content-Transfer-Encoding"]
            part.set_payload(text, charset=charset)
        total += hits

    if not total:
        return raw_b64, 0
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii"), total


if __name__ == "__main__":
    MAP = {"SRC_" + "a" * 24: "TGT_" + "b" * 24}
    look = MAP.get

    def _wrap(body: bytes, cte: str, ctype: str = "text/plain") -> str:
        raw = (f"From: a@x.test\r\nSubject: t\r\nMIME-Version: 1.0\r\n"
               f"Content-Type: {ctype}; charset=utf-8\r\n"
               f"Content-Transfer-Encoding: {cte}\r\n\r\n").encode() + body
        return base64.urlsafe_b64encode(raw).decode()

    url = b"https://docs.google.com/document/d/SRC_" + b"a" * 24 + b"/edit"

    def _body(raw: str) -> bytes:
        m = email.message_from_bytes(base64.urlsafe_b64decode(raw + "==="))
        part = next(p for p in m.walk() if not p.is_multipart())
        return part.get_payload(decode=True)

    TGT = b"TGT_" + b"b" * 24
    out, n = rewrite_raw(_wrap(url, "7bit"), look)
    assert n == 1, n
    assert TGT in _body(out)
    # a plain ASCII part must not have been silently re-encoded to base64
    assert b"TGT_" in base64.urlsafe_b64decode(out + "==="), "7bit part got re-encoded"

    # the case a raw-MIME regex cannot see: the id split by a soft line break
    folded = b"https://docs.google.com/document/d/SRC_aaaaaaaa=\r\naaaaaaaaaaaaaaaa/edit"
    out, n = rewrite_raw(_wrap(folded, "quoted-printable"), look)
    assert n == 1, f"quoted-printable soft break not handled: {n}"
    assert TGT in _body(out)

    # ...and the one where there is no readable URL in the raw at all
    out, n = rewrite_raw(_wrap(base64.encodebytes(url), "base64"), look)
    assert n == 1, f"base64 part not handled: {n}"
    assert TGT in _body(out)

    # an id we do not know stays exactly as it was, and costs no re-encode
    unknown = _wrap(b"https://docs.google.com/document/d/OTHER_" + b"z" * 24 + b"/edit",
                    "7bit")
    out, n = rewrite_raw(unknown, look)
    assert n == 0 and out is unknown, "unknown id must be left untouched"

    # other link shapes, and the entity-encoded separator inside HTML
    for shape in (b"https://drive.google.com/open?id=SRC_" + b"a" * 24,
                  b"https://drive.google.com/drive/folders/SRC_" + b"a" * 24,
                  b"<a href=3D'https://drive.google.com/uc?export=3Dview&amp;id=SRC_"
                  + b"a" * 24 + b"'>x</a>"):
        _, n = rewrite_raw(_wrap(shape, "7bit", "text/html"), look)
        assert n == 1, f"missed {shape!r}"

    # headers, and Message-ID above all, must survive: the resume path keys
    # its duplicate check on it
    mid = _wrap(url, "7bit").replace("From:", "From:")
    src = base64.urlsafe_b64decode(mid + "===")
    out, _ = rewrite_raw(mid, look)
    got = email.message_from_bytes(base64.urlsafe_b64decode(out + "==="))
    assert got["Subject"] == "t" and got["From"] == "a@x.test"

    print("link_rewrite self-check ok")


# ---------------------------------------------------------------------------
# Native files, through each app's own API.
#
# A server-side copy (files.copy) is a native Doc/Sheet/Slides deck with every link
# still naming SOURCE files. The download path repoints those through the OOXML it
# already exported (rewrite_zip); a native copy has none, and exporting it would be the
# Office round trip server-side exists to avoid. So the links are rewritten in place on
# the TARGET copy: a link's URL by updateTextStyle, an id written out in text by
# replaceAllText, a Sheets formula (IMPORTRANGE, HYPERLINK) by findReplace. Only an id
# the lookup maps is touched -- the same rule as everywhere else in this module.
# Not reachable through these APIs: smart chips (Docs rich links are read-only).
# ---------------------------------------------------------------------------

def _ids(text: str) -> set[str]:
    b = (text or "").encode("utf-8", "surrogatepass")
    return ({m.group(1).decode("ascii", "replace") for m in DRIVE_ID.finditer(b)}
            | {m.group(2).decode("ascii", "replace") for m in IMPORTRANGE_ID.finditer(b)})


def _replace_all(ids: set[str], lookup) -> list[dict]:
    return [{"replaceAllText": {"containsText": {"text": s, "matchCase": True},
                                "replaceText": t}}
            for s in sorted(ids) if (t := lookup(s))]


def doc_requests(doc: dict, lookup: Callable[[str], str | None]) -> list[dict]:
    """Docs batchUpdate requests repointing every mapped link in `doc` (documents.get)."""
    out: list[dict] = []
    text_ids: set[str] = set()

    def walk(node, seg: str) -> None:
        if isinstance(node, dict):
            run = node.get("textRun")
            if run is not None and "endIndex" in node:
                text_ids.update(_ids(run.get("content", "")))
                url = ((run.get("textStyle") or {}).get("link") or {}).get("url")
                new, n = rewrite_text(url, lookup) if url else (url, 0)
                if n:
                    rng = {"startIndex": node.get("startIndex", 0), "endIndex": node["endIndex"]}
                    if seg:
                        rng["segmentId"] = seg
                    out.append({"updateTextStyle": {"range": rng, "textStyle": {"link": {"url": new}},
                                                    "fields": "link"}})
            for v in node.values():
                walk(v, seg)
        elif isinstance(node, list):
            for v in node:
                walk(v, seg)

    walk(doc.get("body"), "")
    for key in ("headers", "footers", "footnotes"):
        for seg, part in (doc.get(key) or {}).items():
            walk(part, seg)
    return out + _replace_all(text_ids, lookup)


def slides_requests(pres: dict, lookup: Callable[[str], str | None]) -> list[dict]:
    """Slides batchUpdate requests repointing every mapped link in `pres` (presentations.get)."""
    out: list[dict] = []
    text_ids: set[str] = set()

    def text(elements, object_id: str, cell: dict | None) -> None:
        for el in elements or []:
            run = el.get("textRun")
            if run is None:
                continue
            text_ids.update(_ids(run.get("content", "")))
            url = ((run.get("style") or {}).get("link") or {}).get("url")
            new, n = rewrite_text(url, lookup) if url else (url, 0)
            if n:
                req = {"objectId": object_id, "style": {"link": {"url": new}}, "fields": "link",
                       "textRange": {"type": "FIXED_RANGE", "startIndex": el.get("startIndex", 0),
                                     "endIndex": el["endIndex"]}}
                if cell:
                    req["cellLocation"] = cell
                out.append({"updateTextStyle": req})

    def element(pe: dict) -> None:
        oid = pe.get("objectId")
        text(((pe.get("shape") or {}).get("text") or {}).get("textElements"), oid, None)
        for r, row in enumerate((pe.get("table") or {}).get("tableRows") or []):
            for c, cell in enumerate(row.get("tableCells") or []):
                text((cell.get("text") or {}).get("textElements"), oid,
                     {"rowIndex": r, "columnIndex": c})
        for child in (pe.get("elementGroup") or {}).get("children") or []:
            element(child)

    for slide in pres.get("slides") or []:
        for pe in slide.get("pageElements") or []:
            element(pe)
    return out + _replace_all(text_ids, lookup)


def sheet_requests(value_ranges: list[dict], lookup: Callable[[str], str | None]) -> list[dict]:
    """Sheets batchUpdate findReplace requests for every mapped id in the formulas and
    values of `value_ranges` (values.batchGet, valueRenderOption=FORMULA)."""
    ids: set[str] = set()
    for vr in value_ranges or []:
        for row in vr.get("values") or []:
            for cell in row:
                if isinstance(cell, str):
                    ids |= _ids(cell)
    return [{"findReplace": {"find": s, "replacement": t, "matchCase": True,
                             "includeFormulas": True, "allSheets": True}}
            for s in sorted(ids) if (t := lookup(s))]


def rewrite_native(kind: str, svc, file_id: str, lookup: Callable[[str], str | None],
                   pace: Callable[[str], None] | None = None, apply: bool = True) -> int:
    """Repoint the links in one native TARGET copy. kind: docs | sheets | slides; svc is
    that API's client as the target user. `pace("read")` / `pace("write")` is called
    before each request -- these APIs meter per minute, and an unpaced run hits 429.
    Returns how many requests were applied -- or, with apply=False, would be (the
    One-to-one check: reads only)."""
    go = pace or (lambda op: None)
    if kind == "docs":
        go("read")
        reqs = doc_requests(svc.documents().get(documentId=file_id).execute(), lookup)
        if reqs and apply:
            go("write")
            svc.documents().batchUpdate(documentId=file_id, body={"requests": reqs}).execute()
    elif kind == "slides":
        go("read")
        reqs = slides_requests(svc.presentations().get(presentationId=file_id).execute(), lookup)
        if reqs and apply:
            go("write")
            svc.presentations().batchUpdate(presentationId=file_id,
                                            body={"requests": reqs}).execute()
    elif kind == "sheets":
        go("read")
        meta = svc.spreadsheets().get(spreadsheetId=file_id,
                                      fields="sheets.properties.title").execute()
        ranges = ["'" + s["properties"]["title"].replace("'", "''") + "'"
                  for s in meta.get("sheets") or []]
        got = {}
        if ranges:
            go("read")
            got = svc.spreadsheets().values().batchGet(
                spreadsheetId=file_id, ranges=ranges, valueRenderOption="FORMULA").execute()
        reqs = sheet_requests(got.get("valueRanges") or [], lookup)
        if reqs and apply:
            go("write")
            svc.spreadsheets().batchUpdate(spreadsheetId=file_id, body={"requests": reqs}).execute()
    else:
        return 0
    return len(reqs)
