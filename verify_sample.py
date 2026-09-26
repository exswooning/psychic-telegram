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

Nothing is written to either tenant. A check that could not be made says so and
makes the verdict INCOMPLETE; it is never counted as a pass. That is the same rule
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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
FOLDER = "application/vnd.google-apps.folder"
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
log = logging.getLogger(__name__)

ALL_SERVICES = ("drive", "gmail", "calendar", "contacts", "tasks")


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
    if sh == th and sb == tb:
        added = sorted({k for k in email.message_from_bytes(tgt_raw).keys()} - {k for k in email.message_from_bytes(want).keys()})
        return "equivalent", [f"same message; headers added on the target: {', '.join(added) or 'none named'}"]
    notes = []
    for k in sorted(set(sh) | set(th)):
        if sh.get(k) != th.get(k):
            notes.append(f"header {k}: {sh.get(k)} -> {th.get(k)}")
    if sb != tb:
        notes.append(f"body differs ({len(sb)} -> {len(tb)} bytes)")
    return "different", notes


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


def compare_labels(src_names: set[str], tgt_names: set[str]) -> list[str]:
    out = []
    if src_names - tgt_names:
        out.append(f"labels missing on the target: {sorted(src_names - tgt_names)}")
    if tgt_names - src_names:
        out.append(f"labels added on the target: {sorted(tgt_names - src_names)}")
    return out


def _norm_when(t: dict | None) -> str:
    if not t:
        return ""
    return (t.get("dateTime") or t.get("date") or "")


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
    b = sorted({(x.get("email") or "").lower() for x in tgt.get("attendees") or []})
    # The target calendar's own owner is added when it is not the primary; ignore
    # only addresses the source never had AND that are the target's own account.
    if a != b:
        out.append(f"attendees: {a} -> {b}")
    return out


def _vals(person: dict, key: str, sub: str) -> list[str]:
    return sorted({str((v.get(sub) or "")).strip() for v in person.get(key) or [] if v.get(sub)})


