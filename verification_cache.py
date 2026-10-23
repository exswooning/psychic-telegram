"""Reuse successful Gmail comparisons only with complete two-tenant change evidence.

A cache entry is not an acceptance verdict. Source coverage, drafts, target extras
and selected feature checks still run. Unavailable/expired history means full reads.
"""

from __future__ import annotations

import hashlib
import json

from db import utc_now

VERSION = 1


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


class MailCache:
    def __init__(self, verifier, source, target, label_maps):
        self.v = verifier
        self.user = verifier.src_user
        from pathlib import Path
        import verify_sample, verification_reads, mail_identity
        comparison_code = digest({
            module.__name__: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for module in (verify_sample, verification_reads, mail_identity)
        })
        self.context = digest(
            {
                "version": VERSION, "comparisonCode":comparison_code,
                "targetUser": verifier.tgt_user,
                "rewrite": getattr(verifier.settings, "rewrite_drive_links", False),
                "labels": label_maps,
                "rewrittenSourceIds": [row[0] for row in verifier.db.conn.execute(
                    "SELECT item_id FROM audit_log WHERE source_user=? AND item_type='link_rewrite' AND status='SUCCESS' ORDER BY item_id",
                    (self.user,),
                )],
                "mappings": [
                    tuple(row)
                    for row in verifier.db.conn.execute(
                        "SELECT source_user,type,source_id,target_id FROM id_mapping WHERE type IN ('file','folder','shortcut','calendar','group') ORDER BY source_user,type,source_id"
                    )
                ],
            }
        )
        self.active = False
        self.rows, self.fresh = {}, {}
        self.changed = {"source": set(), "target": set()}
        self.starts = {}
        self.hits = 0
        self.reason = "disabled or sampled"
        if (
            not getattr(verifier.settings, "verification_cache_enabled", True)
            or verifier.limit is not None
        ):
            return
        try:
            with verifier.db.write() as c:
                c.execute("""CREATE TABLE IF NOT EXISTS verification_mail_cache (
                    source_user TEXT, source_id TEXT, target_id TEXT, context TEXT,
                    payload TEXT, checked_at TEXT, PRIMARY KEY(source_user,source_id))""")
                c.execute("""CREATE TABLE IF NOT EXISTS verification_mail_checkpoints (
                    source_user TEXT PRIMARY KEY, target_user TEXT, context TEXT,
                    source_history TEXT, target_history TEXT, checked_at TEXT)""")
            prior = verifier.db.conn.execute(
                "SELECT * FROM verification_mail_checkpoints WHERE source_user=?",
                (self.user,),
            ).fetchone()
            for side, svc in (("source", source), ("target", target)):
                profile = verifier._x(
                    lambda: svc.users().getProfile(userId="me").execute()
                )
                anchor = str(profile.get("historyId") or "")
                if not anchor.isdigit():
                    raise RuntimeError("Mailbox history checkpoint unavailable")
                self.starts[side] = anchor
            if (
                prior
                and prior["context"] == self.context
                and prior["target_user"] == verifier.tgt_user
            ):
                for side, svc in (("source", source), ("target", target)):
                    self.changed[side] = self._changes(
                        svc, prior[side + "_history"], self.starts[side]
                    )
                self.rows = {
                    r["source_id"]: dict(r)
                    for r in verifier.db.conn.execute(
                        "SELECT * FROM verification_mail_cache WHERE source_user=? AND context=?",
                        (self.user, self.context),
                    )
                }
            self.active = True
            self.reason = (
                "complete history evidence"
                if self.rows
                else "new comparison context; full reads"
            )
        except Exception as exc:
            self.rows.clear()
            # Fresh profiles can seed a full-read pass after expired history.
            self.active = len(self.starts) == 2
            self.reason = "full-read fallback: " + str(exc)[:200]

    def _changes(self, svc, start, current):
        if not str(start).isdigit() or int(current) < int(start):
            raise RuntimeError("Mailbox history moved backwards or is unavailable")
        if str(start) == str(current):
            return set()
        ids, token, seen, records = set(), None, set(), 0
        while True:
            response = self.v._x(
                lambda: (
                    svc.users()
                    .history()
                    .list(
                        userId="me",
                        startHistoryId=str(start),
                        maxResults=500,
                        pageToken=token,
                    )
                    .execute()
                )
            )
            if not str(response.get("historyId") or "").isdigit() or int(
                response["historyId"]
            ) < int(current):
                raise RuntimeError(
                    "History response did not cover the observed mailbox checkpoint"
                )
            for record in response.get("history", []):
                records += 1
                if records > 10000:
                    raise RuntimeError(
                        "History window too large; full comparison required"
                    )
                for message in record.get("messages", []):
                    if message.get("id"):
                        ids.add(message["id"])
                for kind in (
                    "messagesAdded",
                    "messagesDeleted",
                    "labelsAdded",
                    "labelsRemoved",
                ):
                    for change in record.get(kind, []):
                        message_id = change.get("message", {}).get("id")
                        if not message_id:
                            raise RuntimeError(
                                "History change omitted its message identity"
                            )
                        ids.add(message_id)
                if not any(
                    record.get(k)
                    for k in (
                        "messages",
                        "messagesAdded",
                        "messagesDeleted",
                        "labelsAdded",
                        "labelsRemoved",
                    )
                ):
                    raise RuntimeError(
                        "History record has no usable message identities"
                    )
                known = {
                    "id",
                    "messages",
                    "messagesAdded",
                    "messagesDeleted",
                    "labelsAdded",
                    "labelsRemoved",
                }
                if set(record) - known:
                    raise RuntimeError(
                        "Unrecognized history change; full comparison required"
                    )
            token = response.get("nextPageToken")
            if not token:
                return ids
            if token in seen:
                raise RuntimeError("Repeated Gmail history page token")
            seen.add(token)

    def get(self, source_id, target_id):
        row = self.rows.get(source_id)
        if not self.active or not row or row["target_id"] != target_id:
            return None
        if source_id in self.changed["source"] or target_id in self.changed["target"]:
            return None
        try:
            evidence = json.loads(row["payload"])
            if not evidence.get("sourceSha256") or not evidence.get("targetSha256"):
                return None
        except (ValueError, TypeError):
            return None
        self.hits += 1
        self.fresh[source_id] = (target_id, evidence)
        return evidence

    def put(self, source_id, target_id, evidence):
        if self.active:
            self.fresh[source_id] = (target_id, evidence)

    def finish(self):
        if not self.active:
            return
        with self.v.db.write() as c:
            # Old rows must not acquire new anchors without having been revalidated.
            c.execute(
                "DELETE FROM verification_mail_cache WHERE source_user=?", (self.user,)
            )
            for sid, (tid, evidence) in self.fresh.items():
                c.execute(
                    "INSERT INTO verification_mail_cache VALUES(?,?,?,?,?,?)",
                    (
                        self.user,
                        sid,
                        tid,
                        self.context,
                        json.dumps(evidence),
                        utc_now(),
                    ),
                )
            c.execute(
                "INSERT OR REPLACE INTO verification_mail_checkpoints VALUES(?,?,?,?,?,?)",
                (
                    self.user,
                    self.v.tgt_user,
                    self.context,
                    self.starts["source"],
                    self.starts["target"],
                    utc_now(),
                ),
            )

    def stats(self):
        return {
            "reusedComparisons": self.hits,
            "freshComparisons": len(self.fresh) - self.hits,
            "contextVersion": VERSION,
            "reason": self.reason,
            "scope": "message content/labels only; inventories and other scopes still checked",
        }
