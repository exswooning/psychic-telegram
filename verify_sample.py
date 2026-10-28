"""
verify_sample.py -- check a migration ONE TO ONE, without taking its word for it.

The ledger records what the engine believes it did. This reads both tenants and
compares what is actually there. The ledger is used for exactly one thing, to say
WHICH source item was paired with WHICH target item; everything that is then
asserted about the pair is read fresh from each side:

  Drive     exists, same name, type, size and MD5, same modified time, same folder
            path, and the CONTENT compared byte for byte (both files downloaded and
            hashed; Google-native docs are exported to text and compared). Plus
            duplicates and strays on the target that the ledger would never mention.
  Gmail     the raw message, byte for byte -- or, where a Drive link was rewritten,
            against the same rewrite recomputed here; where Google added headers,
            said so instead of hiding it. Labels, unread state, dates, and duplicate
            Message-IDs.
  Calendar  the fields a person sees: title, time, place, attendees, recurrence...
  Contacts  names, addresses, numbers, organisations...
  Tasks     title, notes, status, due date, and the list each sits in.

Comparisons only read the tenants. The optional --repair-after-user mode repairs
supported target differences and rechecks that user; the source stays read-only.
A check that could not be made says so and makes the verdict INCOMPLETE; it is never counted as a pass. That is the same rule
the run report follows, and for the same reason.

Run standalone (`python verify_sample.py --account-id 3`), or automatically at the
end of `main.py migrate --verify-after`, which is how a quick migration checks
itself on the server with nobody watching.
"""
from __future__ import annotations

import base64
import email
import hashlib
import json
import logging
import os
import re
import sys
import time
import unicodedata
from collections.abc import Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from email.parser import BytesHeaderParser
from verification_reads import error_detail

# The CLI and repair callbacks must share the same module and cancellation type.
if __name__ == "__main__":
    sys.modules.setdefault("verify_sample", sys.modules[__name__])


class VerificationCancelled(BaseException):
    """Cooperative stop must escape per-item error collectors."""


def _stop_check():
    from resilience import shutdown_requested
    if shutdown_requested():
        raise VerificationCancelled("verification stopped by operator")


HERE = os.path.dirname(os.path.abspath(__file__))
FOLDER = "application/vnd.google-apps.folder"
DRIVE_METADATA_BATCH_SIZE = 25
DRIVE_META_FIELDS = ("id,name,mimeType,parents,size,md5Checksum,modifiedTime,trashed,ownedByMe,version,"
                     "permissions(id,type,role,emailAddress,domain,deleted)")
# Files larger than this are compared by size and MD5 only. Both sides are read into
# memory to hash, and a sample is small; a 2 GB video is not what it is for.
MAX_COMPARE_BYTES = 25 * 1024 * 1024
NATIVE_EXPORT = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
# Headers Google is known to add when it stores a message. Their presence on the
# target and not the source is reported as "equivalent", never as a difference and
# never silently.
VOLATILE_HEADERS = {"received", "x-received", "x-gm-message-state", "x-google-smtp-source",
                    "return-path", "delivered-to", "authentication-results", "received-spf",
                    "arc-seal", "arc-message-signature", "arc-authentication-results"}
PERSON_FIELDS = "names,emailAddresses,phoneNumbers,organizations,addresses,birthdays,biographies,urls,nicknames"
PEOPLE_BATCH_SIZE = 200  # People API getBatchGet limit; halves calls for every 200 contacts per tenant.
GMAIL_BATCH_SIZE = 10  # Bound response memory while collapsing per-message HTTP round trips.
VERIFY_PROGRESS_EVERY = 500  # Keep large-mailbox job logs useful without one line per message.
log = logging.getLogger(__name__)

ALL_SERVICES = ("drive", "gmail", "calendar", "contacts", "tasks", "chat", "directory")


def _gmail_batch_size():
    """Stay within Gmail's recommended maximum; ten remains the memory-safe default."""
    try:
        return max(1, min(50, int(os.getenv("VERIFY_GMAIL_BATCH_SIZE", str(GMAIL_BATCH_SIZE)))))
    except ValueError:
        return GMAIL_BATCH_SIZE


def _gmail_read_window():
    """How many batch pairs are read ahead (VERIFY_GMAIL_WINDOW, 1-8; default 4)."""
    try:
        return max(1, min(8, int(os.getenv("VERIFY_GMAIL_WINDOW", "4"))))
    except ValueError:
        return 4


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------------------
# Comparisons. Pure: dicts and bytes in, a list of differences out (empty = the
# same). Kept apart from the fetching so each rule can be tested on its own.
# ---------------------------------------------------------------------------
def compare_drive_file(src: dict, tgt: dict, src_path: str, tgt_path: str) -> list[str]:
    """Metadata of one migrated Drive item. `src`/`tgt` carry name, mimeType, size,
    md5Checksum, modifiedTime."""
    out = []
    for key, label in (("name", "name"), ("mimeType", "type")):
        if src.get(key) != tgt.get(key):
            out.append(f"{label} differs: {src.get(key)!r} -> {tgt.get(key)!r}")
    if src.get("mimeType") != FOLDER:
        if src.get("md5Checksum") != tgt.get("md5Checksum"):
            out.append(f"MD5 differs: {src.get('md5Checksum')} -> {tgt.get('md5Checksum')}")
        if str(src.get("size")) != str(tgt.get("size")) and src.get("md5Checksum"):
            out.append(f"size differs: {src.get('size')} -> {tgt.get('size')}")
    if (src.get("modifiedTime") or "")[:19] != (tgt.get("modifiedTime") or "")[:19]:
        out.append(f"modified time not preserved: {src.get('modifiedTime')} -> {tgt.get('modifiedTime')}")
    if src_path != tgt_path:
        out.append(f"folder path differs: {src_path!r} -> {tgt_path!r}")
    return out


def normalise_message(raw: bytes) -> tuple[dict, bytes]:
    """(headers minus the ones Google adds, payload) -- for the case where the bytes
    differ and the question is whether the MESSAGE does."""
    msg = email.message_from_bytes(raw)
    headers: dict[str, list[str]] = {}
    for k, v in msg.items():
        if k.lower() in VOLATILE_HEADERS:
            continue
        headers.setdefault(k.lower(), []).append(" ".join(str(v).split()))
    body = b"".join((p.get_payload(decode=True) or b"") for p in msg.walk() if not p.is_multipart())
    return headers, body


def compare_message(src_raw: bytes, tgt_raw: bytes, expected_after_rewrite: bytes | None = None) -> tuple[str, list[str]]:
    """('identical' | 'identical after rewrite' | 'equivalent' | 'different', notes)."""
    if src_raw == tgt_raw:
        return "identical", []
    if expected_after_rewrite is not None and expected_after_rewrite == tgt_raw:
        return "identical after rewrite", ["Drive links repointed at the copies on the target, as intended"]
    want = expected_after_rewrite if expected_after_rewrite is not None else src_raw
    sh, sb = normalise_message(want)
    th, tb = normalise_message(tgt_raw)
    assigned_id = "message-id" not in sh and "message-id" in th
    if assigned_id:
        th.pop("message-id")
    if sh == th and sb == tb:
        if assigned_id:
            return "equivalent", ["Gmail assigned a Message-ID absent from the source; other compared headers and payload match"]
        added = sorted(set(BytesHeaderParser().parsebytes(tgt_raw).keys()) - set(BytesHeaderParser().parsebytes(want).keys()))
        return "equivalent", [f"same message; headers added on the target: {', '.join(added) or 'none named'}"]
    notes = []
    for k in sorted(set(sh) | set(th)):
        if sh.get(k) != th.get(k):
            notes.append(f"header {k}: {sh.get(k)} -> {th.get(k)}")
    if sb != tb:
        notes.append(f"body differs ({len(sb)} -> {len(tb)} bytes)")
    return "different", notes


def _gone_from_source(exc, res: dict) -> bool:
    """A 404 reading the SOURCE: deleted there after it was copied. Nothing is left to
    compare, so it is neither a check made nor one that could not be -- it is counted and
    said (two contacts turned a user INCOMPLETE, 2026-10-10)."""
    if not _is_not_found(exc):
        return False
    res["checked"] -= 1
    res["goneFromSource"] = res.get("goneFromSource", 0) + 1
    return True


def _is_not_found(exc) -> bool:
    """Recognise a real 404 both before and after resilience wraps Google errors.

    `retry_on_google_error` turns permanent HttpErrors into PermanentAPIError, so looking
    only at `exc.resp.status` misses those 404s. Quota, transport, and permission failures
    must remain INCOMPLETE; they are not proof that an item is missing.
    """
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status == 404:
        return True
    text = str(exc)
    match = re.search(r"\bHTTP\s+(\d{3})\b", text, re.IGNORECASE)
    if match and int(match.group(1)) == 404:
        return True
    return bool(re.search(r"\bnotFound\b", text, re.IGNORECASE))


def compare_grants(src_perms: list[dict], tgt_perms: list[dict], translate, src_domain: str, tgt_domain: str,
                   skipped_ids: set[str], source_id: str) -> dict:
    """Who can see one file, source against target.

    Each grant is reduced to (who, role) -- the thing a person would notice
    changing -- with the source's people translated to their target addresses.
      matched         on both
      notReproduced   on the source, not the target, AND the migration recorded that it
                      deliberately did not create it (the grantee has no account on the
                      target). Said, never hidden -- but not a fault.
      missing         on the source, not the target, and nothing explains it
      extra           on the target and NOT on the source: access nobody granted
    """
    from acl_audit import _grant_key
    want = {k for k in (_grant_key(p, translate, src_domain, tgt_domain) for p in src_perms if not p.get("deleted")) if k}
    got = {k for k in (_grant_key(p, lambda e: e) for p in tgt_perms if not p.get("deleted")) if k}

    def audit_id(k):
        kind, who, _role = k
        return f"{source_id}:{who}" if kind in ("user", "group") else (
            f"{source_id}:domain:{who}" if kind == "domain" else f"{source_id}:anyone")
    missing = sorted(want - got)
    explained = [k for k in missing if audit_id(k) in skipped_ids]
    return {"matched": len(want & got), "notReproduced": [list(k) for k in explained],
            "missing": [list(k) for k in missing if k not in explained], "extra": [list(k) for k in sorted(got - want)]}


def compare_draft(src_raw: bytes, tgt_raw: bytes, translate) -> tuple[str, list[str]]:
    """('equivalent' | 'different', differences). Gmail rebuilds a draft's Message-Id, Date and From
    when it is created, so no copy of a draft is byte-identical to its source. What a person wrote
    is who it is addressed to, its subject and its body (attachments included) -- those are compared,
    and From only after the migration's own address mapping."""
    from email.policy import default
    from email.utils import parseaddr
    # policy.default reads a raw UTF-8 header and an RFC 2047 encoded one to the same text.
    s = email.message_from_bytes(src_raw, policy=default)
    t = email.message_from_bytes(tgt_raw, policy=default)

    def hdr(m, k):
        return " ".join(str(m.get(k, "")).split())
    diffs = [f"{k}: {hdr(s, k)!r} -> {hdr(t, k)!r}"
             for k in ("to", "cc", "bcc", "subject", "reply-to", "in-reply-to", "references") if hdr(s, k) != hdr(t, k)]
    sa, ta = parseaddr(str(s.get("from", "")))[1].lower(), parseaddr(str(t.get("from", "")))[1].lower()
    if translate(sa) != ta:
        diffs.append(f"from: {sa} -> {ta}")
    if normalise_message(src_raw)[1] != normalise_message(tgt_raw)[1]:
        diffs.append("body or attachments differ")
    return ("different" if diffs else "equivalent"), diffs


# Gmail's coloured "superstars": shown in the Gmail UI, but no API can apply them, so a
# copy never carries one. Not a difference this tool could close.
SUPERSTARS = {"YELLOW_STAR", "ORANGE_STAR", "RED_STAR", "PURPLE_STAR", "BLUE_STAR", "GREEN_STAR",
              "RED_BANG", "ORANGE_GUILLEMET", "YELLOW_BANG", "GREEN_CHECK", "BLUE_INFO",
              "PURPLE_QUESTION"}


def compare_labels(src_names: set[str], tgt_names: set[str]) -> list[str]:
    src_names, tgt_names = set(src_names) - SUPERSTARS, set(tgt_names) - SUPERSTARS
    # Gmail label names are Unicode text. Compare canonical forms and
    # normalized whitespace so visually equivalent names (notably labels
    # containing decomposed Nepali text) do not become false deltas.
    def key(value: str) -> str:
        return " ".join(unicodedata.normalize("NFC", str(value)).split())

    src = {key(name): name for name in src_names}
    tgt = {key(name): name for name in tgt_names}
    out = []
    if src.keys() - tgt.keys():
        out.append(f"labels missing on the target: {sorted(src[k] for k in src.keys() - tgt.keys())}")
    if tgt.keys() - src.keys():
        out.append(f"labels added on the target: {sorted(tgt[k] for k in tgt.keys() - src.keys())}")
    return out


def _norm_when(t: dict | None) -> str:
    """The moment, in UTC: Calendar answers 14:00+05:45 on one side and 08:15Z on the other
    for the same event, and those are equal."""
    if not t:
        return ""
    if t.get("dateTime"):
        try:
            return datetime.fromisoformat(t["dateTime"].replace("Z", "+00:00")) \
                .astimezone(timezone.utc).isoformat()
        except ValueError:
            return t["dateTime"]
    return t.get("date") or ""