def compare_contact(src: dict, tgt: dict, translate) -> list[str]:
    out = []
    def digits(values): return sorted({re.sub(r"\D", "", v) for v in values})
    for label, a, b in (
        ("given name", _vals(src, "names", "givenName"), _vals(tgt, "names", "givenName")),
        ("family name", _vals(src, "names", "familyName"), _vals(tgt, "names", "familyName")),
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
    return out


def compare_task(src: dict, tgt: dict) -> list[str]:
    out = []
    for f in ("title", "notes", "status"):
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


class Verifier:
    def __init__(self, auth, db, settings, source_user: str, target_user: str, retry=lambda f: f,
                 limit: int | None = None):
        self.auth, self.db, self.settings = auth, db, settings
        self.src_user, self.tgt_user = source_user, target_user
        self._retry = retry
        self.evidence: list[dict] = []
        # None checks every item the ledger pairs (a sample migration, a small user); a number
        # checks that many of each kind, evenly spread -- see verify_user.
        self.limit = limit
        self._picked: dict[str, tuple[int, int]] = {}

    # -- plumbing ---------------------------------------------------------
    def _x(self, fn):
        """Run one API call under the retry policy."""
        return self._retry(fn)()

    def _translate(self, addr: str) -> str:
        return (self.db.resolve_identity(addr) or addr).lower() if addr else addr

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
    def _drive_meta(self, svc, fid):
        return self._x(lambda: svc.files().get(
            fileId=fid, supportsAllDrives=True,
            fields="id,name,mimeType,parents,size,md5Checksum,modifiedTime,trashed").execute())

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
        return self._x(lambda: svc.files().get_media(fileId=fid, supportsAllDrives=True).execute())

    def _export(self, svc, fid, mime) -> bytes:
        return self._x(lambda: svc.files().export_media(fileId=fid, mimeType=mime).execute())

    def drive(self) -> dict:
        res = self._blank()
        src, tgt = self.auth.source_drive(self.src_user), self.auth.target_drive(self.tgt_user)
        sc, tc = {}, {}
        # Every target the ledger knows -- not only the ones opened below -- so a sampled
        # run does not mistake the rest of the migration for strays.
        seen_targets = {tid for kind in ("folder", "file") for _, tid, _ in self._pairs(kind)}
        target_keys = {}
        for kind in ("folder", "file"):
            for sid, tid, _ in self._pairs(kind, sample=True):
                res["checked"] += 1
                try:
                    sm = self._drive_meta(src, sid)
                except Exception as exc:      # noqa: BLE001
                    res["errors"].append(f"{kind} {sid}: could not read the SOURCE: {str(exc)[:120]}")
                    continue
                try:
                    tm = self._drive_meta(tgt, tid)
                except Exception as exc:      # noqa: BLE001
                    res["missing"].append({"source": sid, "target": tid, "name": sm.get("name"),
                                           "why": f"not on the target ({str(exc)[:80]})"})
                    continue
                if tm.get("trashed"):
                    res["missing"].append({"source": sid, "target": tid, "name": sm.get("name"), "why": "in the target's trash"})
                    continue
                sp, tp = self._drive_path(src, sid, sc), self._drive_path(tgt, tid, tc)
                diffs = compare_drive_file(sm, tm, sp, tp)
                ev = {"kind": kind, "name": sm.get("name"), "path": sp, "opened": False}
                if kind == "file":
                    target_keys[(tp, tm.get("name"), tm.get("md5Checksum"))] = tid
                    diffs += self._content(src, tgt, sid, tid, sm, tm, ev)
                sharing = self._sharing(src, tgt, sid, tid, res)
                diffs += sharing
                self.evidence.append({"service": "drive", **ev})
                if diffs:
                    res["differences"].append({"item": sm.get("name"), "path": sp, "source": sid, "target": tid, "diffs": diffs})
                else:
                    res["identical"] += 1
        # Strays and duplicates on the target the ledger would never mention.
        try:
            extra = self._target_files(tgt)
            for f in extra:
                if f["id"] in seen_targets:
                    continue
                key = (self._drive_path(tgt, f["id"], tc), f.get("name"), f.get("md5Checksum"))
                if key in target_keys:
                    res["duplicates"].append({"target": f["id"], "name": f.get("name"), "path": key[0],
                                              "duplicateOf": target_keys[key]})
                else:
                    res["extras"].append({"target": f["id"], "name": f.get("name"), "path": key[0]})
        except Exception as exc:      # noqa: BLE001
            res["errors"].append(f"could not list the target's Drive: {str(exc)[:120]}")
        self._note_sample(res, "folder", "file")
        sh = res.get("sharing")
        if sh and sh["notReproduced"]:
            res["notes"].append(f"sharing: {sh['matched']} grants matched; {sh['notReproduced']} were deliberately not "
                                "reproduced because the person has no account on the target")
        elif sh:
            res["notes"].append(f"sharing: all {sh['matched']} grants matched")
        res["notCopied"] = self._failed(("file", "folder", "drive"))
        return res

    def _perms(self, svc, fid) -> list[dict]:
        r = self._x(lambda: svc.permissions().list(
            fileId=fid, supportsAllDrives=True,
            fields="permissions(id,type,role,emailAddress,domain,deleted)").execute())
        return r.get("permissions", [])

    def _sharing(self, src, tgt, sid, tid, res) -> list[str]:
        """Compare who can see this item; returns the differences, and tallies the
        grants that were matched or deliberately not reproduced on `res`."""
        try:
            sp, tp = self._perms(src, sid), self._perms(tgt, tid)
        except Exception as exc:      # noqa: BLE001
            res["errors"].append(f"sharing of {sid}: could not be read: {str(exc)[:100]}")
            return []
        if not hasattr(self, "_skipped_ids"):
            self._skipped_ids = {r["item_id"] for r in self.db.conn.execute(
                "SELECT item_id FROM audit_log WHERE source_user=? AND item_type='acl' AND status LIKE 'SKIPPED%'",
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
        out, token = [], None
        while True:
            r = self._x(lambda t=token: svc.files().list(
                q="trashed=false and 'me' in owners", spaces="drive", pageSize=1000, pageToken=t,
                supportsAllDrives=True, fields="nextPageToken, files(id,name,mimeType,md5Checksum)").execute())
            out += [f for f in r.get("files", []) if f.get("mimeType") != FOLDER]
            token = r.get("nextPageToken")
            if not token:
                return out

    def _content(self, src, tgt, sid, tid, sm, tm, ev) -> list[str]:
        """Open BOTH files and compare what is inside them."""
        mime = sm.get("mimeType") or ""
        try:
            if mime in NATIVE_EXPORT:
                a, b = self._export(src, sid, NATIVE_EXPORT[mime]), self._export(tgt, tid, NATIVE_EXPORT[mime])
                ev.update(opened=True, how=f"exported as {NATIVE_EXPORT[mime]}", bytes=len(a),
                          sha256=sha256(a)[:16])
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
            ev["how"] = f"could not be opened: {str(exc)[:100]}"
            return [f"content could not be compared: {str(exc)[:100]}"]

    # -- Gmail ------------------------------------------------------------
    def _label_names(self, svc) -> dict:
        r = self._x(lambda: svc.users().labels().list(userId="me").execute())
        return {l["id"]: l["name"] for l in r.get("labels", [])}

    def _raw(self, svc, mid) -> tuple[bytes, dict]:
        m = self._x(lambda: svc.users().messages().get(userId="me", id=mid, format="raw").execute())
        raw = m.get("raw", "")
        raw = raw if isinstance(raw, str) else raw.decode()
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), m

    def gmail(self) -> dict:
        res = self._blank()
        src, tgt = self.auth.source_gmail(self.src_user), self.auth.target_gmail(self.tgt_user)
        try:
            sl, tl = self._label_names(src), self._label_names(tgt)
        except Exception as exc:      # noqa: BLE001
            res["errors"].append(f"could not read labels: {str(exc)[:100]}")
            sl = tl = {}
        rewritten = {r["item_id"] for r in self.db.conn.execute(
            "SELECT item_id FROM audit_log WHERE source_user=? AND item_type='link_rewrite' AND status='SUCCESS'",
            (self.src_user,))}
        from link_rewrite import rewrite_raw
        mapped_ids, mapped_msgids = {tid for _, tid, _ in self._pairs("message")}, set()
        for sid, tid, _ in self._pairs("message", sample=True):
            res["checked"] += 1
            try:
                sraw, sm = self._raw(src, sid)
            except Exception as exc:      # noqa: BLE001
                res["errors"].append(f"message {sid}: could not read the SOURCE: {str(exc)[:100]}")
                continue
            try:
                traw, tm = self._raw(tgt, tid)
            except Exception as exc:      # noqa: BLE001
                res["missing"].append({"source": sid, "target": tid, "why": f"not on the target ({str(exc)[:80]})"})
                continue
            msgid = (email.message_from_bytes(sraw).get("Message-ID") or "").strip()
            mapped_msgids.add(msgid)
            expected = None
            if sid in rewritten:
                b64 = base64.urlsafe_b64encode(sraw).decode().rstrip("=")
                new_b64, _n = rewrite_raw(b64, self.db.target_for_source_id)
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
                hdr_ms = int(parsedate_to_datetime(email.message_from_bytes(sraw).get("Date")).timestamp() * 1000)
            except Exception:      # noqa: BLE001 - no usable Date header
                hdr_ms = None
            ti, si = int(tm.get("internalDate") or 0), int(sm.get("internalDate") or 0)
            if ti and si and ti != si and (hdr_ms is None or abs(ti - hdr_ms) > 1000):
                diffs.append(f"date differs: source {si}, target {ti}, Date header {hdr_ms}")
            self.evidence.append({"service": "gmail", "name": msgid or sid, "how": verdict, "bytes": len(sraw),
                                  "sha256": sha256(sraw)[:16], "opened": True})
            if verdict in ("identical after rewrite", "equivalent"):
                res["notes"].append(f"{msgid or sid}: {verdict} -- {'; '.join(notes)}")
            if diffs:
                res["differences"].append({"item": msgid or sid, "source": sid, "target": tid, "diffs": diffs})
            else:
                res["identical"] += 1
        try:
            mapped_ids |= self._draft_message_ids(tgt)
        except Exception as exc:      # noqa: BLE001 - only the stray check suffers; the drafts are still compared
            res["errors"].append(f"could not list the target's drafts: {str(exc)[:100]}")
        self._drafts(src, tgt, res, mapped_ids)
        self._note_sample(res, "message", "draft")
        # Duplicates and extras by Message-ID.
        try:
            welcome = 0
            for m in self._list_all(tgt):
                if m["id"] in mapped_ids:
                    continue
                hdr = self._x(lambda i=m["id"]: tgt.users().messages().get(
                    userId="me", id=i, format="metadata", metadataHeaders=["Message-ID", "From"]).execute())
                head = {h["name"].lower(): h["value"].strip() for h in hdr.get("payload", {}).get("headers", [])}
                mid = head.get("message-id", "")
                if mid not in mapped_msgids and "@google.com" in head.get("from", "").lower():
                    welcome += 1        # what Google puts in every new mailbox; nobody migrated it
                    continue
                (res["duplicates"] if mid in mapped_msgids else res["extras"]).append({"target": m["id"], "messageId": mid})
            if welcome:
                res["notes"].append(f"{welcome} message(s) from Google itself (new-mailbox welcome mail) sit on the "
                                    f"target; they were never on the source and are not counted as strays")
        except Exception as exc:      # noqa: BLE001
            res["errors"].append(f"could not list the target's mailbox: {str(exc)[:100]}")
        res["notCopied"] = self._failed(("message", "gmail"))
        return res

    def _draft_message_ids(self, svc) -> set[str]:
        """The message behind every draft in the mailbox: messages.list returns a draft's message
        among the rest, and it is not a stray."""
        out, token = set(), None
        while True:
            r = self._x(lambda t=token: svc.users().drafts().list(userId="me", maxResults=500, pageToken=t).execute())
            out |= {(d.get("message") or {}).get("id") for d in r.get("drafts", [])}
            token = r.get("nextPageToken")
            if not token:
                return out - {None}

    def _draft_raw(self, svc, did) -> tuple[bytes, str]:
        d = self._x(lambda: svc.users().drafts().get(userId="me", id=did, format="raw").execute())
        m = d.get("message") or {}
        raw = m.get("raw", "")
        raw = raw if isinstance(raw, str) else raw.decode()
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), m.get("id", "")

    def _drafts(self, src, tgt, res, mapped_ids) -> None:
        """Drafts are copied by a pass of their own, so they are opened and compared like messages.
        Their underlying message ids are recorded so a draft is never mistaken for a stray."""
        for sid, tid, _ in self._pairs("draft", sample=True):
            res["checked"] += 1
            try:
                sraw, _m = self._draft_raw(src, sid)
            except Exception as exc:      # noqa: BLE001
                res["errors"].append(f"draft {sid}: could not read the SOURCE: {str(exc)[:100]}")
                continue
            try:
                traw, tmsg = self._draft_raw(tgt, tid)
            except Exception as exc:      # noqa: BLE001
                res["missing"].append({"source": sid, "target": tid, "why": f"draft not on the target ({str(exc)[:80]})"})
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
        if self._pairs("draft"):
            res["notes"].append("drafts: Gmail rebuilds a draft's Message-Id, Date and From when it is created, so drafts "
                                "are compared on recipients, subject and body, not byte for byte")

    @staticmethod
    def _list_all(svc) -> list[dict]:
        out, token = [], None
        while True:
            r = svc.users().messages().list(userId="me", maxResults=500, pageToken=token, includeSpamTrash=True).execute()
            out += r.get("messages", [])
            token = r.get("nextPageToken")
            if not token:
                return out

    # -- Calendar ---------------------------------------------------------
    def calendar(self) -> dict:
        res = self._blank()
        src, tgt = self.auth.source_calendar(self.src_user), self.auth.target_calendar(self.tgt_user)
        cal_map = {s: t for s, t, _ in self._pairs("calendar")}
        for key, tid, _ in self._pairs("event", sample=True):
            res["checked"] += 1
            src_cal, _, eid = key.partition("::")
            tgt_cal = cal_map.get(src_cal) or "primary"
            try:
                sev = self._x(lambda: src.events().get(calendarId=src_cal or "primary", eventId=eid).execute())
            except Exception as exc:      # noqa: BLE001
                res["errors"].append(f"event {eid}: could not read the SOURCE: {str(exc)[:100]}")
                continue
            try:
                tev = self._x(lambda: tgt.events().get(calendarId=tgt_cal, eventId=tid).execute())
            except Exception as exc:      # noqa: BLE001
                res["missing"].append({"source": eid, "target": tid, "name": sev.get("summary"),
                                       "why": f"not on the target ({str(exc)[:80]})"})
                continue
            diffs = compare_event(sev, tev, self._translate, self.db.target_for_source_id, res["notes"])
            self.evidence.append({"service": "calendar", "name": sev.get("summary"), "opened": True, "how": "fields compared"})
            if diffs:
                res["differences"].append({"item": sev.get("summary"), "source": eid, "target": tid, "diffs": diffs})
            else:
                res["identical"] += 1
        self._note_sample(res, "event")
        res["notCopied"] = self._failed(("event", "calendar"))
        return res

    # -- Contacts ---------------------------------------------------------
    def contacts(self) -> dict:
        res = self._blank()
        src, tgt = self.auth.source_people(self.src_user), self.auth.target_people(self.tgt_user)
        for sid, tid, _ in self._pairs("contact", sample=True):
            res["checked"] += 1
            try:
                sp = self._x(lambda: src.people().get(resourceName=sid, personFields=PERSON_FIELDS).execute())
            except Exception as exc:      # noqa: BLE001
                res["errors"].append(f"contact {sid}: could not read the SOURCE: {str(exc)[:100]}")
                continue
            try:
                tp = self._x(lambda: tgt.people().get(resourceName=tid, personFields=PERSON_FIELDS).execute())
            except Exception as exc:      # noqa: BLE001
                res["missing"].append({"source": sid, "target": tid, "why": f"not on the target ({str(exc)[:80]})"})
                continue
            name = ((sp.get("names") or [{}])[0]).get("displayName") or sid
            diffs = compare_contact(sp, tp, self._translate)
            self.evidence.append({"service": "contacts", "name": name, "opened": True, "how": "fields compared"})
            if diffs:
                res["differences"].append({"item": name, "source": sid, "target": tid, "diffs": diffs})
            else:
                res["identical"] += 1
        self._note_sample(res, "contact")
        res["notCopied"] = self._failed(("contact", "contacts"))
        return res

    # -- Tasks ------------------------------------------------------------
    def _tasks_of(self, svc, list_id) -> dict:
        out, token = {}, None
        while True:
            r = self._x(lambda t=token: svc.tasks().list(
                tasklist=list_id, maxResults=100, pageToken=t, showCompleted=True, showHidden=True).execute())
            for t in r.get("items", []):
                out[t["id"]] = t
            token = r.get("nextPageToken")
            if not token:
                return out

    def tasks(self) -> dict:
        res = self._blank()
        src, tgt = self.auth.source_tasks(self.src_user), self.auth.target_tasks(self.tgt_user)
        task_map = {s: t for s, t, _ in self._pairs("task")}
        keep = {s for s, _, _ in self._pairs("task", sample=True)}
        for slist, tlist, title in self._pairs("task_list"):
            try:
                sts, tts = self._tasks_of(src, slist), self._tasks_of(tgt, tlist)
            except Exception as exc:      # noqa: BLE001
                res["errors"].append(f"task list {title!r}: {str(exc)[:100]}")
                continue
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
        self._note_sample(res, "task")
        res["notCopied"] = self._failed(("task", "task_list", "tasks"))
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
            if r["errors"]:
                incomplete = True
                reasons.append(f"{user}: {svc} had {len(r['errors'])} check(s) that could not be made")
            for label in ("differences", "missing", "duplicates", "notCopied"):
                if r[label]:
                    bad = True
                    reasons.append(f"{user}: {svc} has {len(r[label])} {label}")
    if bad:
        return "DIFFERENCES", reasons
    if incomplete:
        return "INCOMPLETE", reasons
    return "IDENTICAL", reasons


def run(auth, db, settings, users: list[str] | None = None, services=ALL_SERVICES, retry=lambda f: f,
        progress=print, limit: int | None = None) -> dict:
    ident = {r["source_email"]: r["target_email"] for r in db.all_identities() if r["entity_type"] == "user"}
    chosen = [u for u in (users or list(ident)) if u in ident]
    services = tuple(s for s in ALL_SERVICES if s in services)
    report = {"generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "accountId": getattr(settings, "account_id", None),
              "sourceDomain": settings.source_domain, "targetDomain": settings.target_domain,
              "sampleLimit": getattr(settings, "sample_limit", None), "verifyLimit": limit, "services": list(services),
              "users": {}, "evidence": [], "notes": []}
    for u in chosen:
        progress(f"verify: {u}")
        v = Verifier(auth, db, settings, u, ident[u], retry=retry, limit=limit)
        per = {}
        for svc in services:
            try:
                per[svc] = getattr(v, svc)()
            except Exception as exc:      # noqa: BLE001 - one service failing must not lose the rest
                r = Verifier._blank()
                r["errors"].append(f"{svc} could not be verified: {type(exc).__name__}: {str(exc)[:160]}")
                per[svc] = r
        report["users"][u] = per
        report["evidence"] += [{"user": u, **e} for e in v.evidence]
    report["verdict"], report["reasons"] = verdict_of(report["users"], services)
    for u, per in report["users"].items():
        for svc, r in per.items():
            if r["checked"] == 0 and not r["errors"]:
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
    if report["verdict"] == "IDENTICAL" and tot["checked"] == 0:
        # Zero of zero is not a match. Nothing was copied (or nothing was recorded as
        # copied), so nothing could be compared -- that is "not verified", never a pass.
        report["verdict"] = "INCOMPLETE"
        report["reasons"].append("nothing was copied, so nothing could be verified")
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
         f"- **{t['identical']:,} of {t['checked']:,}** paired items identical; {t['filesOpened']:,} Drive files opened "
         f"and compared; {t['bytesCompared']:,} bytes read from each side",
         f"- differences {t['differences']}, missing on target {t['missing']}, duplicates {t['duplicates']}, "
         f"strays on target {t['extras']}, not copied (failed) {t['notCopied']}, checks that could not be made {t['errors']}", ""]
    if report["reasons"]:
        L += ["## Why not IDENTICAL", ""] + [f"- {r}" for r in report["reasons"]] + [""]
    for user, per in report["users"].items():
        L += [f"## {user}", "", "| service | checked | identical | differences | missing | duplicates | strays | not copied | could not check |",
              "|---|---|---|---|---|---|---|---|---|"]
        for svc, r in per.items():
            L.append(f"| {svc} | {r['checked']} | {r['identical']} | {len(r['differences'])} | {len(r['missing'])} | "
                     f"{len(r['duplicates'])} | {len(r['extras'])} | {len(r['notCopied'])} | {len(r['errors'])} |")
        L.append("")
        for svc, r in per.items():
            for label in ("differences", "missing", "duplicates", "notCopied", "errors"):
                for item in r[label][:25]:
                    L.append(f"- **{svc} / {label}**: {json.dumps(item, default=str)[:400]}")
            for n in r["notes"][:10]:
                L.append(f"- {svc} note: {n}")
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
        for svc, r in per.items():
            verdict, _ = verdict_of({user: {svc: r}}, (svc,))
            if verdict == "IDENTICAL" and r["checked"] == 0:
                verdict = "INCOMPLETE"          # nothing checked is not a pass
            db.save_user_verification(user, svc, verdict, r["checked"], r["identical"],
                                      (r.get("sampled") or {}).get("of"), {
                "counts": {k: len(r[k]) for k in ("differences", "missing", "duplicates", "extras", "notCopied", "errors")},
                **{k: r[k][:25] for k in ("differences", "missing", "duplicates", "notCopied", "errors")},
                "extras": r["extras"][:10], "notes": r["notes"][:10], "sampled": r.get("sampled")})


