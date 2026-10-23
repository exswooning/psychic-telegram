"""Recover messages without Message-ID, without adding headers or retrying inserts.

A complete inventory of *unclaimed* destination messages and a decoded MIME-tree
hash preserve legitimate source multiplicity. Durable uncertain attempts block a
second insert after a lost response until an exact copy can be adopted.
"""

from __future__ import annotations

import base64
import email
import hashlib
import json
import threading

_LOCKS = {}
_GUARD = threading.Lock()


def fingerprint(raw, missing_id=True):
    from verify_sample import VOLATILE_HEADERS

    message = email.message_from_bytes(raw)

    def node(part, root=False):
        ignore = set(VOLATILE_HEADERS)
        if missing_id and root:
            ignore.add("message-id")
        # Encoding and multipart boundaries can be assigned by Gmail; retain
        # all semantic MIME headers and compare every individual attachment.
        ignore.update({"content-transfer-encoding", "content-type"})
        headers = {}
        for key, value in part.items():
            if key.lower() not in ignore:
                headers.setdefault(key.lower(), []).append(" ".join(str(value).split()))
        params = sorted(
            (str(k).lower(), str(v))
            for k, v in (part.get_params() or [])[1:]
            if str(k).lower() != "boundary"
        )
        children = part.get_payload() if part.is_multipart() else None
        payload = (
            [node(p) for p in children]
            if isinstance(children, list)
            else hashlib.sha256(part.get_payload(decode=True) or b"").hexdigest()
        )
        return [headers, part.get_content_type(), params, payload]

    return hashlib.sha256(
        json.dumps(node(message, True), sort_keys=True).encode()
    ).hexdigest()


def _schema(db):
    with db.write() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS mail_recovery_attempts (
            source_user TEXT NOT NULL, source_id TEXT NOT NULL, target_user TEXT NOT NULL,
            digest TEXT NOT NULL, state TEXT NOT NULL, target_id TEXT, updated_at TEXT NOT NULL,
            PRIMARY KEY(source_user,source_id))""")


def _put(db, user, sid, target, digest, state, tid=None):
    from db import utc_now

    with db.write() as c:
        c.execute(
            """INSERT INTO mail_recovery_attempts VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(source_user,source_id) DO UPDATE SET state=excluded.state,
            target_id=excluded.target_id,updated_at=excluded.updated_at""",
            (user, sid, target, digest, state, tid, utc_now()),
        )


def _unclaimed_matches(gm, digest):
    from verification_reads import gmail_raw
    from verify_sample import _stop_check

    used = {
        r[0]
        for r in gm.db.conn.execute(
            "SELECT target_id FROM id_mapping WHERE source_user=? AND type='message'",
            (gm.source_user,),
        )
    }
    token, seen, matches = None, set(), []
    while True:
        _stop_check()
        gm.limiter.acquire()
        page = gm._retry(
            lambda: (
                gm.tgt.users()
                .messages()
                .list(
                    userId="me", maxResults=500, includeSpamTrash=True, pageToken=token
                )
                .execute()
            )
        )
        for ref in page.get("messages", []):
            if ref["id"] in used:
                continue
            raw, metadata = gmail_raw(gm.tgt, ref["id"], lambda fn: gm._retry(fn))
            if "DRAFT" in metadata.get("labelIds", []) or "CHAT" in metadata.get(
                "labelIds", []
            ):
                continue
            if fingerprint(raw) == digest:
                matches.append(metadata)
        token = page.get("nextPageToken")
        if not token:
            return matches
        if token in seen:
            raise RuntimeError("Incomplete destination inventory: repeated page token")
        seen.add(token)


def insert_without_message_id(gm, sid, raw, insert):
    """One durable attempt, with fresh content readback before it can be mapped."""
    from domain_guard import require_not_source
    from verification_reads import gmail_raw, status_of

    require_not_source(gm.target_user.rsplit("@", 1)[-1], gm.settings.source_domain)
    # Writes only to the target; the source is read through the migration's own Gmail
    # grant, which is read-only by scope on every source. Requiring a PROTECTED source
    # here failed every message without a Message-ID on any sandbox source.
    if gm.settings.dry_run:
        raise RuntimeError("Missing-ID recovery requires an apply run")
    if gm.target_user != gm.db.resolve_identity(gm.source_user) or (
        gm.target_user.rsplit("@", 1)[-1].lower() != gm.settings.target_domain.lower()
    ):
        raise RuntimeError(
            "Recovery target does not match the configured identity and tenant"
        )
    data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    if email.message_from_bytes(data).get("Message-ID"):
        raise ValueError("Missing-ID recovery called for a message that has Message-ID")
    digest = fingerprint(data)
    with _GUARD:
        lock = _LOCKS.setdefault(
            (gm.settings.db_path, gm.target_user), threading.Lock()
        )
    with lock:
        _schema(gm.db)
        prior = gm.db.conn.execute(
            "SELECT * FROM mail_recovery_attempts WHERE source_user=? AND source_id=?",
            (gm.source_user, sid),
        ).fetchone()
        if prior and (
            prior["digest"] != digest or prior["target_user"] != gm.target_user
        ):
            raise RuntimeError(
                "Recovery source content or target changed; previous attempt needs reconciliation"
            )
        matches = _unclaimed_matches(gm, digest)
        if len(matches) > 1:
            raise RuntimeError(
                "Multiple unclaimed MIME-identical copies; source multiplicity needs reconciliation"
            )
        if matches:
            found = matches[0]
            _put(
                gm.db,
                gm.source_user,
                sid,
                gm.target_user,
                digest,
                "CONFIRMED",
                found["id"],
            )
            gm.db.record_mapping(gm.source_user, sid, found["id"], "message")
            return {**found, "adopted": True}
        if prior and prior["state"] in ("UNCERTAIN", "CONFIRMED"):
            raise RuntimeError(
                "Prior insert may have landed; no exact copy is currently visible. A second insert is blocked"
            )
        _put(gm.db, gm.source_user, sid, gm.target_user, digest, "UNCERTAIN")
        try:
            # Deliberately bypass the transport retry decorator. An insert is
            # not idempotent; the next invocation must reconcile its outcome.
            result = insert()
        except Exception as exc:
            if status_of(exc) in (400, 401, 403, 404, 413):
                _put(gm.db, gm.source_user, sid, gm.target_user, digest, "REJECTED")
            raise RuntimeError(
                f"Missing-ID insert needs reconciliation: {exc}"
            ) from exc
        target_raw, metadata = gmail_raw(gm.tgt, result["id"], lambda fn: gm._retry(fn))
        if fingerprint(target_raw) != digest:
            raise RuntimeError(
                "Inserted message failed MIME/header/attachment readback; repair remains unresolved"
            )
        _put(
            gm.db,
            gm.source_user,
            sid,
            gm.target_user,
            digest,
            "CONFIRMED",
            result["id"],
        )
        gm.db.record_mapping(gm.source_user, sid, result["id"], "message")
        return {**result, "threadId": metadata.get("threadId")}