def compare_event(src: dict, tgt: dict, translate, lookup=None, notes: list | None = None) -> list[str]:
    """`lookup` maps a source Drive id to its target id. The engine repoints Drive links in an
    event's description and location, so those two fields are equal when the source text put
    through the engine's OWN rewriter is exactly the target's -- said in `notes`, not hidden."""
    out = []
    for f in ("summary", "description", "location", "status", "transparency", "visibility", "colorId"):
        a, b = src.get(f) or None, tgt.get(f) or None
        if a != b and lookup and f in ("description", "location") and a and b:
            from link_rewrite import rewrite_text
            if rewrite_text(a, lookup)[0] == b:
                if notes is not None:
                    notes.append(f"{f}: Drive links repointed at the copies on the target, as intended")
                continue
        if a != b:
            out.append(f"{f}: {a!r} -> {b!r}")
    for f in ("start", "end"):
        a, b = _norm_when(src.get(f)), _norm_when(tgt.get(f))
        if a != b:
            out.append(f"{f}: {a} -> {b}")
    if (src.get("recurrence") or []) != (tgt.get("recurrence") or []):
        out.append(f"recurrence: {src.get('recurrence')} -> {tgt.get('recurrence')}")
    a = sorted({translate((x.get("email") or "").lower()) for x in src.get("attendees") or []})
    # The calendar an event lives in is added as its attendee when that is not the
    # primary -- Google's import rule, not a guest -- and Google marks it `self`. Ignored
    # only when the source never had it.
    b = sorted({e for x in tgt.get("attendees") or [] for e in [(x.get("email") or "").lower()]
                if e in a or not x.get("self")})
    if a != b:
        out.append(f"attendees: {a} -> {b}")
    return out


def _vals(person: dict, key: str, sub: str) -> list[str]:
    return sorted({str((v.get(sub) or "")).strip() for v in person.get(key) or [] if v.get(sub)})


def compare_contact(src: dict, tgt: dict, translate) -> list[str]:
    out = []
    def digits(values): return sorted({re.sub(r"\D", "", v) for v in values})

    def preferred_name(person):
        names = person.get("names") or []
        if not names:
            return {}
        def priority(pair):
            index, name = pair
            metadata = name.get("metadata") or {}
            source = metadata.get("source") or {}
            return bool(metadata.get("primary")), source.get("type") == "CONTACT", -index
        return max(enumerate(names), key=priority)[1]

    def name_aliases(person):
        aliases = set()
        for name in person.get("names") or []:
            text = (name.get("displayName") or "").strip()
            if not text:
                text = " ".join(str(name.get(k) or "").strip() for k in
                                ("honorificPrefix", "givenName", "middleName", "familyName", "honorificSuffix")
                                if str(name.get(k) or "").strip())
            if text:
                aliases.add(" ".join(unicodedata.normalize("NFKC", text).split()).casefold())
        for nickname in person.get("nicknames") or []:
            text = (nickname.get("value") or "").strip()
            if text:
                aliases.add(" ".join(unicodedata.normalize("NFKC", text).split()).casefold())
        return aliases

    src_name, tgt_name = preferred_name(src), preferred_name(tgt)
    for label, a, b in (
        ("given name", [src_name.get("givenName")] if src_name.get("givenName") else [],
         [tgt_name.get("givenName")] if tgt_name.get("givenName") else []),
        ("family name", [src_name.get("familyName")] if src_name.get("familyName") else [],
         [tgt_name.get("familyName")] if tgt_name.get("familyName") else []),
        ("emails", sorted({translate(v.lower()) for v in _vals(src, "emailAddresses", "value")}),
                   sorted({v.lower() for v in _vals(tgt, "emailAddresses", "value")})),
        ("phones", digits(_vals(src, "phoneNumbers", "value")), digits(_vals(tgt, "phoneNumbers", "value"))),
        ("organisations", _vals(src, "organizations", "name"), _vals(tgt, "organizations", "name")),
        ("addresses", _vals(src, "addresses", "formattedValue"), _vals(tgt, "addresses", "formattedValue")),
        ("notes", _vals(src, "biographies", "value"), _vals(tgt, "biographies", "value")),
        ("urls", _vals(src, "urls", "value"), _vals(tgt, "urls", "value")),
    ):
        if a != b:
            out.append(f"{label}: {a} -> {b}")
    if name_aliases(src) != name_aliases(tgt):
        out.append(f"names and nicknames: {sorted(name_aliases(src))} -> {sorted(name_aliases(tgt))}")
    return out


def compare_task(src: dict, tgt: dict) -> list[str]:
    out = []
    for f in ("title", "notes", "status", "completed"):
        a, b = src.get(f) or None, tgt.get(f) or None
        if a != b:
            out.append(f"{f}: {a!r} -> {b!r}")
    if (src.get("due") or "")[:10] != (tgt.get("due") or "")[:10]:
        out.append(f"due: {src.get('due')} -> {tgt.get('due')}")
    return out


# ---------------------------------------------------------------------------
# Reading the tenants
# ---------------------------------------------------------------------------
def _sample(items: list, limit: int | None) -> list:
    """`limit` items spread evenly over the list (all of it when there are fewer), in a fixed
    order so the same ledger is sampled the same way twice -- and a difference found once is
    found again, not lost to a different draw."""
    if not limit or len(items) <= limit:
        return items
    ordered = sorted(items, key=lambda x: str(x[0]))
    return [ordered[int(i * len(ordered) / limit)] for i in range(limit)]


# -- reusing a Drive content comparison ----------------------------------------
# Opening both copies (download, export, Docs/Sheets/Slides structure, image hashes) is
# the slow part of a Drive check: hours for a big user, and all of it again after any
# interruption. Drive's `version` rises on every change to a file, so a comparison made
# when the source and the target were at these exact versions, by this exact comparison
# code, cannot have a different answer now. Reused only then; a failed read is never kept.
_CODE_DIGEST = None


def _comparison_code() -> str:
    global _CODE_DIGEST
    if _CODE_DIGEST is None:
        import hashlib as _h
        from pathlib import Path
        here = Path(__file__).resolve().parent
        _CODE_DIGEST = _h.sha256(b"".join(
            (here / f).read_bytes() for f in ("verify_sample.py", "native_fidelity.py", "native_repair.py",
                                              "verification_reads.py", "link_rewrite.py")
            if (here / f).exists())).hexdigest()[:24]
    return _CODE_DIGEST