def verify_user(auth, db, settings, source_user: str, services, limit: int | None, retry=lambda f: f) -> str | None:
    """Verify what one user's migration just finished, and record it. Returns the verdict, or None
    when it could not be run -- never raises: it runs on the heels of a migration worker and a
    failed check must not become a failed migration."""
    try:
        report = run(auth, db, settings, [source_user], tuple(services), retry, lambda *_: None, limit)
        save_user_results(db, report)
        log.info("[%s] verified %s: %s (%d of %d identical)", source_user, ",".join(services), report["verdict"],
                 report["totals"]["identical"], report["totals"]["checked"])
        return report["verdict"]
    except Exception:      # noqa: BLE001
        log.exception("[%s] verification could not be run", source_user)
        return None


def run_and_save(auth, db, settings, users=None, services=ALL_SERVICES, retry=lambda f: f, progress=print,
                 limit: int | None = None, base: str | None = None) -> dict:
    report = run(auth, db, settings, users, services, retry, progress, limit)
    jp, mp = save(report, base)
    save_user_results(db, report)
    progress(f"verification: {report['verdict']} -- {report['totals']['identical']} of {report['totals']['checked']} "
             f"identical; report saved to {os.path.relpath(mp, HERE)}")
    return report


def main(argv=None) -> int:
    import argparse

    from auth import AuthManager
    from config import Settings
    from db import MigrationDB
    from resilience import retry_on_google_error
    ap = argparse.ArgumentParser(description="Verify a migration one to one; writes nothing to either tenant.")
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--user", action="append", help="limit to these source users")
    ap.add_argument("--services", default=",".join(ALL_SERVICES))
    ap.add_argument("--limit", type=int, help="check only this many of each kind of item per user, evenly spread")
    a = ap.parse_args(argv)
    services = tuple(s.strip() for s in a.services.split(","))
    settings = Settings(account_id=a.account_id)
    # The scopes a credential carries follow these flags (config.source_scopes), and main.py switches
    # them on for the services a run names. Without this, reading contacts or tasks is a 403.
    settings.migrate_contacts = settings.migrate_contacts or "contacts" in services
    settings.migrate_tasks = settings.migrate_tasks or "tasks" in services
    db = MigrationDB(settings.db_path)
    report = run_and_save(AuthManager(settings), db, settings, a.user, services,
                          retry=retry_on_google_error(max_retries=settings.max_retries),
                          limit=a.limit, base=verify_dir(a.account_id))
    # 0 whenever the check RAN. The verdict is in the report, not the exit code: a
    # non-zero exit reads as a crash to everything that watches jobs (run_watch opens an
    # incident for it), and "the migration has differences" is a finding, not a crash.
    return 0


if __name__ == "__main__":
    sys.exit(main())
