"""Complete thread reads and duplicate-safe target-only comment/reply delivery."""

from __future__ import annotations

import html
from collections import Counter
from link_rewrite import rewrite_text
from db import utc_now

FIELDS = "id,content,anchor,quotedFileContent,createdTime,resolved,author(displayName),replies(id,content,createdTime,action,author(displayName))"


def list_threads(svc, fid, execute=lambda fn: fn()):
    out, token, seen = [], None, set()
    while True:
        response = execute(
            lambda: (
                svc.comments()
                .list(
                    fileId=fid,
                    pageSize=100,
                    pageToken=token,
                    fields="nextPageToken,comments(" + FIELDS + ")",
                )
                .execute()
            )
        )
        out.extend(response.get("comments", []))
        token = response.get("nextPageToken")
        if not token:
            return out
        if token in seen:
            raise RuntimeError("Comment inventory repeated a page token")
        seen.add(token)


def _text(value):
    # Drive keeps a comment's text without the trailing space the engine writes after the
    # "[Author, date]" prefix, so an exact comparison called every action-only reply
    # (empty body) "differs" and refused it. Trailing whitespace is not a difference.
    # Only text is trimmed: quotedFileContent is an object ({"mimeType", "value"}), and trimming it
    # failed health@, ward15@ and ward4@ with "'dict' object has no attribute 'rstrip'" (7 Nov 2026).
    if isinstance(value, str):
        return value.rstrip()
    return "" if value is None else value


def _quote(q):
    """A quote as (mimeType, value); an empty one and none at all are the same (Drive drops an empty quote)."""
    value = q.get("value") if isinstance(q, dict) else None
    return (q.get("mimeType"), value) if value else None


def quote_match(source, target) -> str:
    """'same', 'escaped' or 'differs'. Drive HTML-escapes a quote when a comment is written, and the engine
    used to post the value as read (already escaped), so every copy made before 15 Nov 2026 carries one more
    level ('&amp;' -> '&amp;amp;'; health@, ward4@ on account 3). 'escaped' names exactly that and nothing else."""
    a, b = _quote(source), _quote(target)
    if a == b:
        return "same"
    if a and b and a[0] == b[0] and html.escape(a[1], quote=False) == b[1]:
        return "escaped"
    return "differs"


def for_writing(quoted):
    """The quote as it must be sent so Drive stores what the source shows: one level un-escaped."""
    return {**quoted, "value": html.unescape(quoted["value"])} if _quote(quoted) else quoted


def _body_matches(found, body):
    # Only comment text has Drive's trailing-whitespace normalization. A quote matches exactly or with the one
    # extra escaping level of an older copy of ours; any other change is a target edit and is preserved.
    def same(key, value):
        if key == "content":
            return _text(found.get(key)) == _text(value)
        if key == "quotedFileContent":
            return quote_match(value, found.get(key)) != "differs"
        return found.get(key) == value
    return all(same(key, value) for key, value in body.items())


def content(row, lookup):
    author = (row.get("author") or {}).get("displayName") or "unknown"
    return (
        "["
        + author
        + ", "
        + (row.get("createdTime") or "")[:10]
        + "] "
        + rewrite_text(row.get("content", ""), lookup)[0]
    )


def differences(source, target, lookup):
    remaining = list(target)
    diffs = []
    for comment in source:
        wanted = content(comment, lookup)
        candidates = [
            c
            for c in remaining
            if _text(c.get("content"))
            in (_text(wanted), _text(rewrite_text(comment.get("content", ""), lookup)[0]))
        ]
        if len(candidates) > 1:
            # Two threads with the same words on different spots (live, finance@s.: one PDF,
            # "[FINANCE, 2023-01-13]" twice): the anchor tells them apart, as delivery does.
            candidates = [c for c in candidates if c.get("anchor") == comment.get("anchor")
                          and quote_match(comment.get("quotedFileContent"), c.get("quotedFileContent")) != "differs"]
        if len(candidates) != 1:
            diffs.append("comment thread missing or ambiguous")
            continue
        found = candidates[0]
        remaining.remove(found)
        if bool(comment.get("resolved")) != bool(found.get("resolved")):
            diffs.append("comment resolution differs")
        if comment.get("anchor") != found.get("anchor"):
            diffs.append("comment anchor differs")
        quoted = quote_match(comment.get("quotedFileContent"), found.get("quotedFileContent"))
        if quoted == "escaped":
            diffs.append("comment quote escaped twice (Drive escapes on write; copy made before the fix)")
        elif quoted == "differs":
            diffs.append("comment quotedFileContent differs")
        want = Counter(
            (_text(content(r, lookup)), r.get("action", ""))
            for r in comment.get("replies", [])
        )
        actual = Counter(
            (_text(r.get("content")), r.get("action", ""))
            for r in found.get("replies", [])
        )
        raw = Counter(
            (_text(rewrite_text(r.get("content", ""), lookup)[0]), r.get("action", ""))
            for r in comment.get("replies", [])
        )
        if actual not in (want, raw):
            diffs.append("comment replies differ")
    if remaining:
        diffs.append("extra target comment threads")
    return diffs


