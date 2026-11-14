"""Lighter proof for DMS-delivered mail (operator's choice, 14 Nov 2026; off unless DMS_PROOF=light).

Full proof downloads every message from both tenants and compares the bytes: about a day for 141,000
messages on account 3's 3 MB/s line. Light proof, per DMS-delivered message:
- identity: its Message-ID finds exactly one message on the target (or the ledger already maps it);
- facts: Date, Subject, From, To and Cc equal, and the target's size within the few KB Gmail adds on
  delivery -- all from metadata reads of a few hundred bytes, no body;
- labels: compared as everywhere else (verify_sample.compare_labels).
A sample per mailbox (DMS_LIGHT_SAMPLE, 50) still gets full byte-for-byte proof first; if any sampled
message fails, the whole mailbox falls back to full proof. Mail the engine wrote (link mail, rewritten)
is never proven lightly. Anything light proof cannot judge falls back to full proof, never to a pass.
What it can miss: a body that changed while size, date, subject and every address stayed the same.
"""
from __future__ import annotations

import hashlib
import json
import os

LIGHT_HEADERS = ["Message-ID", "Date", "Subject", "From", "To", "Cc"]
COMPARED = ("date", "subject", "from", "to", "cc")
SIZE_SLACK = 16384          # bytes Gmail may add on delivery (Received, Delivered-To, X-Forwarded-Encrypted...)
DEFERRED = ("SKIPPED_NO_DRIVE_LINK",)


def enabled() -> bool:
    return (os.getenv("DMS_PROOF", "full") or "full").strip().lower() == "light"


def sample(ids, n: int | None = None) -> set:
    """A fixed, evenly spread sample of a mailbox: the same ids every run, so a rerun re-checks the same ones."""
    n = int(os.getenv("DMS_LIGHT_SAMPLE", "50")) if n is None else n
    return set(sorted(ids, key=lambda i: hashlib.sha256(str(i).encode()).hexdigest())[:max(0, n)])


def eligible(db, source_user: str, mid: str) -> bool:
    """DMS mail only: deferred by the split run, or new mail the engine never saw. Never engine link mail."""
    row = db.conn.execute("SELECT status FROM audit_log WHERE source_user=? AND item_id=? AND item_type='message'",
                          (source_user, mid)).fetchone()
    return row is None or row[0] in DEFERRED


def _meta(gm, svc, mid, label):
    gm.limiter.acquire()
    m = gm._retry(lambda: svc.users().messages().get(userId="me", id=mid, format="metadata",
                                                     metadataHeaders=LIGHT_HEADERS).execute(), label=label)
    headers = {h["name"].lower(): " ".join(str(h.get("value", "")).split())
               for h in (m.get("payload") or {}).get("headers") or []}
    return m, headers


def check(gm, db, source_user: str, mid: str, mapped: str | None):
    """('verified' | 'labels' | 'unknown', target_id, evidence). 'unknown' always means: prove it fully."""
    import verify_sample as V
    from gmail_engine import SYSTEM_LABELS
    sm, sh = _meta(gm, gm.src, mid, "gmail.light.source")
    msgid = sh.get("message-id")
    if not msgid and not mapped:
        return "unknown", None, {"why": "no Message-ID and no mapping"}
    tid = mapped
    if not tid:
        gm.limiter.acquire()
        found = gm._retry(lambda: gm.tgt.users().messages().list(
            userId="me", q=f"rfc822msgid:{msgid}", includeSpamTrash=True, maxResults=2).execute(),
            label="gmail.light.match")
        hits = found.get("messages", [])
        if len(hits) != 1 or found.get("nextPageToken"):
            return "unknown", None, {"why": "the target match is absent or ambiguous"}
        tid = hits[0]["id"]
    clash = db.conn.execute("SELECT 1 FROM id_mapping WHERE source_user=? AND type='message' AND target_id=? "
                            "AND source_id!=? LIMIT 1", (source_user, tid, mid)).fetchone()
    if clash:
        return "unknown", tid, {"why": "that target copy is mapped to another message"}
    if set(sm.get("labelIds") or []) - SYSTEM_LABELS - set(db.get_label_map(source_user)):
        return "unknown", tid, {"why": "a source label has no target label yet"}
    tm, th = _meta(gm, gm.tgt, tid, "gmail.light.target")
    facts = [k for k in COMPARED if sh.get(k, "") != th.get(k, "")]
    if msgid and th.get("message-id") and th.get("message-id") != msgid:
        facts.append("message-id")
    ssz, tsz = int(sm.get("sizeEstimate") or 0), int(tm.get("sizeEstimate") or 0)
    if not ssz or not (0 <= tsz - ssz <= SIZE_SLACK):
        facts.append(f"size {ssz} -> {tsz}")
    labels = V.compare_labels(set(gm._map_label_ids(sm.get("labelIds") or [])), set(tm.get("labelIds") or []))
    evidence = {"target": tid, "proof": "light", "scope": "identity, headers, size and labels (no body)",
                "sizes": [ssz, tsz], "labelDifferences": labels}
    if facts:
        return "unknown", tid, {**evidence, "why": "differs: " + ", ".join(facts)}
    return ("labels" if labels else "verified"), tid, evidence


def record(db, source_user: str, mid: str, tid: str, verdict: str, evidence: dict) -> None:
    """The receipt, in the form the label alignment and the verify read. contentProven is True by the
    operator's acceptance of light proof; 'proof': 'light' says how it was proven."""
    from db import utc_now
    proof = {**evidence, "verifiedAt": utc_now(), "contentProven": True, "comparison": "light",
             "verificationKind": "item", "findings": {"errors": []}}
    with db.write() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS migration_item_receipts (
            source_user TEXT NOT NULL, service TEXT NOT NULL, source_id TEXT NOT NULL,
            target_id TEXT NOT NULL, verified_at TEXT NOT NULL, status TEXT NOT NULL,
            evidence TEXT NOT NULL, PRIMARY KEY(source_user,service,source_id))""")
        c.execute("INSERT OR REPLACE INTO migration_item_receipts VALUES(?,?,?,?,?,?,?)",
                  (source_user, "gmail", mid, tid, proof["verifiedAt"],
                   "VERIFIED" if verdict == "verified" else "NEEDS_RECHECK", json.dumps(proof)))