def _cache_table(db) -> None:
    with db.write() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS drive_content_cache (
            source_user TEXT NOT NULL, source_id TEXT NOT NULL, target_id TEXT NOT NULL,
            source_version TEXT NOT NULL, target_version TEXT NOT NULL, code TEXT NOT NULL,
            diffs TEXT NOT NULL, compared_at TEXT NOT NULL,
            PRIMARY KEY (source_user, source_id, target_id))""")


def drive_content_cache_get(db, user, sid, tid, sm, tm):
    sv, tv = sm.get("version"), tm.get("version")
    if not sv or not tv or not hasattr(db, "write"):
        return None
    try:
        row = db.conn.execute("SELECT source_version, target_version, code, diffs, compared_at FROM "
                              "drive_content_cache WHERE source_user=? AND source_id=? AND target_id=?",
                              (user, sid, tid)).fetchone()
    except Exception:      # noqa: BLE001 - no table yet: nothing to reuse
        return None
    if not row or (row[0], row[1], row[2]) != (str(sv), str(tv), _comparison_code()):
        return None
    kept = json.loads(row[3])
    if isinstance(kept, list):                 # never written this way; read as "not reusable"
        return None
    return {"diffs": kept["diffs"], "ev": kept.get("ev", {}), "at": row[4]}


# What a comparison established about the file besides its differences -- the coverage
# rules read these (verification_coverage: native features checked / matched), so a
# reused result carries them or it would read as "not checked".
CACHED_EVIDENCE = ("opened", "how", "bytes", "sha256", "nativeFeaturesChecked", "nativeFeaturesMatched")


def drive_content_cache_put(db, user, sid, tid, sm, tm, diffs, ev=None) -> None:
    sv, tv = sm.get("version"), tm.get("version")
    if not sv or not tv or not hasattr(db, "write"):
        return
    try:
        _cache_table(db)
        with db.write() as c:
            c.execute("INSERT OR REPLACE INTO drive_content_cache VALUES (?,?,?,?,?,?,?,?)",
                      (user, sid, tid, str(sv), str(tv), _comparison_code(),
                       json.dumps({"diffs": diffs, "ev": {k: (ev or {})[k] for k in CACHED_EVIDENCE if k in (ev or {})}}),
                       datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    except Exception as exc:      # noqa: BLE001 - a cache that cannot be written only costs time
        log.warning("drive comparison cache not written for %s: %s", sid, exc)


def copy_lookup(db, target_bytes: bytes):
    """The rewriter's lookup, for checking one target item: a source id resolves to
    the copy this item actually names when that is one of its genuine copies (one
    file shared with many users has one copy per user), otherwise to the default
    pick. A link to anything that is not a copy of that source file still differs."""
    many = getattr(db, "targets_for_source_id", None)

    def lookup(source_id: str):
        if many:
            for tid in many(source_id):
                if tid.encode() in target_bytes:
                    return tid
        return db.target_for_source_id(source_id)
    return lookup


class Verifier:
    def __init__(self, auth, db, settings, source_user: str, target_user: str, retry=lambda f: f,
                 limit: int | None = None, on_api_attempt=None, on_progress=None):
        self.auth, self.db, self.settings = auth, db, settings
        self.src_user, self.tgt_user = source_user, target_user
        self._retry = retry
        self.evidence: list[dict] = []
        # None checks every item the ledger pairs (a sample migration, a small user); a number
        # checks that many of each kind, evenly spread -- see verify_user.
        self.limit = limit
        self._picked: dict[str, tuple[int, int]] = {}
        self._on_api_attempt = on_api_attempt
        self._on_progress = on_progress
        self._operation = "initialising verifier"

    def _report_progress(self, operation: str, completed: int | None = None,
                         total: int | None = None, item: str | None = None) -> None:
        self._operation = operation
        if self._on_progress:
            self._on_progress(operation=operation, completed=completed, total=total, item=item)

    # -- plumbing ---------------------------------------------------------
    def _x(self, fn, operation: str | None = None, request_count: int = 1):
        """Run one API attempt under retry policy, counting retries as activity."""
        _stop_check()
        operation = operation or self._operation
        attempt = 0
        def counted():
            nonlocal attempt
            _stop_check()
            attempt += 1
            if self._on_api_attempt:
                self._on_api_attempt("start", operation=operation, request_count=request_count,
                                     attempt=attempt)
            try:
                result = fn()
            except Exception as exc:
                if self._on_api_attempt:
                    self._on_api_attempt("finish", operation=operation, request_count=request_count,
                                         attempt=attempt, error=f"{type(exc).__name__}: {str(exc)[:180]}")
                raise
            else:
                if self._on_api_attempt:
                    self._on_api_attempt("finish", operation=operation, request_count=request_count,
                                         attempt=attempt)
                return result
        return self._retry(counted)()

    def _translate(self, addr: str) -> str:
        # A secondary calendar named as a guest maps through its calendar mapping, as
        # calendar_engine._map_address maps it.
        if not addr:
            return addr
        return (self.db.resolve_identity(addr)
                or self.db.target_for_source_id(addr.lower(), ("calendar", "group")) or addr).lower()

    def _pairs(self, type_: str, sample: bool = False) -> list[tuple[str, str, str | None]]:
        """Everything the ledger paired -- or, with `sample`, the part of it to open and compare.
        Lookups (which target ids the ledger knows, task ids, calendars) always take the whole."""
        rows = [(r["source_id"], r["target_id"], r["source_name"]) for r in self.db.conn.execute(
            "SELECT source_id, target_id, source_name FROM id_mapping WHERE source_user=? AND type=?",
            (self.src_user, type_))]
        if not sample:
            return rows
        use = _sample(rows, self.limit)
        self._picked[type_] = (len(use), len(rows))
        return use

    def _note_sample(self, res: dict, *types: str) -> None:
        n = sum(self._picked.get(t, (0, 0))[0] for t in types)
        m = sum(self._picked.get(t, (0, 0))[1] for t in types)
        if self.limit and n < m:
            res["sampled"] = {"checked": n, "of": m}
            res["notes"].append(f"checked {n} of {m} items -- an evenly spaced sample of up to {self.limit} of each kind")

    def _failed(self, types: tuple[str, ...]) -> list[dict]:
        """Items the engine tried and failed to copy -- INCLUDING a whole service that
        failed for this user, which migrate_user records under the service's own name
        ('drive', 'gmail', ...). Missing that is how a run that copied nothing at all
        would read as identical."""
        q = ",".join("?" * len(types))
        items = types[:-1]          # every caller ends `types` with the service's own name
        rows = self.db.conn.execute(
            f"SELECT item_id, item_type, error_message, timestamp FROM audit_log WHERE source_user=? "
            f"AND status LIKE 'FAILED%' AND item_type IN ({q})", (self.src_user, *types)).fetchall()
        out = []
        for r in rows:
            if r["item_type"] == types[-1] and self.db.conn.execute(
                    f"SELECT 1 FROM audit_log WHERE source_user=? AND status='SUCCESS' AND timestamp>? "
                    f"AND item_type IN ({','.join('?' * len(items))}) LIMIT 1",
                    (self.src_user, r["timestamp"], *items)).fetchone():
                continue            # the service failed once, and a later run of it copied items
            out.append({"id": r["item_id"], "type": r["item_type"], "error": (r["error_message"] or "")[:200]})
        return out

    @staticmethod
    def _blank() -> dict:
        return {"checked": 0, "identical": 0, "differences": [], "missing": [], "extras": [],
                "duplicates": [], "notCopied": [], "errors": [], "notes": []}

    # -- Drive ------------------------------------------------------------
    def _remember_drive_permissions(self, svc, fid, metadata):
        if "permissions" in metadata:
            if not hasattr(self, "_drive_permission_cache"):
                self._drive_permission_cache = {}
            self._drive_permission_cache[(id(svc), fid)] = metadata.pop("permissions")
        return metadata

    def _drive_meta(self, svc, fid):
        try:
            metadata = self._x(lambda: svc.files().get(
                fileId=fid, supportsAllDrives=True, fields=DRIVE_META_FIELDS).execute())
        except Exception as exc:
            if getattr(getattr(exc, "resp", None), "status", None) != 403:
                raise
            # Some carried-in files expose metadata but not their ACL field.
            metadata = self._x(lambda: svc.files().get(
                fileId=fid, supportsAllDrives=True,
                fields=DRIVE_META_FIELDS.split(",permissions(", 1)[0]).execute())
        return self._remember_drive_permissions(svc, fid, metadata)

    def _drive_meta_many(self, svc, ids, side):
        """Read a bounded window of metadata and ACLs together, keyed by file ID.

        Media reads stay independent. A failed or missing batch response never
        drops an item: the caller handles its error or retries the normal read.
        """
        factory = getattr(svc, "new_batch_http_request", None)
        if not callable(factory) or not ids:
            return {}, {}
        cache = getattr(self, "_drive_permission_cache", {})
        for key in [k for k in cache if k[0] == id(svc)]:
            cache.pop(key, None)  # carried-in items intentionally don't consume ACLs
        result, errors = {}, {}
        def received(request_id, response, error):
            if error is not None:
                errors[request_id] = error
            elif response is not None:
                result[request_id] = self._remember_drive_permissions(svc, request_id, response)
        try:
            batch = factory(callback=received)
            for fid in ids:
                batch.add(svc.files().get(fileId=fid, supportsAllDrives=True,
                                         fields=DRIVE_META_FIELDS), request_id=fid)
            self._x(batch.execute, operation=f"Drive {side} metadata batch", request_count=len(ids))
        except Exception:
            # All operations are reads, so falling back after an ambiguous
            # transport failure is safe and preserves per-item retry policy.
            return {}, {}
        return result, errors

    def _drive_meta_from_batch(self, svc, fid, result, errors):
        if fid in result:
            return result[fid]
        error = errors.get(fid)
        status = getattr(getattr(error, "resp", None), "status", None)
        if error is not None and status not in (403, 408, 429, 500, 502, 503, 504):
            raise error
        return self._drive_meta(svc, fid)

    def _drive_path(self, svc, fid, cache: dict) -> str:
        parts, cur, guard = [], fid, 0
        while cur and guard < 200:
            guard += 1
            if cur not in cache:
                cache[cur] = self._drive_meta(svc, cur)
            m = cache[cur]
            parents = m.get("parents") or []
            if not parents:            # My Drive itself: not part of the path
                break
            parts.append(m.get("name") or "")
            cur = parents[0]
        return "/" + "/".join(reversed(parts))

    def _download(self, svc, fid) -> bytes:
        try:
            return self._x(lambda: svc.files().get_media(fileId=fid, supportsAllDrives=True).execute())
        except Exception as exc:
            if "cannotDownloadAbusiveFile" not in error_detail(exc):
                raise
            # Read only, no execution or unpacking of the flagged content.
            return self._x(lambda: svc.files().get_media(fileId=fid, supportsAllDrives=True,
                                                        acknowledgeAbuse=True).execute())

    def _export(self, svc, fid, mime) -> bytes:
        return self._x(lambda: svc.files().export_media(fileId=fid, mimeType=mime).execute())

    def drive(self) -> dict:
        res = self._blank()
        src, tgt = self.auth.source_drive(self.src_user), self.auth.target_drive(self.tgt_user)
        sc, tc = {}, {}
        # Every target the ledger knows -- not only the ones opened below -- so a sampled
        # run does not mistake the rest of the migration for strays.
        # Shortcuts too: a migrated shortcut is mapped as one, and leaving it out
        # reported every copied shortcut as a stray (live: fiona's shortcut-to-deep-file).
        seen_targets = {tid for kind in ("folder", "file", "shortcut") for _, tid, _ in self._pairs(kind)}
        target_keys = {}
        for kind in ("folder", "file"):
            pairs = self._pairs(kind, sample=True)
            operation = f"Drive compare {kind}s"
            self._report_progress(operation, 0, len(pairs))
            source_meta, source_errors, target_meta, target_errors = {}, {}, {}, {}
            for pair_index, (sid, tid, recorded_name) in enumerate(pairs):
                if pair_index % DRIVE_METADATA_BATCH_SIZE == 0:
                    window = pairs[pair_index:pair_index + DRIVE_METADATA_BATCH_SIZE]
                    source_meta, source_errors = self._drive_meta_many(
                        src, [s for s, t, _ in window if s != t], "source")
                    target_meta, target_errors = self._drive_meta_many(
                        tgt, [t for s, t, _ in window if s != t], "target")
                self._report_progress(operation, pair_index, len(pairs), f"item {sid}")
                res["checked"] += 1
                if sid == tid:
                    # MOVED, not copied (drive_engine._sync_move): the source no longer
                    # holds it, and what is on the target IS the file -- one id, its own
                    # history. So it is checked there: present, not binned, its name.
                    self._check_moved(tgt, sid, recorded_name, res)
                    continue
                try:
                    sm = self._drive_meta_from_batch(src, sid, source_meta, source_errors)
                    sc[sid] = sm
                except Exception as exc:      # noqa: BLE001
                    if _gone_from_source(exc, res):
                        if kind == "file":
                            from copy_resolution import record
                            record(self, res, sid, tid, source_unreadable=True)
                        continue
                    res["errors"].append(f"{kind} {sid}: could not read the SOURCE: {error_detail(exc)}")
                    if kind == "file":
                        from copy_resolution import record
                        record(self, res, sid, tid, source_unreadable=True)
                    continue
                try:
                    tm = self._drive_meta_from_batch(tgt, tid, target_meta, target_errors)
                    tc[tid] = tm
                except Exception as exc:      # noqa: BLE001
                    if _is_not_found(exc):
                        res["missing"].append({"source": sid, "target": tid, "name": sm.get("name"),
                                               "why": f"not on the target ({str(exc)[:80]})"})
                    else:
                        res["errors"].append(f"file {tid}: could not read the TARGET: {error_detail(exc)}")
                    continue
                if tm.get("trashed") and sm.get("trashed"):
                    # Binned on the source too, and the mirror carried it across: the
                    # copy is where its original is (11 of seeduser200's, 2026-10-10).
                    res["identical"] += 1
                    continue
                if tm.get("trashed"):
                    res["missing"].append({"source": sid, "target": tid, "name": sm.get("name"), "why": "in the target's trash"})
                    continue
                carried_in = sm.get("ownedByMe") is False
                # A file shared into this mailbox belongs to another source
                # user. Its source-side ancestor path is not readable or
                # meaningful to the copy, and the ACL is deliberately not
                # migrated on behalf of this user. Avoid repeated parent and
                # permission calls for that out-of-scope metadata.
                sp = "" if carried_in else self._drive_path(src, sid, sc)
                tp = self._drive_path(tgt, tid, tc)
                diffs = compare_drive_file(sm, tm, sp, tp)
                ev = {"kind": kind, "name": sm.get("name"), "path": sp, "opened": False}
                if kind == "file":
                    target_keys[(tp, tm.get("name"), tm.get("md5Checksum"))] = tid
                    reused = drive_content_cache_get(self.db, self.src_user, sid, tid, sm, tm)
                    if reused is not None:
                        diffs += reused["diffs"]
                        content = reused["diffs"]
                        ev.update(reused["ev"])
                        ev.update(reused=True, how=f"{reused['ev'].get('how', 'compared')} -- neither copy has "
                                                   f"changed since {reused['at']}, so that comparison is reused")
                        res["contentReused"] = res.get("contentReused", 0) + 1
                    else:
                        content = self._content(src, tgt, sid, tid, sm, tm, ev)
                        diffs += content
                        if not ev.get("error"):
                            drive_content_cache_put(self.db, self.src_user, sid, tid, sm, tm, content, ev)
                    from copy_resolution import remember_content
                    remember_content(self, sid, tid, sm, tm, ev, content)
                    if ev.get("error"):
                        res["errors"].append(f"file {sid}: {ev.pop('error')}")
                    if getattr(self.settings, "rewrite_drive_links", False):
                        try:
                            diffs += self._native_links(tid, sm)
                        except Exception as exc:      # noqa: BLE001 - unread is INCOMPLETE, never a pass
                            res["errors"].append(f"file {sid}: the links inside could not be read: {error_detail(exc)}")
                if carried_in:
                    # Carried in: owned by someone outside the source and shared with this
                    # user, copied for them (drive_engine._walk_shared_with_me). The copy is
                    # theirs, in their Drive; the owner's folder and sharing stay with the
                    # owner's original -- by design, not a difference (live, dharan.gov.np).
                    res["carriedIn"] = res.get("carriedIn", 0) + 1
                    diffs = [d for d in diffs if not d.startswith("folder path differs")]
                    sharing = []
                else:
                    sharing = self._sharing(src, tgt, sid, tid, res)
                    if getattr(self, "_not_owner", False):
                        diffs = [d for d in diffs if not d.startswith("folder path differs")]
                diffs += sharing
                self.evidence.append({"service": "drive", **ev})
                if diffs:
                    res["differences"].append({"item": sm.get("name"), "path": sp, "source": sid, "target": tid, "diffs": diffs})
                else:
                    res["identical"] += 1
            self._report_progress(operation, len(pairs), len(pairs))
        # Strays and duplicates on the target the ledger would never mention.
        try:
            extra = self._target_files(tgt)
            for f in extra:
                if f["id"] in seen_targets:
                    continue
                try:
                    receipt = self.db.conn.execute("SELECT state,detail FROM finding_repair_actions WHERE source_user=? "
                        "AND item_id=? AND service='drive-duplicate'", (self.src_user, f["id"])).fetchone()
                except Exception:
                    receipt = None
                if receipt and receipt["state"] == "QUARANTINED":
                    detail = json.loads(receipt["detail"])
                    current = self._drive_meta(tgt, f["id"])
                    canonical = self._drive_meta(tgt, detail["canonical"])
                    same = bool(current.get("md5Checksum") and current.get("md5Checksum") == canonical.get("md5Checksum"))
                    if detail.get("native"):
                        from finding_repairs import _native_duplicate_equal
                        same = self._x(lambda: _native_duplicate_equal(self.auth, self.tgt_user, f["id"], detail["canonical"], current["mimeType"]))
                    if (current.get("parents") == detail.get("originalParents")
                            and current.get("name") == detail.get("quarantinedName")
                            and current.get("ownedByMe") and not current.get("trashed") and same):
                        res.setdefault("quarantined", []).append({"target": f["id"], "name": f.get("name")})
                        res["notes"].append("Confirmed duplicate retained in target quarantine: " + f["id"])
                        continue
                    res["errors"].append("Quarantined duplicate changed or moved: " + f["id"])
                key = (self._drive_path(tgt, f["id"], tc), f.get("name"), f.get("md5Checksum"))
                if key in target_keys:
                    res["duplicates"].append({"target": f["id"], "name": f.get("name"), "path": key[0],
                                              "duplicateOf": target_keys[key]})
                else:
                    res["extras"].append({"target": f["id"], "name": f.get("name"), "path": key[0]})
        except Exception as exc:      # noqa: BLE001
            res["errors"].append(f"could not list the target's Drive: {error_detail(exc)}")
        if not any(self._pairs(kind) for kind in ("folder", "file", "shortcut")):
            # Zero mappings are not proof that the source Drive is empty. A
            # single read distinguishes a genuinely empty account from a
            # migration that never produced a ledger pairing, without
            # inventorying every file just to decide the empty case.
            try:
                source_inventory = self._x(lambda: src.files().list(
                    q="trashed = false", spaces="drive", pageSize=1,
                    includeItemsFromAllDrives=True, supportsAllDrives=True,
                    fields="files(id,name,mimeType)").execute(),
                    operation="Drive source empty check")
                source_files = source_inventory.get("files") or []
                if source_files:
                    first = source_files[0]
                    res["errors"].append(
                        "the source has Drive content but no source-to-target mapping; "
                        f"cannot verify it against a target copy (first item: {first.get('name') or first.get('id')})")
                else:
                    res["emptySource"] = True
                    res["notes"].append("the source Drive has no non-trashed files")
            except Exception as exc:      # noqa: BLE001 - unknown is not empty
                res["errors"].append(f"could not confirm whether the source Drive is empty: {error_detail(exc)}")
        self._note_sample(res, "folder", "file")
        sh = res.get("sharing")
        if sh and sh["notReproduced"]:
            res["notes"].append(f"sharing: {sh['matched']} grants matched; {sh['notReproduced']} were deliberately not "
                                "reproduced because the person has no account on the target")
        elif sh:
            res["notes"].append(f"sharing: all {sh['matched']} grants matched")
        if res.get("not_owner"):
            res["notes"].append(f"{res.pop('not_owner')} file(s) shared with this user by someone "
                                "else: their sharing and folder belong to the owner, not compared")
        res["notCopied"] = self._failed(("file", "folder", "drive"))
        from migration_checks import drive_completion
        coverage = self._blank()
        drive_completion(self.auth, self.db, self.settings, self.src_user, self.tgt_user,
                         self._retry, persist=False, finding_result=coverage)
        for category in ("notCopied", "missing"):
            known = {i.get("id") or i.get("source") for i in res[category]}
            res[category].extend(i for i in coverage[category] if (i.get("id") or i.get("source")) not in known)
        res["errors"].extend(coverage["errors"])
        return res

    def _check_moved(self, tgt, fid: str, name: str | None, res: dict) -> None:
        try:
            tm = self._drive_meta(tgt, fid)
        except Exception as exc:      # noqa: BLE001
            if _is_not_found(exc):
                res["missing"].append({"source": fid, "target": fid, "name": name,
                                       "why": f"moved, but not on the target ({str(exc)[:80]})"})
            else:
                res["errors"].append(f"moved item {fid}: could not read the TARGET: {error_detail(exc)}")
            return
        if tm.get("trashed"):
            res["missing"].append({"source": fid, "target": fid, "name": name,
                                   "why": "moved, and in the target's trash"})
        elif name and tm.get("name") != name:
            res["differences"].append({"item": name, "path": "", "source": fid, "target": fid,
                                       "diffs": [f"moved, now named {tm.get('name')!r}"]})
        else:
            res["identical"] += 1
        self.evidence.append({"service": "drive", "kind": "file", "name": name,
                              "path": "", "opened": False, "moved": True})

    def _perms(self, svc, fid) -> list[dict]:
        key = (id(svc), fid)
        cache = getattr(self, "_drive_permission_cache", {})
        if key in cache:
            return cache.pop(key)
        out, token, seen = [], None, set()
        while True:
            r = self._x(lambda t=token: svc.permissions().list(
                fileId=fid, supportsAllDrives=True, pageToken=t, pageSize=100,
                fields="nextPageToken,permissions(id,type,role,emailAddress,domain,deleted)").execute())
            out.extend(r.get("permissions", []))
            token = r.get("nextPageToken")
            if not token:
                return out
            if token in seen:
                raise RuntimeError(f"permissions for {fid} repeated a page token")
            seen.add(token)

    def _sharing(self, src, tgt, sid, tid, res) -> list[str]:
        """Compare who can see this item; returns the differences, and tallies the
        grants that were matched or deliberately not reproduced on `res`."""
        self._not_owner = False
        try:
            sp, tp = self._perms(src, sid), self._perms(tgt, tid)
        except Exception as exc:      # noqa: BLE001
            if "insufficientFilePermissions" in str(exc):
                # Shared WITH this user by someone else: its sharing and its folder are
                # the owner's, which this user cannot read -- nothing to compare.
                self._not_owner = True
                res["not_owner"] = res.get("not_owner", 0) + 1
                return []
            res["errors"].append(f"sharing of {sid}: could not be read: {error_detail(exc)}")
            return []
        if not hasattr(self, "_skipped_ids"):
            self._skipped_ids = {r["item_id"] for r in self.db.conn.execute(
                # OWED too: a share with a colleague not migrated yet is recorded and granted
                # once they arrive (repair.reapply_owed_grants) -- explained, not missing.
                "SELECT item_id FROM audit_log WHERE source_user=? AND item_type='acl' "
                "AND (status LIKE 'SKIPPED%' OR status LIKE 'OWED%')",
                (self.src_user,))}
        g = compare_grants(sp, tp, self._translate, self.settings.source_domain, self.settings.target_domain,
                           self._skipped_ids, sid)
        sh = res.setdefault("sharing", {"filesCompared": 0, "matched": 0, "notReproduced": 0})
        sh["filesCompared"] += 1
        sh["matched"] += g["matched"]
        sh["notReproduced"] += len(g["notReproduced"])
        out = [f"grant missing on the target: {m}" for m in g["missing"]]
        out += [f"extra grant on the target (nobody granted it): {e}" for e in g["extra"]]
        return out

    def _target_files(self, svc) -> list[dict]:
        out, token, seen_tokens, page = [], None, set(), 0
        while True:
            page += 1
            operation = "Drive target inventory"
            self._report_progress(operation, len(out), item=f"page {page}")
            r = self._x(lambda t=token: svc.files().list(
                q="trashed=false and 'me' in owners", spaces="drive", pageSize=1000, pageToken=t,
                supportsAllDrives=True, fields="nextPageToken, files(id,name,mimeType,md5Checksum)").execute(),
                operation=operation)
            out += [f for f in r.get("files", []) if f.get("mimeType") != FOLDER]
            next_token = r.get("nextPageToken")
            if not next_token:
                return out
            if next_token in seen_tokens:
                raise RuntimeError(f"Drive target inventory repeated a page token after page {page}; "
                                   f"stopped after {len(out)} files")
            seen_tokens.add(next_token)
            token = next_token

    def _content(self, src, tgt, sid, tid, sm, tm, ev) -> list[str]:
        """Open BOTH files and compare what is inside them."""
        mime = sm.get("mimeType") or ""
        try:
            if mime == "application/vnd.google-apps.spreadsheet":
                from native_repair import read_sheet, compare_sheets
                source_sheet = self._x(lambda: read_sheet(self.auth.api("source", "sheets", self.src_user), sid))
                target_sheet = self._x(lambda: read_sheet(self.auth.api("target", "sheets", self.tgt_user), tid))
                differences = compare_sheets(source_sheet, target_sheet, self.db.target_for_source_id)
                if differences and self.db.conn.execute(
                        "SELECT 1 FROM audit_log WHERE item_type='form_link' AND status='RELINKED' "
                        "AND error_message LIKE ?", (f"%-> sheet {tid}",)).fetchone():
                    # The operator's choice (2026-10-24): a linked Form over an identical Sheet.
                    differences = [d + " -- includes the empty responses tab Google adds when a copied "
                                       "Form is relinked" for d in differences]
                ev.update(opened=True, how="native Sheet entered values, formulas, cell styles and structures; calculated results excluded",nativeFeaturesChecked=True,nativeFeaturesMatched=not differences)
                return differences
            if mime in ("application/vnd.google-apps.document", "application/vnd.google-apps.presentation"):
                from native_fidelity import compare_native
                differences,scope=self._x(lambda: compare_native(self.auth,self.src_user,self.tgt_user,sid,tid,mime,self.db.target_for_source_id))
                ev.update(opened=True,how=scope,nativeFeaturesChecked=True,nativeFeaturesMatched=not differences)
                return differences
            if mime in NATIVE_EXPORT:
                a, b = self._export(src, sid, NATIVE_EXPORT[mime]), self._export(tgt, tid, NATIVE_EXPORT[mime])
                ev.update(opened=True, how=f"exported as {NATIVE_EXPORT[mime]}", bytes=len(a),
                          sha256=sha256(a)[:16])
                if a != b:
                    # The migration repoints a Doc's links at the copies on purpose, so the
                    # source put through the engine's own rewriter is what the target must be.
                    import link_rewrite
                    if link_rewrite.rewrite_bytes(a, copy_lookup(self.db, b))[0] == b:
                        ev["how"] += "; its Drive links repointed at the copies, as intended"
                        return []
                return [] if a == b else [f"content differs when exported ({len(a)} -> {len(b)} bytes)"]
            if mime.startswith("application/vnd.google-apps."):
                ev["how"] = "not opened: this Google type has no text export"
                return []
            size = int(sm.get("size") or 0)
            if size > MAX_COMPARE_BYTES:
                ev["how"] = f"not opened: {size:,} bytes is over the {MAX_COMPARE_BYTES:,}-byte comparison limit (size and MD5 only)"
                return []
            a, b = self._download(src, sid), self._download(tgt, tid)
            ev.update(opened=True, how="downloaded from both and compared", bytes=len(a), sha256=sha256(a)[:16])
            out = []
            if sm.get("md5Checksum") and hashlib.md5(a).hexdigest() != sm["md5Checksum"]:
                out.append("the SOURCE download does not match its own MD5 (read error, not a migration fault)")
            if a != b:
                out.append(f"content differs: sha256 {sha256(a)[:12]} -> {sha256(b)[:12]} ({len(a)} -> {len(b)} bytes)")
            return out
        except Exception as exc:      # noqa: BLE001
            ev["how"] = f"could not be opened: {error_detail(exc)}"
            ev["error"] = f"content could not be compared: {error_detail(exc)}"
            return []

    def _native_links(self, tid, sm) -> list[str]:
        """A Doc, Sheet or Slide's links live inside it, where no export comparison sees
        them: ask the same builder the rewrite uses what it would still change on the
        TARGET copy -- a link still naming a migrated source file. Reads only."""
        from drive_engine import _NATIVE_KIND, _native_project_limiter
        import link_rewrite
        k = _NATIVE_KIND.get(sm.get("mimeType") or "")
        if not k:
            return []
        n = self._x(lambda: link_rewrite.rewrite_native(
            k, self.auth.api("target", k, self.tgt_user), tid, self.db.target_for_source_id,
            pace=lambda op: _native_project_limiter(k, op).acquire(), apply=False))
        return [f"links inside still point at the source ({n} rewrite(s) due)"] if n else []

    # -- Gmail ------------------------------------------------------------
    def _label_names(self, svc) -> dict:
        r = self._x(lambda: svc.users().labels().list(userId="me").execute())
        return {l["id"]: l["name"] for l in r.get("labels", [])}

    def _raw(self, svc, mid) -> tuple[bytes, dict]:
        from verification_reads import gmail_raw
        return gmail_raw(svc, mid, self._x)

    def _raw_many(self, svc, message_ids: list[str], side: str, progress_offset: int = 0,
                  progress_total: int | None = None) -> tuple[dict[int, tuple[bytes, dict]], dict[int, Exception]]:
        """Read a bounded batch of raw messages, falling back for test/service adapters.

        Gmail has no collection-level raw export. The old loop made two synchronous HTTP
        round trips per message; a 39k-message mailbox therefore needed ~80k sequential
        requests. The discovery client can batch up to 100 reads into one HTTP exchange.
        Requests still count against Google quota, but latency and connection overhead drop
        sharply. Only 10 message bodies are held at a time, and individual errors remain
        attached to their source/target item.
        """
        values: dict[int, tuple[bytes, dict]] = {}
        errors: dict[int, Exception] = {}
        progress_total = progress_total if progress_total is not None else len(message_ids)
        if not hasattr(svc, "new_batch_http_request"):
            for index, message_id in enumerate(message_ids):
                operation = f"Gmail {side} raw message reads"
                self._report_progress(operation, progress_offset + index, progress_total,
                                      f"message {message_id}")
                try:
                    values[index] = self._raw(svc, message_id)
                except Exception as exc:  # noqa: BLE001 - retain per-message failure
                    errors[index] = exc
            return values, errors

        def decode_message(message: dict) -> tuple[bytes, dict]:
            raw = message.get("raw", "")
            raw = raw if isinstance(raw, str) else raw.decode()
            return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), message

        def retryable_part_error(exc: Exception) -> bool:
            status = getattr(getattr(exc, "resp", None), "status", None)
            text = str(exc).lower()
            return (status == 429 or (status is not None and status >= 500)
                    or any(reason in text for reason in
                           ("ratelimitexceeded", "userratelimitexceeded", "quotaexceeded")))

        batch_size = _gmail_batch_size()
        for offset in range(0, len(message_ids), batch_size):
            chunk = message_ids[offset:offset + batch_size]
            batch_number = offset // batch_size + 1
            batch_total = (len(message_ids) + batch_size - 1) // batch_size
            operation = f"Gmail {side} raw message reads"
            item = (f"batch {batch_number}/{batch_total}; messages "
                    f"{progress_offset + offset + 1}-{progress_offset + offset + len(chunk)} "
                    f"of {progress_total}")

            def receive(request_id, response, exception):
                index = int(request_id)
                if exception is not None:
                    errors[index] = exception
                elif response is None:
                    errors[index] = RuntimeError("Gmail batch response was empty")
                else:
                    try:
                        values[index] = decode_message(response)
                    except Exception as exc:  # noqa: BLE001 - malformed one-item response
                        errors[index] = exc

            batch = svc.new_batch_http_request(callback=receive)
            for index, message_id in enumerate(chunk):
                request = svc.users().messages().get(userId="me", id=message_id, format="raw")
                batch.add(request, request_id=str(offset + index))
            self._report_progress(operation, progress_offset + offset, progress_total, item)

            def execute_batch():
                # A multipart batch can return HTTP 200 while individual reads contain
                # quota/5xx errors. Surface one transient item error to the normal retry
                # policy; successful siblings are safe to repeat because these are reads.
                for index in range(offset, offset + len(chunk)):
                    values.pop(index, None)
                    errors.pop(index, None)
                batch.execute()
                transient = next((errors[i] for i in range(offset, offset + len(chunk))
                                  if i in errors and retryable_part_error(errors[i])), None)
                if transient is not None:
                    raise transient

            try:
                self._x(execute_batch, operation=operation, request_count=len(chunk))
            except Exception as exc:  # noqa: BLE001 - a rejected batch must finish as item errors
                for index in range(offset, offset + len(chunk)):
                    if index not in values:
                        errors.setdefault(index, exc)
            for index in range(offset, offset + len(chunk)):
                if index not in values and index not in errors:
                    errors[index] = RuntimeError("Gmail batch omitted this message response")
                if index in errors:
                    from verification_reads import status_of
                    if status_of(errors[index]) == 400:
                        try:
                            values[index] = self._raw(svc, message_ids[index])
                            errors.pop(index, None)
                        except Exception as exc:
                            errors[index] = exc
        return values, errors

    def _gmail_raw_chunks(self, pairs):
        """Overlap independent tenant reads without sharing discovery HTTP clients.

        AuthManager caches transports per thread. Resolve each service INSIDE its
        worker; never send the main thread's httplib2 transport into the pool.
        At most VERIFY_GMAIL_WINDOW pairs of batches (default 4) are live or held,
        yielded in order; ten bodies per side per batch.
        """
        from concurrent.futures import ThreadPoolExecutor
        size = _gmail_batch_size()
        parallel = os.getenv("VERIFY_GMAIL_PARALLEL_SIDES", "1").lower() not in ("0", "false", "no")
        total = len(pairs)
        def read(side, ids, offset):
            _stop_check()
            svc = (self.auth.source_gmail(self.src_user) if side == "source"
                   else self.auth.target_gmail(self.tgt_user))
            return self._raw_many(svc, ids, side, offset, total)
        # A bounded read-ahead window of batch pairs, yielded in order. One pair at a
        # time left each mailbox ~10x under Gmail's per-user quota and took 8.7-11.5 h
        # for info@'s 39,756 messages (dharan, 2026-10-23). The window bounds memory too:
        # at most `window` pairs of `size` bodies are held.
        window = _gmail_read_window() if parallel else 1
        import collections
        ahead = collections.deque()

        def submit(offset):
            chunk = pairs[offset:offset + size]
            src_ids, tgt_ids = [p[0] for p in chunk], [p[1] for p in chunk]
            if parallel:
                return offset, chunk, pool.submit(read, "source", src_ids, offset), pool.submit(read, "target", tgt_ids, offset)
            return offset, chunk, None, (read("source", src_ids, offset), read("target", tgt_ids, offset))
        # One executor per mailbox, not per item. It is drained even on stop/error.
        with ThreadPoolExecutor(max_workers=2 * window, thread_name_prefix="verify-mail-read") as pool:
            offsets = iter(range(0, total, size))
            for offset in offsets:
                _stop_check()
                ahead.append(submit(offset))
                if len(ahead) < window:
                    continue
                yield self._settle(ahead.popleft())
            while ahead:
                _stop_check()
                yield self._settle(ahead.popleft())

    @staticmethod
    def _settle(entry):
        offset, chunk, source, target = entry
        if source is None:
            source_result, target_result = target
        else:
            source_result, target_result = source.result(), target.result()
        _stop_check()
        return offset, chunk, source_result, target_result

    def gmail(self) -> dict:
        res = self._blank()
        src, tgt = self.auth.source_gmail(self.src_user), self.auth.target_gmail(self.tgt_user)
        try:
            sl, tl = self._label_names(src), self._label_names(tgt)
        except Exception as exc:      # noqa: BLE001
            res["errors"].append(f"could not read labels: {error_detail(exc)}")
            sl = tl = {}
        rewritten = {r["item_id"] for r in self.db.conn.execute(
            "SELECT item_id FROM audit_log WHERE source_user=? AND item_type='link_rewrite' AND status='SUCCESS'",
            (self.src_user,))}
        from link_rewrite import rewrite_raw
        mapped_ids, mapped_msgids = {tid for _, tid, _ in self._pairs("message")}, set()
        said: dict[str, int] = {}          # verdict -> how many messages, said once below
        message_pairs = self._pairs("message", sample=True)
        from verification_cache import MailCache
        cache = MailCache(self, src, tgt, (sl, tl))
        pending = []
        for sid, tid, name in message_pairs:
            cached = cache.get(sid, tid)
            if cached is None:
                pending.append((sid, tid, name))
                continue
            res["checked"] += 1
            res["identical"] += 1
            mapped_msgids.add(cached.get("messageId", ""))
            self.evidence.append({"service":"gmail", "name":cached.get("messageId") or sid,
                "how":"validated receipt reused after two-tenant history reconciliation",
                "opened":False, "sha256":cached["sourceSha256"][:16]})
        total_messages = len(pending)
        self._report_progress("Gmail compare mapped messages", 0, total_messages)
        for offset, chunk, source_result, target_result in self._gmail_raw_chunks(pending):
            source_raw, source_errors = source_result
            target_raw, target_errors = target_result
            for index, (sid, tid, _) in enumerate(chunk):
                res["checked"] += 1
                if index in source_errors:
                    exc = source_errors[index]
                    if _gone_from_source(exc, res):
                        continue
                    res["errors"].append(f"message {sid}: could not read the SOURCE: {error_detail(exc)}")
                    continue
                if index in target_errors:
                    exc = target_errors[index]
                    if _is_not_found(exc):
                        res["missing"].append({"source": sid, "target": tid,
                                               "why": f"not on the target ({str(exc)[:80]})"})
                    else:
                        res["errors"].append(f"message {tid}: could not read the TARGET: {error_detail(exc)}")
                    continue
                sraw, sm = source_raw[index]
                traw, tm = target_raw[index]
                source_headers = BytesHeaderParser().parsebytes(sraw)
                msgid = (source_headers.get("Message-ID") or "").strip()
                mapped_msgids.add(msgid)
                expected = None
                if sid in rewritten:
                    b64 = base64.urlsafe_b64encode(sraw).decode().rstrip("=")
                    # decoded parts too: a base64 or quoted-printable body hides the ids
                    new_b64, _n = rewrite_raw(b64, copy_lookup(self.db, traw + normalise_message(traw)[1]))
                    expected = base64.urlsafe_b64decode(new_b64 + "=" * (-len(new_b64) % 4))
                verdict, notes = compare_message(sraw, traw, expected)
                diffs = []
                if verdict == "different":
                    diffs += [f"message differs: {n}" for n in notes] or ["message differs"]
                diffs += compare_labels({sl.get(i, i) for i in sm.get("labelIds") or []},
                                        {tl.get(i, i) for i in tm.get("labelIds") or []})
                if ("UNREAD" in (sm.get("labelIds") or [])) != ("UNREAD" in (tm.get("labelIds") or [])):
                    diffs.append("unread state differs")
                try:
                    hdr_ms = int(parsedate_to_datetime(source_headers.get("Date")).timestamp() * 1000)
                except Exception:      # noqa: BLE001 - no usable Date header
                    hdr_ms = None
                ti, si = int(tm.get("internalDate") or 0), int(sm.get("internalDate") or 0)
                if ti and si and ti != si and (hdr_ms is None or abs(ti - hdr_ms) > 1000):
                    diffs.append(f"date differs: source {si}, target {ti}, Date header {hdr_ms}")
                self.evidence.append({"service": "gmail", "name": msgid or sid, "how": verdict, "bytes": len(sraw),
                                      "sha256": sha256(sraw)[:16], "opened": True})
                if verdict in ("identical after rewrite", "equivalent"):
                    said[verdict] = said.get(verdict, 0) + 1
                if diffs:
                    res["differences"].append({"item": msgid or sid, "source": sid, "target": tid, "diffs": diffs})
                else:
                    res["identical"] += 1
                    cache.put(sid, tid, {"messageId":msgid, "sourceSha256":sha256(sraw),
                        "targetSha256":sha256(traw), "comparison":verdict})
            completed = min(offset + len(chunk), total_messages)
            self._report_progress("Gmail compare mapped messages", completed, total_messages)
        for verdict, n in said.items():
            res["notes"].append(f"{n} message(s) {verdict}: " + (
                "Drive links repointed at the copies on the target, as intended" if verdict == "identical after rewrite"
                else "the same message, apart from headers Google adds when it inserts one"))
        try:
            mapped_ids |= self._draft_message_ids(tgt)
        except Exception as exc:      # noqa: BLE001 - only the stray check suffers; the drafts are still compared
            res["errors"].append(f"could not list the target's drafts: {error_detail(exc)}")
        self._drafts(src, tgt, res, mapped_ids)
        self._note_sample(res, "message", "draft")
        # Duplicates and extras by Message-ID.
        try:
            welcome = trashed = 0
            scanned = 0
            for m in self._list_all(tgt):
                scanned += 1
                self._report_progress("Gmail target inventory", scanned,
                                      item=f"message {m.get('id', '?')}")
                if m["id"] in mapped_ids:
                    continue
                hdr = self._x(lambda i=m["id"]: tgt.users().messages().get(
                    userId="me", id=i, format="metadata", metadataHeaders=["Message-ID", "From"]).execute())
                head = {h["name"].lower(): h["value"].strip() for h in hdr.get("payload", {}).get("headers", [])}
                mid = head.get("message-id", "")
                if "TRASH" in (hdr.get("labelIds") or []):
                    trashed += 1        # deleted as far as the user can tell: not a copy that exists
                    continue
                if mid not in mapped_msgids and "@google.com" in head.get("from", "").lower():
                    welcome += 1        # what Google puts in every new mailbox; nobody migrated it
                    continue
                (res["duplicates"] if mid in mapped_msgids else res["extras"]).append({"target": m["id"], "messageId": mid})
            if trashed:
                res["notes"].append(f"{trashed} message(s) in the target's trash -- what an earlier run left before a "
                                    f"reset or a delete; they are not counted as copies or strays")
            if welcome:
                res["notes"].append(f"{welcome} message(s) from Google itself (new-mailbox welcome mail) sit on the "
                                    f"target; they were never on the source and are not counted as strays")
        except Exception as exc:      # noqa: BLE001
            res["errors"].append(f"could not list the target's mailbox: {error_detail(exc)}")
        res["notCopied"] = self._failed(("message", "gmail"))
        self._source_mail_coverage(src, res)
        reference_gaps = self.db.conn.execute(
            "SELECT item_id,error_message FROM audit_log WHERE source_user=? "
            "AND item_type='source_link_reference' AND status='NEEDS_ATTENTION'",
            (self.src_user,)).fetchall()
        if reference_gaps:
            from verification_coverage import check
            res.setdefault("checks", []).append(check(
                "gmail.sourceReferences", "Access to original unavailable Drive references", "NOT_VERIFIED",
                checked=len(reference_gaps), note="Original unavailable URLs were preserved; "
                "restoring their access requires the original file owner. Message equality does not establish link access."))
            res["notes"].append(f"{len(reference_gaps)} source message(s) retain original unavailable Drive references; "
                                "their content is preserved, but referenced-file access remains unverified")
        try:
            cache.finish()
        except Exception as exc:
            res["notes"].append("Receipt cache was not saved; next check will read content again: "+str(exc)[:160])
        res["cache"] = {**cache.stats(),"freshComparisons":len(pending)}
        return res

    def _source_mail_coverage(self, src, res):
        """A matched mapping is not proof that every source message was mapped."""
        known = {sid for sid, _, _ in self._pairs("message")}
        known_drafts = {sid for sid, _, _ in self._pairs("draft")}
        existing = {(i.get("type"), i.get("id")) for i in res["notCopied"]}
        try:
            draft_messages, token, seen = set(), None, set()
            while True:
                response = self._x(lambda t=token: src.users().drafts().list(
                    userId="me", maxResults=100, pageToken=t, includeSpamTrash=True).execute(),
                    operation="Gmail source draft coverage")
                for draft in response.get("drafts", []):
                    mid = (draft.get("message") or {}).get("id")
                    if mid:
                        draft_messages.add(mid)
                    did = draft.get("id")
                    if did and did not in known_drafts and ("draft", did) not in existing:
                        res["notCopied"].append({"type": "draft", "id": did,
                            "status": "UNMAPPED_SOURCE", "error": "Live source draft has no migration mapping"})
                        existing.add(("draft", did))
                token = response.get("nextPageToken")
                if not token:
                    break
                if token in seen:
                    raise RuntimeError("source draft inventory repeated a page token")
                seen.add(token)
            deliberately_skipped = {r[0] for r in self.db.conn.execute(
                "SELECT item_id FROM audit_log WHERE source_user=? AND item_type='message' AND status='SKIPPED_CHAT'",
                (self.src_user,))}
            scanned = 0
            for message in self._list_all(src, side="source"):
                scanned += 1
                sid = message.get("id")
                if sid and sid not in known and sid not in draft_messages and sid not in deliberately_skipped and ("message", sid) not in existing:
                    res["notCopied"].append({"type": "message", "id": sid,
                        "status": "UNMAPPED_SOURCE", "error": "Live source message has no migration mapping; destination coverage is not established"})
                    existing.add(("message", sid))
            res["notes"].append(f"Source coverage: inventoried {scanned} live message IDs, including spam and trash; checked unmapped messages and drafts")
            if deliberately_skipped:
                res["notes"].append(f"{len(deliberately_skipped)} legacy chat message(s) are outside Gmail migration coverage")
        except Exception as exc:
            res["errors"].append(f"could not establish full source mailbox coverage: {str(exc)[:160]}")

    def _draft_message_ids(self, svc) -> set[str]:
        """The message behind every draft in the mailbox: messages.list returns a draft's message
        among the rest, and it is not a stray."""
        out, token, seen_tokens, page = set(), None, set(), 0
        while True:
            page += 1
            operation = "Gmail draft inventory"
            self._report_progress(operation, len(out), item=f"page {page}")
            r = self._x(lambda t=token: svc.users().drafts().list(
                userId="me", maxResults=500, pageToken=t).execute(), operation=operation)
            out |= {(d.get("message") or {}).get("id") for d in r.get("drafts", [])}
            next_token = r.get("nextPageToken")
            if not next_token:
                return out - {None}
            if next_token in seen_tokens:
                raise RuntimeError(f"Gmail draft inventory repeated a page token after page {page}")
            seen_tokens.add(next_token)
            token = next_token

    def _draft_raw(self, svc, did) -> tuple[bytes, str]:
        d = self._x(lambda: svc.users().drafts().get(userId="me", id=did, format="raw").execute())
        m = d.get("message") or {}
        raw = m.get("raw", "")
        raw = raw if isinstance(raw, str) else raw.decode()
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), m.get("id", "")

    def _drafts(self, src, tgt, res, mapped_ids) -> None:
        """Drafts are copied by a pass of their own, so they are opened and compared like messages.
        Their underlying message ids are recorded so a draft is never mistaken for a stray."""
        pairs = self._pairs("draft", sample=True)
        self._report_progress("Gmail compare drafts", 0, len(pairs))
        for pair_index, (sid, tid, _) in enumerate(pairs):
            self._report_progress("Gmail compare drafts", pair_index, len(pairs), f"draft {sid}")
            res["checked"] += 1
            try:
                sraw, _m = self._draft_raw(src, sid)
            except Exception as exc:      # noqa: BLE001
                if _gone_from_source(exc, res):
                    continue
                res["errors"].append(f"draft {sid}: could not read the SOURCE: {error_detail(exc)}")
                continue
            try:
                traw, tmsg = self._draft_raw(tgt, tid)
            except Exception as exc:      # noqa: BLE001
                if _is_not_found(exc):
                    res["missing"].append({"source": sid, "target": tid,
                                           "why": f"draft not on the target ({str(exc)[:80]})"})
                else:
                    res["errors"].append(f"draft {tid}: could not read the TARGET: {error_detail(exc)}")
                continue
            mapped_ids.add(tmsg)
            verdict, notes = compare_draft(sraw, traw, self._translate)
            self.evidence.append({"service": "gmail", "name": f"draft {sid}", "how": verdict, "bytes": len(sraw),
                                  "sha256": sha256(sraw)[:16], "opened": True})
            if verdict == "different":
                res["differences"].append({"item": f"draft {sid}", "source": sid, "target": tid,
                                           "diffs": [f"draft differs: {n}" for n in notes]})
            else:
                res["identical"] += 1
        self._report_progress("Gmail compare drafts", len(pairs), len(pairs))
        if self._pairs("draft"):
            res["notes"].append("drafts: Gmail rebuilds a draft's Message-Id, Date and From when it is created, so drafts "
                                "are compared on recipients, subject and body, not byte for byte")

    def _list_all(self, svc, side="target") -> Iterator[dict]:
        """Stream target message IDs so a large mailbox does not accumulate in memory.

        The previous implementation built a list of every target message, called the API
        outside the retry/progress wrapper, and trusted every nextPageToken forever. A large
        tenant could therefore appear frozen for hours or loop forever on a repeated token.
        """
        token, seen_tokens, page, total = None, set(), 0, 0
        while True:
            page += 1
            operation = f"Gmail {side} inventory"
            self._report_progress(operation, total, item=f"page {page}")
            r = self._x(lambda t=token: svc.users().messages().list(
                userId="me", maxResults=500, pageToken=t, includeSpamTrash=True).execute(),
                operation=operation)
            messages = r.get("messages", [])
            total += len(messages)
            for message in messages:
                yield message
            next_token = r.get("nextPageToken")
            if not next_token:
                self._report_progress(operation, total, item=f"complete after {page} page(s)")
                return
            if next_token in seen_tokens:
                raise RuntimeError(f"Gmail {side} inventory repeated a page token after page {page}; "
                                   f"stopped after {total} message IDs")
            seen_tokens.add(next_token)
            token = next_token

    # -- Calendar ---------------------------------------------------------
    def calendar(self) -> dict:
        res = self._blank()
        src, tgt = self.auth.source_calendar(self.src_user), self.auth.target_calendar(self.tgt_user)
        cal_map = {s: t for s, t, _ in self._pairs("calendar")}
        ev_notes: list[str] = []
        pairs = self._pairs("event", sample=True)
        self._report_progress("Calendar compare events", 0, len(pairs))
        for pair_index, (key, tid, _) in enumerate(pairs):
            self._report_progress("Calendar compare events", pair_index, len(pairs), f"event {tid}")
            res["checked"] += 1
            src_cal, _, eid = key.partition("::")
            tgt_cal = cal_map.get(src_cal) or "primary"
            try:
                sev = self._x(lambda: src.events().get(calendarId=src_cal or "primary", eventId=eid).execute())
            except Exception as exc:      # noqa: BLE001
                if _gone_from_source(exc, res):
                    continue
                res["errors"].append(f"event {eid}: could not read the SOURCE: {error_detail(exc)}")
                continue
            try:
                tev = self._x(lambda: tgt.events().get(calendarId=tgt_cal, eventId=tid).execute())
            except Exception as exc:      # noqa: BLE001
                if _is_not_found(exc):
                    res["missing"].append({"source": eid, "target": tid, "name": sev.get("summary"),
                                           "why": f"not on the target ({str(exc)[:80]})"})
                else:
                    res["errors"].append(f"event {tid}: could not read the TARGET: {error_detail(exc)}")
                continue
            diffs = compare_event(sev, tev, self._translate, self.db.target_for_source_id, ev_notes)
            self.evidence.append({"service": "calendar", "name": sev.get("summary"), "opened": True, "how": "fields compared"})
            if diffs:
                res["differences"].append({"item": sev.get("summary"), "source": eid, "target": tid, "diffs": diffs})
            else:
                res["identical"] += 1
        self._report_progress("Calendar compare events", len(pairs), len(pairs))
        for text in sorted(set(ev_notes)):
            res["notes"].append(f"{ev_notes.count(text)} time(s), {text}")
        self._note_sample(res, "event")
        res["notCopied"] = self._failed(("event", "calendar"))
        # Nothing recorded and nothing failed: read the source's own calendars. None holding
        # an event is 1:1 with nothing to copy, not unchecked (live, dharan.gov.np pilot:
        # three users with no events of their own read "incomplete 0/0").
        if res["checked"] == 0 and not res["errors"] and not res["notCopied"]:
            try:
                import inventory
                n = inventory.scan_calendar(src)
                # At least the primary must have been read: no calendar at all is not
                # evidence of an empty one.
                if n.get("calendars") and not n.get("events"):
                    res["emptySource"] = True
                    res["notes"].append(f"the source's {n.get('calendars', 0)} own calendar(s) hold no events"
                                        + (f"; {n['followed']} followed calendar(s) are re-followed, not copied"
                                           if n.get("followed") else ""))
            except Exception as exc:      # noqa: BLE001 - unread stays INCOMPLETE
                res["errors"].append(f"the source's calendars could not be read: {error_detail(exc)}")
        return res

    def directory(self):
        import verification_coverage
        res = verification_coverage.directory(self)
        res["notCopied"] += self._failed(("directory",))
        return res

    def chat(self):
        import verification_coverage
        res = verification_coverage.chat(self)
        res["notCopied"] += self._failed(("chat",))      # the whole service failed for this user
        return res

    # -- Contacts ---------------------------------------------------------
    def contacts(self) -> dict:
        res = self._blank()
        src, tgt = self.auth.source_people(self.src_user), self.auth.target_people(self.tgt_user)
        pairs = self._pairs("contact", sample=True)
        all_pairs = self._pairs("contact")
        mapped_source_ids = {sid for sid, _, _ in all_pairs}
        skipped_source_ids = {r["item_id"] for r in self.db.conn.execute(
            "SELECT item_id FROM audit_log WHERE source_user=? AND item_type='contact' "
            "AND status='SKIPPED_EMPTY'", (self.src_user,))}
        source_ids = self._contact_source_ids(src)
        if source_ids is None:
            res["errors"].append(getattr(self, "_contact_inventory_error",
                                         "could not inventory source contacts"))
        else:
            unmapped = sorted(source_ids - mapped_source_ids - skipped_source_ids)
            if unmapped:
                res["unmappedSource"] = len(unmapped)
                res["errors"].append(
                    f"{len(unmapped)} source contact(s) have no ledger mapping; "
                    "the target cannot be matched safely"
                    + (f" (examples: {', '.join(unmapped[:5])})" if unmapped else ""))
            elif not (source_ids - skipped_source_ids):
                res["emptySource"] = True
                if skipped_source_ids:
                    res["notes"].append(
                        f"the source has no mappable contacts; {len(source_ids & skipped_source_ids)} empty contact stub(s) were skipped")
                else:
                    res["notes"].append("the source has no remaining contacts to compare")
        self._report_progress("Contacts read source", 0, len(pairs))
        source_people, source_errors = self._people_batch(src, [sid for sid, _, _ in pairs], "source")
        self._report_progress("Contacts read target", 0, len(pairs))
        target_people, target_errors = self._people_batch(tgt, [tid for _, tid, _ in pairs], "target")
        for sid, tid, _ in pairs:
            res["checked"] += 1
            if sid in source_errors:
                code, message = source_errors[sid]
                if code == 5:  # google.rpc.Code.NOT_FOUND: deleted on source after migration
                    res["checked"] -= 1
                    res["goneFromSource"] = res.get("goneFromSource", 0) + 1
                    continue
                res["errors"].append(f"contact {sid}: could not read the SOURCE: {message[:100]}")
                continue
            if tid in target_errors:
                code, message = target_errors[tid]
                if code == 5:
                    res["missing"].append({"source": sid, "target": tid,
                                           "why": f"not on the target ({message[:80]})"})
                else:
                    res["errors"].append(f"contact {tid}: could not read the TARGET: {message[:100]}")
                continue
            sp, tp = source_people.get(sid), target_people.get(tid)
            if sp is None or tp is None:
                res["errors"].append(f"contact {sid}: People API batch response omitted a requested person")
                continue
            name = ((sp.get("names") or [{}])[0]).get("displayName") or sid
            # As written: contacts_engine copies a contact's addresses exactly, so a
            # colleague's old address stays old (live, dharan.gov.np pilot: the check
            # translated them and called 20 exact copies different).
            diffs = compare_contact(sp, tp, lambda v: v)
            self.evidence.append({"service": "contacts", "name": name, "opened": True, "how": "fields compared"})
            if diffs:
                res["differences"].append({"item": name, "source": sid, "target": tid, "diffs": diffs})
            else:
                res["identical"] += 1
        self._report_progress("Contacts compare people", len(pairs), len(pairs))
        self._note_sample(res, "contact")
        res["notCopied"] = self._failed(("contact", "contacts"))
        return res

    def _contact_source_ids(self, svc) -> set[str] | None:
        """Inventory source contact resource names to confirm empty sources and
        expose source contacts that have no ledger mapping.

        People API permits 1,000 people per page. This is an ID-only read; the
        expensive field comparison remains batched and limited by the verifier
        sample setting.
        """
        ids: set[str] = set()
        token = None
        page = 0
        try:
            while True:
                page += 1
                self._report_progress("Contacts source inventory", len(ids), item=f"page {page}")
                response = self._x(lambda t=token: svc.people().connections().list(
                    resourceName="people/me", pageSize=1000, pageToken=t,
                    personFields="metadata").execute(), operation="Contacts source inventory")
                ids.update(p["resourceName"] for p in response.get("connections", [])
                           if p.get("resourceName"))
                token = response.get("nextPageToken")
                if not token:
                    self._report_progress("Contacts source inventory", len(ids), len(ids))
                    return ids
        except Exception as exc:      # noqa: BLE001 - API failure must not look like an empty source
            self._report_progress("Contacts source inventory", len(ids), item="inventory failed")
            # Let the caller distinguish the failed inventory from a genuinely empty set.
            res_error = f"could not inventory source contacts: {error_detail(exc)}"
            self._contact_inventory_error = res_error
            return None

    def _people_batch(self, svc, resource_names: list[str], side: str = "tenant") -> tuple[dict[str, dict], dict[str, tuple[int | None, str]]]:
        """Fetch contact details in bounded People API batches.

        The former one-get-per-contact loop made two round trips for every
        migrated contact. A batch of up to 200 preserves per-person failures
        while reducing that to one round trip per 200 contacts per tenant.
        """
        people: dict[str, dict] = {}
        errors: dict[str, tuple[int | None, str]] = {}
        batch_total = (len(resource_names) + PEOPLE_BATCH_SIZE - 1) // PEOPLE_BATCH_SIZE
        for start in range(0, len(resource_names), PEOPLE_BATCH_SIZE):
            batch = resource_names[start:start + PEOPLE_BATCH_SIZE]
            batch_number = start // PEOPLE_BATCH_SIZE + 1
            operation = f"Contacts read {side}"
            self._report_progress(operation, start, len(resource_names), f"batch {batch_number}/{batch_total}")
            response = self._x(lambda names=batch: svc.people().getBatchGet(
                resourceNames=names, personFields=PERSON_FIELDS).execute(), operation=operation)
            entries = response.get("responses", [])
            for index, entry in enumerate(entries):
                person = entry.get("person") or {}
                fallback = batch[index] if index < len(batch) else None
                name = (entry.get("requestedResourceName")
                        or person.get("resourceName") or fallback)
                if name not in batch:
                    continue
                if person:
                    people[name] = person
                    continue
                status = entry.get("status") or {}
                try:
                    code = int(status.get("code")) if status.get("code") is not None else None
                except (TypeError, ValueError):
                    code = None
                errors[name] = (code, str(status.get("message") or "no person returned"))
            for name in batch:
                if name not in people and name not in errors:
                    errors[name] = (None, "People API batch response omitted this person")
        self._report_progress(f"Contacts read {side}", len(resource_names), len(resource_names))
        return people, errors

    # -- Tasks ------------------------------------------------------------
    def _tasks_of(self, svc, list_id) -> dict:
        out, token, seen_tokens, page = {}, None, set(), 0
        while True:
            page += 1
            operation = "Tasks pagination"
            self._report_progress(operation, len(out), item=f"list {list_id}, page {page}")
            r = self._x(lambda t=token: svc.tasks().list(
                tasklist=list_id, maxResults=100, pageToken=t, showCompleted=True, showHidden=True).execute(),
                operation=operation)
            for t in r.get("items", []):
                out[t["id"]] = t
            next_token = r.get("nextPageToken")
            if not next_token:
                return out
            if next_token in seen_tokens:
                raise RuntimeError(f"Tasks list {list_id} repeated a page token after page {page}")
            seen_tokens.add(next_token)
            token = next_token

    def tasks(self) -> dict:
        res = self._blank()
        src, tgt = self.auth.source_tasks(self.src_user), self.auth.target_tasks(self.tgt_user)
        task_map = {s: t for s, t, _ in self._pairs("task")}
        keep = {s for s, _, _ in self._pairs("task", sample=True)}
        lists = seen = 0
        task_lists = self._pairs("task_list")
        self._report_progress("Tasks read lists", 0, len(task_lists))
        for list_index, (slist, tlist, title) in enumerate(task_lists):
            self._report_progress("Tasks read lists", list_index, len(task_lists), title)
            try:
                sts, tts = self._tasks_of(src, slist), self._tasks_of(tgt, tlist)
            except Exception as exc:      # noqa: BLE001
                res["errors"].append(f"task list {title!r}: {error_detail(exc)}")
                continue
            lists += 1
            seen += len(sts)
            for sid, st in sts.items():
                tid = task_map.get(sid)
                if not tid or sid not in keep:
                    continue            # not in the sample
                res["checked"] += 1
                tt = tts.get(tid)
                if tt is None:
                    res["missing"].append({"source": sid, "target": tid, "name": st.get("title"), "why": "not on the target"})
                    continue
                diffs = compare_task(st, tt)
                sp, tp = st.get("parent"), tt.get("parent")
                if bool(sp) != bool(tp) or (sp and task_map.get(sp) != tp):
                    diffs.append("parent task differs")
                self.evidence.append({"service": "tasks", "name": st.get("title"), "opened": True, "how": "fields compared"})
                if diffs:
                    res["differences"].append({"item": st.get("title"), "source": sid, "target": tid, "diffs": diffs})
                else:
                    res["identical"] += 1
        self._report_progress("Tasks read lists", len(task_lists), len(task_lists))
        self._report_progress("Tasks compare tasks", res["checked"], len(keep))
        self._note_sample(res, "task")
        res["notCopied"] = self._failed(("task", "task_list", "tasks"))
        # Every list read and none held a task: nothing to copy is 1:1, not unchecked.
        if lists and not seen and not res["errors"]:
            res["emptySource"] = True
            res["notes"].append(f"the source's {lists} task list(s) hold no tasks")
        return res


# ---------------------------------------------------------------------------
# A run
# ---------------------------------------------------------------------------
def verdict_of(users: dict, services: tuple[str, ...]) -> tuple[str, list[str]]:
    """IDENTICAL only when every paired item matched and nothing was left over.
    A check that could not be made is INCOMPLETE, never a pass."""
    reasons, incomplete, bad = [], False, False
    for user, per in users.items():
        for svc in services:
            r = per.get(svc)
            if r is None:
                incomplete = True
                reasons.append(f"{user}: {svc} was not checked")
                continue
            if r.get("coverageComplete") is False:
                incomplete = True
                reasons.append(f"{user}: {svc} has required scopes that are not fully checked")
            if r["errors"]:
                incomplete = True
                reasons.append(f"{user}: {svc} had {len(r['errors'])} check(s) that could not be made")
            elif r["checked"] == 0 and not r.get("emptySource"):
                incomplete = True
                reasons.append(f"{user}: {svc} checked no items and the source was not confirmed empty")
            for label in ("differences", "missing", "duplicates", "notCopied"):
                if r[label]:
                    bad = True
                    reasons.append(f"{user}: {svc} has {len(r[label])} {label}")
    if bad:
        return "DIFFERENCES", reasons
    if incomplete:
        return "INCOMPLETE", reasons
    return "IDENTICAL", reasons


def service_complete(result: dict) -> bool:
    """A service is complete only when every requested read returned a usable result.

    Known differences are complete checks. An error, or zero checks without an explicitly
    verified empty source, is incomplete and must not be presented as a pass.
    """
    return (not result.get("errors") and result.get("coverageComplete") is not False
            and (result.get("checked", 0) > 0 or result.get("emptySource", False)))


def run(auth, db, settings, users: list[str] | None = None, services=ALL_SERVICES, retry=lambda f: f,
        progress=print, limit: int | None = None, on_user=None, on_service=None) -> dict:
    ident = {r["source_email"]: r["target_email"] for r in db.all_identities() if r["entity_type"] == "user"}
    chosen = [u for u in (users or list(ident)) if u in ident]
    services = tuple(s for s in ALL_SERVICES if s in services)
    # Chat is skipped only when it was never part of this migration: not selected,
    # nothing mapped and nothing attempted. A chat that failed outright has nothing
    # mapped either, and must still read as not copied.
    if "chat" in services and not getattr(settings, "migrate_chat", False) and not any(
            db.get_target_id(user, sid, "chat_space") for user in chosen
            for sid, in db.conn.execute("SELECT source_id FROM id_mapping WHERE source_user=? AND type='chat_space' LIMIT 1", (user,))
    ) and not any(db.conn.execute("SELECT 1 FROM audit_log WHERE source_user=? AND item_type IN "
                                  "('chat','chat_space','chat_message') LIMIT 1", (user,)).fetchone()
                  for user in chosen):
        services = tuple(s for s in services if s != "chat")
    report = {"generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "accountId": getattr(settings, "account_id", None),
              "sourceDomain": settings.source_domain, "targetDomain": settings.target_domain,
              "sampleLimit": getattr(settings, "sample_limit", None), "verifyLimit": limit, "services": list(services),
              "users": {}, "evidence": [], "notes": []}
    # Users side by side (VERIFY_WORKERS): a check is mostly waiting on Google, and
    # one user at a time made "verify everything" on 300 users a matter of days.
    # Each user is handed to on_user the moment it is done, so the page fills in
    # as it goes and a run cut short keeps what it found.
    import threading
    from concurrent.futures import ThreadPoolExecutor

    lock, done = threading.Lock(), {}
    started_users: set[str] = set()
    activity: dict[str, dict] = {}
    stop_heartbeats = threading.Event()
    progress_lock = threading.Lock()

    def emit(message: str) -> None:
        # Multiple user workers share one transcript. Keep every progress record a single
        # intact line so the UI parser and the operator can trust the current state.
        with progress_lock:
            progress(message)

    def heartbeat() -> None:
        while not stop_heartbeats.wait(60):
            now = time.monotonic()
            with lock:
                reportable = []
                for user, state in activity.items():
                    if now - state["last_heartbeat"] >= 60:
                        state["last_heartbeat"] = now
                        reportable.append((user, state.copy()))
            for user, state in reportable:
                service_elapsed = int(now - state["service_started"])
                progress_text = (f"{state['items_completed']:,}"
                                 + (f"/{state['items_total']:,}" if state["items_total"] is not None else ""))
                last_api_age = (int(now - state["last_api_at"])
                                if state["last_api_at"] is not None else None)
                last_api = (f"{state['last_api_result']} {last_api_age}s ago"
                            if state["last_api_result"] and last_api_age is not None
                            else (state["last_api_result"] or "none yet"))
                no_item_progress = int(now - state["last_progress_at"])
                detail = (f"verify: heartbeat {user} | service={state['service']} "
                          f"| operation={state['operation']} | serviceElapsed={service_elapsed}s "
                          f"| items={progress_text} | apiAttempts={state['api_attempts']} "
                          f"httpCalls={state['http_calls']} retries={state['retry_rounds']} "
                          f"inFlight={state['in_flight']} "
                          f"| noItemProgress={no_item_progress}s | lastApi={last_api}")
                if state["in_flight"] and state["last_api_started"] is not None:
                    detail += f" apiOpenFor={int(now - state['last_api_started'])}s"
                if state["detail"]:
                    detail += f" | currentItem={state['detail']}"
                if state["last_error"]:
                    error_age = int(now - state["last_error_at"])
                    detail += f" | lastError={state['last_error']} ({error_age}s ago)"
                emit(detail)

    heartbeat_thread = threading.Thread(target=heartbeat,
                                        name="verify-heartbeat", daemon=True)
    heartbeat_thread.start()

    # Verification spends most of its time waiting for Google API responses, so
    # user-level concurrency is the useful speed knob. In auto mode, size the pool
    # to the work actually waiting, up to a conservative ceiling. A smaller user
    # selection never starts idle workers; a large account does not exceed the cap.
    max_workers = 4
    try:
        configured_cap = int(os.getenv("VERIFY_MAX_WORKERS", str(max_workers)) or max_workers)
        max_workers = min(max_workers, max(1, configured_cap))
    except (TypeError, ValueError):
        pass
    worker_setting = (os.getenv("VERIFY_WORKERS", "auto") or "auto").strip().lower()
    if worker_setting == "auto":
        workers = min(max_workers, max(1, len(chosen)))
        worker_mode = "auto"
    else:
        try:
            requested_workers = int(worker_setting)
        except (TypeError, ValueError):
            requested_workers = max_workers
        workers = min(max_workers, max(1, min(len(chosen) or 1, requested_workers)))
        worker_mode = "configured"
    emit(f"VERIFY START: {len(chosen)} user(s), {len(services)} service(s) each, "
         f"{workers} worker(s), mode={worker_mode}, maxWorkers={max_workers}, "
         f"pendingUsers={len(chosen)}, checkLimit={'all' if limit is None else limit}")

    def emit_scheduler() -> None:
        with lock:
            active_count = len(started_users) - len(done)
            pending_count = max(0, len(chosen) - len(started_users))
            completed_count = len(done)
        emit(f"verify: scheduler activeUsers={active_count} pendingUsers={pending_count} "
             f"completedUsers={completed_count} workers={workers} maxWorkers={max_workers}")

    def activity_event(user: str, event: str, **details) -> None:
        should_log = False
        line = ""
        with lock:
            state = activity.get(user)
            if not state:
                return
            now = time.monotonic()
            operation = details.get("operation")
            if operation:
                state["operation"] = operation
            if details.get("item") is not None:
                state["detail"] = str(details["item"])[:180]
            if event == "start":
                state["api_attempts"] += max(1, int(details.get("request_count", 1) or 1))
                state["http_calls"] += 1
                if int(details.get("attempt", 1) or 1) > 1:
                    state["retry_rounds"] += 1
                state["in_flight"] += 1
                state["last_api_started"] = now
            elif event == "finish":
                state["in_flight"] = max(0, state["in_flight"] - 1)
                state["last_api_at"] = now
                if details.get("error"):
                    state["last_api_result"] = "error"
                    state["last_error"] = details["error"]
                    state["last_error_at"] = now
                else:
                    state["last_api_result"] = "ok"
            elif event == "progress":
                completed = details.get("completed")
                total = details.get("total")
                if completed is not None:
                    state["items_completed"] = max(0, int(completed))
                state["items_total"] = max(0, int(total)) if total is not None else None
                state["last_progress_at"] = now
                bucket = (int(completed) // VERIFY_PROGRESS_EVERY
                          if completed is not None else None)
                if bucket is not None and (
                        bucket > state["last_logged_bucket"]
                        or (total is not None and completed >= total)):
                    state["last_logged_bucket"] = bucket
                    should_log = True
                    line = (f"verify: progress {user} | service={state['service']} "
                            f"| operation={state['operation']} | items={state['items_completed']:,}"
                            + (f"/{state['items_total']:,}" if state["items_total"] is not None else ""))
        if should_log:
            emit(line)

    def one(u: str) -> None:
        _stop_check()
        with lock:
            started_users.add(u)
        emit_scheduler()
        emit(f"verify: {u}")
        v = Verifier(auth, db, settings, u, ident[u], retry=retry, limit=limit,
                     on_api_attempt=lambda event, **details: activity_event(u, event, **details),
                     on_progress=lambda **details: activity_event(u, "progress", **details))
        per = {}
        for service_index, svc in enumerate(services, 1):
            _stop_check()
            now = time.monotonic()
            with lock:
                activity[u] = {"service": svc, "started": now,
                               "service_started": now, "last_heartbeat": now,
                               "operation": f"{svc} initialising", "api_attempts": 0,
                               "http_calls": 0, "retry_rounds": 0, "in_flight": 0, "last_api_started": None,
                               "last_api_at": None, "last_api_result": "", "last_error": "",
                               "last_error_at": now,
                               "items_completed": 0, "items_total": None,
                               "last_progress_at": now, "last_logged_bucket": -1}
            emit(f"verify: {u} | service {service_index} of {len(services)} {svc} started")
            v._report_progress(f"{svc} initialising", 0, None)
            service_started = time.monotonic()
            try:
                per[svc] = getattr(v, svc)()
                import verification_coverage as coverage
                if svc in ("drive", "calendar"):
                    coverage.add(v, per[svc], svc + ".coverage", "Migration scope inventory",
                                 lambda: (getattr(coverage, svc)(v, per[svc]) or coverage.check(
                                     svc + ".inventoryRead", "Scope inventory read", "MATCHED")))
                coverage.finish(per[svc], svc)
                if per[svc].get("goneFromSource"):
                    per[svc]["notes"].append(f"{per[svc]['goneFromSource']} item(s) deleted from the source "
                                             f"since they were copied: nothing left to compare")
            except Exception as exc:      # noqa: BLE001 - one service failing must not lose the rest
                r = Verifier._blank()
                r["errors"].append(f"{svc} could not be verified: {type(exc).__name__}: {str(exc)[:160]}")
                per[svc] = r
            elapsed = time.monotonic() - service_started
            with lock:
                state = activity.pop(u, {})
            if on_service:
                try:
                    on_service(u, svc, per[svc])
                except Exception as exc:  # noqa: BLE001 - a ledger save failure is not a pass
                    per[svc]["errors"].append(f"could not save this service's verification: "
                                               f"{type(exc).__name__}: {str(exc)[:140]}")
            service_verdict, _ = verdict_of({u: {svc: per[svc]}}, (svc,))
            completion = "COMPLETE" if service_complete(per[svc]) else "INCOMPLETE"
            emit(f"verify: {u} | {svc} finished in {elapsed:.1f}s; "
                 f"completion={completion}; verdict={service_verdict}; "
                 f"{per[svc]['checked']} checked, {per[svc]['identical']} identical, "
                 f"{len(per[svc]['differences'])} difference(s), {len(per[svc]['missing'])} missing, "
                 f"{len(per[svc]['errors'])} incomplete/error(s), "
                 f"{state.get('api_attempts', 0)} API request attempt(s) over "
                 f"{state.get('http_calls', 0)} HTTP call(s), "
                 f"{state.get('retry_rounds', 0)} retry round(s)")
        evidence = [{"user": u, **e} for e in v.evidence]
        if on_user:
            followup = on_user(u, per)
            if followup is not None:
                per = followup["users"][u]
                rechecked = followup.get("recheckedServices", services)
                evidence = ([e for e in evidence if e.get("service") not in rechecked]
                            + followup.get("evidence", []))
        with lock:
            done[u] = (per, evidence)
            finished = len(done)
        emit_scheduler()
        user_verdict, _ = verdict_of({u: per}, services)
        user_completion = ("INCOMPLETE" if len(per) != len(services)
                           or any(not service_complete(r) for r in per.values()) else "COMPLETE")
        emit(f"verify: [{finished}/{len(chosen)}] {u} checked; completion={user_completion}; "
             f"verdict={user_verdict}")

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, chosen))
    finally:
        stop_heartbeats.set()
        heartbeat_thread.join(timeout=2)
    for u in chosen:                  # the chosen order, not the finishing order
        if u in done:
            report["users"][u] = done[u][0]
            report["evidence"] += done[u][1]
    service_count = sum(len(per) for per in report["users"].values())
    incomplete_users = sum(
        1 for per in report["users"].values()
        if len(per) != len(services) or any(not service_complete(r) for r in per.values())
    )
    report["coverage"] = {
        "usersExpected": len(chosen), "usersCompleted": len(report["users"]),
        "servicesExpected": len(chosen) * len(services), "servicesCompleted": service_count,
        "incompleteUsers": incomplete_users,
        "completion": "COMPLETE" if chosen and len(report["users"]) == len(chosen) and not incomplete_users else "INCOMPLETE",
    }
    report["verdict"], report["reasons"] = verdict_of(report["users"], services)
    for u, per in report["users"].items():
        for svc, r in per.items():
            if r["checked"] == 0 and not r["errors"] and not r.get("emptySource"):
                r["notes"].append("nothing was copied for this service, so there was nothing to check")
    tot = {"checked": 0, "identical": 0, "differences": 0, "missing": 0, "duplicates": 0, "extras": 0,
           "notCopied": 0, "errors": 0, "filesOpened": sum(1 for e in report["evidence"] if e.get("opened") and e["service"] == "drive"),
           "bytesCompared": sum(e.get("bytes", 0) for e in report["evidence"] if e.get("opened"))}
    for per in report["users"].values():
        for r in per.values():
            tot["checked"] += r["checked"]; tot["identical"] += r["identical"]
            for k in ("differences", "missing", "duplicates", "extras", "notCopied", "errors"):
                tot[k] += len(r[k])
    report["totals"] = tot
    if not chosen or not services:
        report["verdict"] = "INCOMPLETE"
        report["reasons"].append("no mapped users or services were selected for verification")
    # verdict_of already rejects unconfirmed zero checks. A source explicitly
    # confirmed empty must not be downgraded by a second zero-total rule.
    report["notes"].append("Nothing was written to either tenant.")
    report["notes"].append("Items are paired using the migration ledger; every property of each pair is read fresh "
                           "from both tenants. Items the sample did not copy are not expected on the target.")
    if report["sampleLimit"]:
        report["notes"].append(f"This was a sample: at most {report['sampleLimit']} items of each service per user.")
    if limit:
        report["notes"].append(f"Verified a sample: up to {limit} of each kind of item per user, evenly spread. "
                               "A difference outside the sample would not be seen.")
    return report


def to_markdown(report: dict) -> str:
    t = report["totals"]
    L = [f"# One-to-one verification: {report['verdict']}", "",
         f"- {report['sourceDomain']} -> {report['targetDomain']}, account {report['accountId']}",
         f"- generated {report['generatedAt']}; services {', '.join(report['services'])}; "
         f"users {len(report['users'])}" + (f"; sample of {report['sampleLimit']} per service" if report.get("sampleLimit") else "")
         + (f"; checked up to {report['verifyLimit']} of each kind per user" if report.get("verifyLimit") else ""),
         f"- completion {report.get('coverage', {}).get('completion', 'UNKNOWN')}; "
         f"users {report.get('coverage', {}).get('usersCompleted', len(report['users']))}/"
         f"{report.get('coverage', {}).get('usersExpected', len(report['users']))}; "
         f"services {report.get('coverage', {}).get('servicesCompleted', 'unknown')}/"
         f"{report.get('coverage', {}).get('servicesExpected', 'unknown')}",
         f"- **{t['identical']:,} of {t['checked']:,}** paired items identical; {t['filesOpened']:,} Drive files opened "
         f"and compared; {t['bytesCompared']:,} bytes read from each side",
         f"- differences {t['differences']}, missing on target {t['missing']}, duplicates {t['duplicates']}, "
         f"strays on target {t['extras']}, unresolved copy or access findings {t['notCopied']}, checks that could not be made {t['errors']}", ""]
    if report["reasons"]:
        L += ["## Why not IDENTICAL", ""] + [f"- {r}" for r in report["reasons"]] + [""]
    for user, per in report["users"].items():
        L += [f"## {user}", "", "| service | completion | checked | identical | differences | missing | duplicates | strays | copy or access findings | could not check |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for svc, r in per.items():
            completion = "COMPLETE" if service_complete(r) else "INCOMPLETE"
            L.append(f"| {svc} | {completion} | {r['checked']} | {r['identical']} | {len(r['differences'])} | {len(r['missing'])} | "
                     f"{len(r['duplicates'])} | {len(r['extras'])} | {len(r['notCopied'])} | {len(r['errors'])} |")
        L.append("")
        for svc, r in per.items():
            for label in ("differences", "missing", "duplicates", "notCopied", "errors"):
                for item in r[label][:25]:
                    L.append(f"- **{svc} / {label}**: {json.dumps(item, default=str)[:400]}")
            for n in r["notes"][:10]:
                L.append(f"- {svc} note: {n}")
            if r.get("copyResolutions"):
                L += ["", f"### {svc}: retained target copies", "",
                      "Copy presence does not resolve source authorization or original sharing. Content comparison scope is shown separately.", ""]
                for copy in r["copyResolutions"]:
                    link = f"[Open copy]({copy['targetUrl']})" if copy.get("targetUrl") else "No readable copy confirmed"
                    L.append(f"- **{copy['label']}** — {copy['source']}: {link}. "
                             f"{copy['contentScope']} Checked {copy['checkedAt']}.")
        L.append("")
    opened = [e for e in report["evidence"] if e.get("opened")][:60]
    if opened:
        L += ["## What was opened", "", "| user | service | item | how | bytes | sha256 |", "|---|---|---|---|---|---|"]
        L += [f"| {e['user'].split('@')[0]} | {e['service']} | {str(e.get('name'))[:40]} | {e.get('how', '')[:50]} | "
              f"{e.get('bytes', '')} | {e.get('sha256', '')} |" for e in opened]
        L.append("")
    L += ["## Notes", ""] + [f"- {n}" for n in report["notes"]]
    return "\n".join(L) + "\n"


def quick_dir(account_id) -> str:
    return os.path.join(HERE, "logs", "quick", "_none" if account_id is None else str(account_id))


def save(report: dict, base: str | None = None) -> tuple[str, str]:
    d = base or quick_dir(report.get("accountId"))
    os.makedirs(d, exist_ok=True)
    stamp = report["generatedAt"].replace(":", "").replace("-", "")
    jp, mp = os.path.join(d, f"quick-{stamp}.json"), os.path.join(d, f"quick-{stamp}.md")
    with open(jp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, default=str)
    with open(mp, "w", encoding="utf-8") as fh:
        fh.write(to_markdown(report))
    for name, src in (("latest.json", jp), ("latest.md", mp)):
        tmp = os.path.join(d, name + ".tmp")
        with open(src, "rb") as a, open(tmp, "wb") as b:
            b.write(a.read())
        os.replace(tmp, os.path.join(d, name))
    return jp, mp


def latest(account_id, base: str | None = None) -> dict | None:
    try:
        with open(os.path.join(base or quick_dir(account_id), "latest.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def verify_dir(account_id) -> str:
    return os.path.join(HERE, "logs", "verify", "_none" if account_id is None else str(account_id))


def save_user_results(db, report: dict) -> None:
    """One row per (user, service) in the account's own database, so the verification page and the
    user's row in a migration can say what was last found without reading a report file."""
    for user, per in report["users"].items():
        import verification_repairs
        verification_repairs.enqueue_findings(db, user, per)
        for svc, r in per.items():
            verdict, _ = verdict_of({user: {svc: r}}, (svc,))
            if verdict == "IDENTICAL" and r["checked"] == 0 and not r.get("emptySource"):
                verdict = "INCOMPLETE"          # nothing checked is not a pass (an empty source is)
            db.save_user_verification(user, svc, verdict, r["checked"], r["identical"],
                                      (r.get("sampled") or {}).get("of"), {
                "completion": "COMPLETE" if service_complete(r) else "INCOMPLETE",
                "counts": {k: len(r[k]) for k in ("differences", "missing", "duplicates", "extras", "notCopied", "errors")},
                **{k: r[k][:25] for k in ("differences", "missing", "duplicates", "notCopied", "errors")},
                "extras": r["extras"][:10], "notes": r["notes"][:10], "sampled": r.get("sampled"),
                **{key: r.get(key) for key in ("scopeVersion", "coverageComplete", "checks", "sharing", "sourceInventory", "emptySource", "cache", "copyResolutions")}})


