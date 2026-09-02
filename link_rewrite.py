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
DRIVE_ID = re.compile(rb"(?:/d/|/folders/|[?&](?:amp;)?id=)([A-Za-z0-9_-]{20,})")


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
                  b"<a href=3D'/uc?export=3Dview&amp;id=SRC_" + b"a" * 24 + b"'>x</a>"):
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