def reconcile(migrator, sid, tid):
    from domain_guard import require_not_source

    require_not_source(
        migrator.target_user.rsplit("@", 1)[-1], migrator.settings.source_domain
    )
    if migrator.db.get_target_id(migrator.source_user, sid, "file") != tid:
        raise RuntimeError("Comment repair requires an exact mapped target file")
    lookup = migrator.db.target_for_source_id
    source = list_threads(
        migrator.src, sid, lambda fn: migrator._retry(fn, write=False, tenant="source")
    )
    target = list_threads(
        migrator.tgt, tid, lambda fn: migrator._retry(fn, write=False)
    )
    written = 0
    with migrator.db.write() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS comment_delivery_attempts (
            source_user TEXT,item_key TEXT,target_file TEXT,state TEXT,updated_at TEXT,
            PRIMARY KEY(source_user,item_key))""")

    def deliver(key, kind, body, candidates, call):
        nonlocal written
        # Keyed by file: a comment id is unique within one file only -- live, ss@'s two
        # copies of one Doc both carried AAABghWPKe8, the second found the first's mapping
        # and stopped ("Mapped comment/reply is missing"). An old unscoped mapping counts
        # only when its comment is on THIS file's copy.
        legacy = migrator.db.get_target_id(migrator.source_user, key, kind)
        key = f"{sid}::{key}"
        mapped = migrator.db.get_target_id(migrator.source_user, key, kind)
        if not mapped and legacy and any(r["id"] == legacy for r in candidates):
            mapped = legacy
        if mapped:
            found = next((r for r in candidates if r["id"] == mapped), None)
            if not found:
                # A proven receipt for another target copy is not evidence
                # about this freshly mapped file. Missing replies on the SAME
                # copy, or without that receipt, remain a target-edit hold.
                receipt = migrator.db.conn.execute(
                    "SELECT target_file FROM comment_delivery_attempts WHERE source_user=? AND item_key=?",
                    (migrator.source_user, key)).fetchone()
                if not receipt or not str(receipt[0] or "").strip() or receipt[0] == tid:
                    raise RuntimeError(
                        "Mapped comment/reply is missing; do not create another blindly"
                    )
            else:
                if not _body_matches(found, body):
                    raise RuntimeError(
                        "Existing target comment/reply differs; preserve possible target edits"
                    )
                return mapped
        matches = [
            r for r in candidates if _body_matches(r, body)
        ]
        if len(matches) > 1:
            raise RuntimeError("Ambiguous comment/reply copies")
        if matches:
            target_id = matches[0]["id"]
        else:
            # Only an attempt against THIS target file says anything about it. A file re-copied to a new
            # target (repair, quarantine) clears its comments' mappings but not their attempt rows, and the
            # old copy's rows then blocked the new copy's comments for good (finance@, it.department@:
            # "needs reconciliation", live 7 Nov 2026) -- the user's comments never reached the new file.
            prior = migrator.db.conn.execute(
                "SELECT state FROM comment_delivery_attempts WHERE source_user=? AND item_key=? "
                "AND target_file=?",
                (migrator.source_user, key, tid),
            ).fetchone()
            if prior:
                raise RuntimeError(
                    "Prior uncertain comment/reply delivery needs reconciliation"
                )
            with migrator.db.write() as c:
                c.execute(
                    "INSERT OR REPLACE INTO comment_delivery_attempts VALUES(?,?,?,'UNCERTAIN',?)",
                    (migrator.source_user, key, tid, utc_now()),
                )
            # No automatic request retry after an ambiguous create response.
            for limiter_name in ("_tgt_write_limiter", "_project_limiter"):
                limiter=getattr(migrator,limiter_name,None)
                if limiter is not None:limiter.acquire()
            created = call().execute()
            target_id = created["id"]
            written += 1
        migrator.db.record_mapping(migrator.source_user, key, target_id, kind)
        with migrator.db.write() as c:
            c.execute(
                "INSERT OR REPLACE INTO comment_delivery_attempts VALUES(?,?,?,'CONFIRMED',?)",
                (migrator.source_user, key, tid, utc_now()),
            )
        return target_id

    for comment in source:
        body = {"content": content(comment, lookup)}
        for field in ("anchor", "quotedFileContent"):
            if comment.get(field):
                body[field] = comment[field]
        cid = deliver(
            comment["id"],
            "comment",
            body,
            target,
            lambda: migrator.tgt.comments().create(
                fileId=tid, fields=FIELDS,
                body={**body, **({"quotedFileContent": for_writing(body["quotedFileContent"])}
                                 if "quotedFileContent" in body else {})},
            ),
        )
        found = next((c for c in target if c["id"] == cid), {})
        for reply in comment.get("replies", []):
            rb = {"content": content(reply, lookup)}
            if reply.get("action"):
                rb["action"] = reply["action"]
            deliver(
                comment["id"] + "::" + reply["id"],
                "comment_reply",
                rb,
                found.get("replies", []),
                lambda: migrator.tgt.replies().create(
                    fileId=tid, commentId=cid, body=rb, fields="id,content,action"
                ),
            )
    return written