def verify_user(auth, db, settings, source_user: str, services, limit: int | None, retry=lambda f: f) -> str | None:
    """Verify what one user's migration just finished, and record it. Returns the verdict, or None
    when it could not be run -- never raises: it runs on the heels of a migration worker and a
    failed check must not become a failed migration."""
    try:
        report = run(auth, db, settings, [source_user], tuple(services), retry, lambda *_: None, limit,
                     on_service=lambda user, service, result: save_user_results(
                         db, {"users": {user: {service: result}}}))
        save_user_results(db, report)
        log.info("[%s] verified %s: %s (%d of %d identical)", source_user, ",".join(services), report["verdict"],
                 report["totals"]["identical"], report["totals"]["checked"])
        return report["verdict"]
    except Exception:      # noqa: BLE001
        log.exception("[%s] verification could not be run", source_user)
        return None


def run_and_save(auth, db, settings, users=None, services=ALL_SERVICES, retry=lambda f: f, progress=print,
                 limit: int | None = None, base: str | None = None,
                 repair_after_user: bool = False) -> dict:
    # Persist each finished service while a large user is still in progress. If the process
    # is stopped or a later service cannot be checked, completed services remain current in
    # the One-to-one page instead of disappearing with that user's unfinished worker.
    callback = None
    if repair_after_user:
        import verification_repairs
        verification_repairs.enable(db)
        callback = lambda user, per: verification_repairs.finished_user(
            auth, db, settings, user, per, limit, retry, progress)
    report = run(auth, db, settings, users, services, retry, progress, limit,
                 on_user=callback,
                 on_service=lambda user, service, result: save_user_results(
                     db, {"users": {user: {service: result}}}))
    if repair_after_user:
        report["notes"] = [n for n in report["notes"] if n != "Nothing was written to either tenant."]
        report["notes"].append("Verification reads both tenants. Supported repairs write only to the target; "
                               "each attempt and recheck is recorded in the per-user repair history.")
    jp, mp = save(report, base)
    coverage = report["coverage"]
    totals = report["totals"]
    progress(f"verification: completion={coverage['completion']} verdict={report['verdict']} -- "
             f"users {coverage['usersCompleted']}/{coverage['usersExpected']}; "
             f"services {coverage['servicesCompleted']}/{coverage['servicesExpected']}; "
             f"{totals['identical']} of {totals['checked']} identical, "
             f"{totals['differences']} differences, {totals['missing']} missing, "
             f"{totals['errors']} check error(s); report saved to {os.path.relpath(mp, HERE)}")
    return report


def main(argv=None) -> int:
    import argparse

    from auth import AuthManager
    from config import Settings
    from db import MigrationDB
    from resilience import retry_on_google_error
    ap = argparse.ArgumentParser(description="Verify a migration one to one; optionally repair supported target differences per user.")
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--user", action="append", help="limit to these source users")
    ap.add_argument("--services", default=",".join(ALL_SERVICES))
    ap.add_argument("--limit", type=int, help="check only this many of each kind of item per user, evenly spread")
    ap.add_argument("--repair-after-user", action="store_true",
                    help="repair supported findings and recheck each user immediately after verification")
    a = ap.parse_args(argv)
    services = tuple(s.strip() for s in a.services.split(","))
    settings = Settings(account_id=a.account_id)
    # The scopes a credential carries follow these flags (config.source_scopes), and main.py switches
    # them on for the services a run names. Without this, reading contacts or tasks is a 403.
    settings.migrate_contacts = settings.migrate_contacts or "contacts" in services
    settings.migrate_tasks = settings.migrate_tasks or "tasks" in services
    db = MigrationDB(settings.db_path)
    # flush: a job's output is a file, and a buffered "verify: <user>" line made a
    # working run look frozen for half an hour.
    import main as migration_main
    migration_main._install_signal_handlers()
    try:
        run_and_save(AuthManager(settings), db, settings, a.user, services,
                     retry=retry_on_google_error(max_retries=settings.max_retries),
                     progress=lambda m: print(m, flush=True),
                     limit=a.limit, base=verify_dir(a.account_id),
                     repair_after_user=a.repair_after_user)
    except (VerificationCancelled, KeyboardInterrupt):
        if a.repair_after_user:
            import verification_repairs
            verification_repairs.pause(db)
        print("verification stopped; completed service results kept; automatic backfill paused until the next launch", flush=True)
        return 130
    finally:
        db.close()
    # 0 whenever the check RAN. The verdict is in the report, not the exit code: a
    # non-zero exit reads as a crash to everything that watches jobs (run_watch opens an
    # incident for it), and "the migration has differences" is a finding, not a crash.
    return 0


if __name__ == "__main__":
    sys.exit(main())
