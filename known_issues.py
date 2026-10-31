"""
known_issues.py
===============
Every kind of one-to-one finding seen on a real migration, with why it happens and
how it is fixed -- and a record of each time one was fixed.

`classify` tags each finding in verification_repair_queue with its kind. When a fresh,
complete check no longer finds it, the row turns RESOLVED and one line is appended to
`issue_fix_log` (who, what, how it was fixed, when): the durable answer to "was this
fixed, and how". A kind marked `auto` has a repair the account's repair policy runs on
its own the next time the kind is found (verification_repairs.scoped_repair); the rest
say who has to act. Kinds were taken from dharan.gov.np -> mdharan.com (2026-10-23).
"""
from __future__ import annotations

import json

# id, service, matches(text) on the finding's own words, title, cause, fix, auto
ISSUES = [
    ("gmail.labels_added", "gmail", lambda t: "labels added on the target" in t,
     "Gmail added a label the source message does not have",
     "Inserted as SENT only, Gmail on the new tenant filed it as received mail (INBOX, "
     "or a category tab): the sender address is the OLD domain's, not the account's own.",
     "Remove the extra labels on the target copy (repair.repair_verified_gmail_labels); "
     "new mail is corrected as it is written (migration_checks.verify_written_mail).", True),
    ("gmail.labels_missing", "gmail", lambda t: "labels missing on the target" in t or "unread state differs" in t,
     "A label or read state did not come across",
     "Label mapping or a later change on the target.",
     "Add the missing labels / read state on the target copy (same repair).", True),
    ("gmail.no_message_id", "gmail", lambda t: "Missing Message-ID" in t,
     "Old message with no Message-ID header",
     "Nothing to look the copy up by, so it was not inserted blind (duplicate risk).",
     "Match by the whole MIME tree's hash against every unclaimed target message; one exact "
     "match is adopted, none is inserted once with a durable attempt record "
     "(mail_identity.insert_without_message_id).", True),
    ("gmail.unmapped_source", "gmail", lambda t: "UNMAPPED_SOURCE" in t or "has no migration mapping" in t,
     "Source message with no copy",
     "Arrived after the run, or the run stopped before it.",
     "Copied by the missing-mail repair (repair.repair_verified_missing_mail) or the delta.", True),
    ("gmail.body_link_copy", "gmail", lambda t: "body differs" in t,
     "Message body differs",
     "Usually Bitport's own Drive-link rewrite pointing at one of several copies of a file "
     "shared with many users; otherwise a real content difference.",
     "Checker accepts any genuine copy (verify_sample.copy_lookup, 2026-10-23); what remains "
     "is a real difference for a person.", False),
    ("chat.attributed", "chat", lambda t: "posted by the user with an attribution" in t,
     "Chat message re-posted with an attribution",
     "The sender is a bot (e.g. Drive notifications) or has no account on the target; Chat lets "
     "nobody post as them, so the user posts the same text prefixed \"[originally from ...]\".",
     "None possible through the Chat API. Text and order are intact; listed in the final report.", False),
    ("chat.space_type", "chat", lambda t: "Chat conversation type differs" in t,
     "A direct message became a group chat",
     "Chat's import mode can only create spaces and group chats; a DIRECT_MESSAGE cannot be "
     "imported, so the conversation lands as a two-person group chat (measured 2026-10-31).",
     "None possible through the Chat API: messages, order and times are intact. Listed in the final report.", False),
    ("chat.members", "chat", lambda t: "Chat participants differ" in t,
     "A Chat participant did not come across",
     "A bot or app (e.g. Drive notifications) cannot be added to an imported space; any other "
     "difference is a member's role or state.",
     "None possible for a bot. A missing person is added by the missing-member repair when one exists; listed otherwise.", False),
    ("chat.sender", "chat", lambda t: "Original Chat sender identity was not reproduced" in t,
     "A Chat message's sender is the migrating user",
     "Same cause as an attributed message: Chat lets nobody post as a bot or an account that is "
     "not on the target, so the thread check sees the user as sender.",
     "None possible through the Chat API; the text carries \"[originally from ...]\".", False),
    ("chat.text", "chat", lambda t: "Chat text differs" in t,
     "Chat message text differs",
     "A real text difference (link rewrite or edit).",
     "Inspect; re-post if needed.", False),
    ("tasks.untitled", "tasks", lambda t: "'(untitled)'" in t,
     "Untitled task got the title \"(untitled)\"",
     "An old engine rule; Google accepts an empty title (measured 2026-10-23).",
     "Patch the title back to empty (finding_repairs.repair_tasks); the engine keeps it empty now.", True),
    ("drive.sheets_scope", "drive", lambda t: "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in t or "insufficient authen" in t,
     "A Sheet could not be read for comparison",
     "The checker used a Sheets method Google allows only with write access; the source grant is read-only.",
     "Checker reads with spreadsheets.get + ranges (native_repair.read_sheet, 2026-10-23); rechecked.", True),
    ("drive.embedded_images", "drive", lambda t: "Embedded image bytes" in t,
     "A Doc/Slide with images could not be compared",
     "Image URLs are signed per read, so they never match.",
     "Checker compares each image by the hash of its bytes (native_fidelity._hash_images, 2026-10-23).", True),
    ("drive.form_relinked", "drive", lambda t: "copied Form is relinked" in t,
     "A responses Sheet has one tab more than the source",
     "Linking a copied Form to its copied responses Sheet makes Google add an empty "
     "\"Form Responses\" tab; no API links a Form, and no link adds none.",
     "None: the operator chose a linked Form over an identical Sheet (2026-10-24). Done by "
     "form_relink.py as the target admin, in repair's \"form links\" step.", False),
    ("drive.native_structure", "drive", lambda t: ("native document content, formatting or structure differs" in t
                                                 or "native Sheet entered values, formatting, or structures differ" in t),
     "Doc/Slide structure differs from the source",
     "A read-only source forces download+upload: Google rebuilds the document's internals "
     "(style/list ids, soft breaks). Text is the same.",
     "None automatic. Exact copies need server-side copy, i.e. Drive write on the source grant "
     "(an operator's decision). Listed in the final report.", False),
    ("drive.content_export", "drive", lambda t: "content differs when exported" in t,
     "Exported content differs",
     "Rebuild differences or a later edit.",
     "Content re-applied on the target where supported (finding_repairs.repair_drive).", True),
    ("drive.shortcut_target", "drive", lambda t: "shortcut target differs" in t,
     "Shortcut points at a different copy",
     "One file shared with many users has a copy per user; the checker compared another user's copy.",
     "Checker accepts any genuine copy (verification_coverage, 2026-10-23).", True),
    ("drive.item_absent", "drive", lambda t: "absent from target inventory" in t,
     "A mapped item is not on the target",
     "Live: a shortcut to a colleague's file, skipped when this user's own copy did not exist.",
     "Engine resolves to any copy of the target file; repair re-walks the user's Drive "
     "(repair.unresolved_shortcut_users -> retry_drive_stragglers).", True),
    ("drive.mtime", "drive", lambda t: "modified time not preserved" in t,
     "Modified time differs",
     "A later write on the target moved it, or the source was edited after the copy.",
     "Put back on the target (repair.fix_modified_times) -- unless the source changed after the "
     "copy: then the delta brings the edit (editedOnSource).", True),
    ("drive.content_changed", "drive", lambda t: "MD5 differs" in t or "size differs" in t,
     "File bytes differ",
     "Edited on the source after it was copied (live: an xlsx edited 10-21, copied 10-17).",
     "The delta (Mirror) copies the new version.", True),
    ("drive.group_grant", "drive", lambda t: "grant missing on the target: ['group'" in t,
     "A group's share did not come across",
     "The group does not exist on the target. Classroom-made groups are hidden from the "
     "Directory API, so their members cannot even be read.",
     "None automatic: create the group on the target or share with its people by hand.", False),
    ("drive.grant_missing", "drive", lambda t: "grant missing on the target" in t,
     "A share did not come across",
     "A grant refused or skipped during the run.",
     "Re-apply the grant on the target (finding_repairs._repair_grants).", True),
    # The rest of dharan's 438 'unclassified' Drive findings (2026-10-24).
    ("drive.unmapped_source", "drive", lambda t: "UNMAPPED_SOURCE" in t,
     "A source file has no copy",
     "Live: a personal account's file inside the user's own folder -- the walk listed only the "
     "user's own files, and shared-with-me shows only direct shares. (Bitport's own copies and "
     "reasoned skips used to be counted here too; the checker no longer does.)",
     "The walk copies such files in place (drive_engine._sync_outsiders_here); repair re-walks "
     "the user's Drive.", True),
    ("drive.office_compared_as_native", "drive", lambda t: "must not be an Office file" in t,
     "An Office file could not be compared",
     "Word's mimeType ends in '.document' and PowerPoint's in '.presentation': the checker sent "
     "them to the Docs/Slides API.",
     "Checker compares Office files by their bytes (verify_sample, 2026-10-24); rechecked.", True),
    ("drive.read_refused", "drive", lambda t: "RESOURCE_EXHAUSTED" in t or "HTTP 429" in t
                                             or "HTTP 500" in t or "(INTERNAL)" in t,
     "Google refused or failed a read during the check",
     "Sheets allows 60 reads a minute per user; the Docs API sometimes answers 500.",
     "Nothing on either tenant is wrong: the next check reads it again.", True),
    ("drive.source_changed", "drive", lambda t: "Source changed during migration" in t,
     "The source changed while the user was copied",
     "Someone at the source edited the file during the run (the tripwire saw it).",
     "The delta (Mirror) copies the new version.", True),
    ("drive.duplicate", "drive", lambda t: "duplicateOf" in t,
     "Two copies of one file on the target",
     "A copy landed twice (an earlier interrupted run).",
     "None automatic: deleting is the operator's call. Listed with both ids.", False),
    ("drive.comments", "drive", lambda t: "comment resolution differs" in t or "comment anchor differs" in t
                                         or "comment replies differ" in t,
     "Comments differ",
     "Drive cannot write a comment as its author, mark one resolved on create, or anchor it "
     "to the same text in a rebuilt Office file.",
     "None automatic beyond what the copy carried. Listed in the final report.", False),
]
BY_ID = {i[0]: i for i in ISSUES}


def classify(service: str, item) -> str:
    """The kind of one finding, from its own words; 'unclassified' when none fits."""
    text = json.dumps(item, default=str) if not isinstance(item, str) else item
    for issue_id, svc, matches, *_ in ISSUES:
        if svc == service and matches(text):
            return issue_id
    return "unclassified"


def ensure(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS issue_fix_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, fixed_at TEXT NOT NULL, source_user TEXT NOT NULL,
        service TEXT NOT NULL, issue TEXT NOT NULL, item_id TEXT, how TEXT NOT NULL)""")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(verification_repair_queue)")}
    if cols and "issue" not in cols:
        conn.execute("ALTER TABLE verification_repair_queue ADD COLUMN issue TEXT")
    if cols:
        # Rows queued before kinds existed, or before their kind was known: classified
        # from their own stored detail (a kind added later names rows already open).
        for r in conn.execute("SELECT rowid, service, category, detail FROM verification_repair_queue "
                              "WHERE issue IS NULL OR issue='unclassified'").fetchall():
            try:
                item = json.loads(r[3])
            except ValueError:
                item = r[3]
            conn.execute("UPDATE verification_repair_queue SET issue=? WHERE rowid=?",
                         (classify(r[1], item if r[2] != "notCopied" else {"notCopied": item}), r[0]))


def log_resolved(conn, rows, now: str) -> None:
    """One line per finding a fresh check no longer finds: how its kind is fixed."""
    for r in rows:
        issue = r["issue"] or "unclassified"
        how = BY_ID[issue][5] if issue in BY_ID else "No longer found by a fresh, complete check."
        conn.execute("INSERT INTO issue_fix_log(fixed_at,source_user,service,issue,item_id,how) "
                     "VALUES(?,?,?,?,?,?)", (now, r["source_user"], r["service"], issue, r["item_id"], how))


def summary_empty() -> list[dict]:
    return [{"id": i, "service": svc, "title": title, "cause": cause, "fix": fix, "auto": auto,
             "open": 0, "fixed": 0, "lastFixed": None} for i, svc, _m, title, cause, fix, auto in ISSUES]


def _open_rows(conn) -> list[tuple[str, str, str]]:
    """(kind, category, source_user) for every open finding. A ledger whose queue
    predates kinds (the column is added on the next write) is classified here, from
    each row's stored detail, rather than read as having nothing open."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "verification_repair_queue" not in tables:
        return []
    cols = {r[1] for r in conn.execute("PRAGMA table_info(verification_repair_queue)")}
    pick = "issue" if "issue" in cols else "NULL"
    out = []
    for issue, service, category, detail, user in conn.execute(
            f"SELECT {pick}, service, category, detail, source_user FROM verification_repair_queue "
            "WHERE state!='RESOLVED'"):
        if not issue or issue == "unclassified":
            try:
                item = json.loads(detail)
            except ValueError:
                item = detail
            issue = classify(service, item if category != "notCopied" else {"notCopied": item})
        out.append((issue, category, user))
    return out


def user_reason(conn, source_user: str, limit: int = 4) -> str:
    """One user's open findings by kind, most first -- what a failed acceptance says
    instead of "Selected scope has unresolved ... findings" (live, 6 dharan users read
    only that; their findings were Doc/Slide rebuilds and files not copied yet)."""
    counts: dict[str, int] = {}
    for issue, _category, user in _open_rows(conn):
        if user == source_user:
            counts[issue] = counts.get(issue, 0) + 1
    if not counts:
        return ""
    parts = [f"{n} {BY_ID[k][3] if k in BY_ID else 'not a known kind yet'}"
             for k, n in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]]
    more = len(counts) - limit
    return "; ".join(parts) + (f"; and {more} more kind(s)" if more > 0 else "")


def summary(conn) -> dict:
    """Every known kind with its counts (open now, by category; fixed so far; last
    fixed), plus the latest fixes -- for the One-to-one page and the pair card."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    open_, by_cat, fixed, last, recent = {}, {}, {}, {}, []
    for issue, category, _user in _open_rows(conn):
        open_[issue] = open_.get(issue, 0) + 1
        by_cat.setdefault(issue, {})[category] = by_cat.setdefault(issue, {}).get(category, 0) + 1
    if "issue_fix_log" in tables:
        for issue, n, at in conn.execute("SELECT issue, COUNT(*), MAX(fixed_at) FROM issue_fix_log GROUP BY 1"):
            fixed[issue], last[issue] = n, at
        recent = [dict(zip(("fixedAt", "user", "service", "issue", "item"), r)) for r in conn.execute(
            "SELECT fixed_at,source_user,service,issue,item_id FROM issue_fix_log ORDER BY id DESC LIMIT 50")]
    kinds = [{"id": i, "service": svc, "title": title, "cause": cause, "fix": fix, "auto": auto,
              "open": open_.get(i, 0), "openByCategory": by_cat.get(i, {}),
              "fixed": fixed.get(i, 0), "lastFixed": last.get(i)}
             for i, svc, _m, title, cause, fix, auto in ISSUES]
    if open_.get("unclassified") or fixed.get("unclassified"):
        kinds.append({"id": "unclassified", "service": "", "title": "Not a known kind yet",
                      "cause": "", "fix": "Read the finding; add its kind here once understood.", "auto": False,
                      "open": open_.get("unclassified", 0), "openByCategory": by_cat.get("unclassified", {}),
                      "fixed": fixed.get("unclassified", 0),
                      "lastFixed": last.get("unclassified")})
    return {"kinds": kinds, "recent": recent}


def users_for(conn, issue: str) -> list[dict]:
    """Who one kind affects: open findings per user, and fixes recorded for them."""
    per: dict[str, dict] = {}
    for kind, _cat, user in _open_rows(conn):
        if kind == issue:
            per.setdefault(user, {"user": user, "open": 0, "fixed": 0, "lastFixed": None})["open"] += 1
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='issue_fix_log'").fetchone():
        for user, n, at in conn.execute("SELECT source_user, COUNT(*), MAX(fixed_at) FROM issue_fix_log "
                                        "WHERE issue=? GROUP BY 1", (issue,)):
            row = per.setdefault(user, {"user": user, "open": 0, "fixed": 0, "lastFixed": None})
            row["fixed"], row["lastFixed"] = n, at
    return sorted(per.values(), key=lambda r: (-(r["open"] + r["fixed"]), r["user"]))


# ---------------------------------------------------------------------------
# Run failures: what a migration itself could not do (FAILED rows in audit_log), named the
# same way the one-to-one findings above are. Each run that finds a new cause adds one here,
# with a fix -- automatic where it can be -- so the next run handles it on its own. Taken
# from dharan.gov.np -> s. (2026-10-25), where all 194 failures read "not yet classified".
# id, item types (None = any), matches(error text), title, cause, fix, auto
RUN_KINDS = [
    ("run.resource_key", ("file", "shortcut", "acl", "comment"),
     lambda t: "File not found" in t,
     "Source file read 'not found' (resource key)",
     "An older file shared by link, reached through a shared folder, opens by id only with its "
     "resource key; without it Google answers 404.",
     "Keys are read from listings, kept in the ledger and sent on every read (resource_keys.py); "
     "repair re-walks the file and re-reads its shares and comments.", True),
    ("run.disk_full", None,
     lambda t: "No space left on device" in t or "disk is full" in t,
     "This server's disk was full",
     "Download-and-upload stages each file on the server; failed downloads left their files "
     "behind until the disk filled.",
     "Files of 256 MB and over are streamed straight into the upload, never staged "
     "(drive_engine.STREAM_ABOVE_BYTES); failed downloads remove their file; scratch is swept "
     "every 15 minutes (housekeeping.py); repair re-walks what failed.", True),
    ("run.db_rollback", None,
     lambda t: "cannot rollback - no transaction is active" in t,
     "A ledger write failed (seen as a rollback error)",
     "A full disk failed the write; the rollback after it raised and hid the real error.",
     "The real error is kept now (db.write); repair re-walks what failed.", True),
    ("run.malware_flag", ("file",),
     lambda t: "cannotDownloadAbusiveFile" in t,
     "Google flagged the file as malware or spam",
     "Google refuses to let it be downloaded, even with the acknowledgement flag.",
     "None by download: only a server-side copy carries it (pending the copy safety test).", False),
    ("run.form_rebuild", ("file",),
     lambda t: "forms.googleapis.com" in t and "INVALID_ARGUMENT" in t,
     "A Form could not be rebuilt",
     "An image's address is the source's own, which the target's Forms service cannot fetch "
     "('Failed to fetch image from source_uri'); the batch is all-or-nothing, so nothing landed.",
     "The rebuild retries without the images it cannot fetch and names them as left out "
     "(native_api._without_images); the next run rebuilds the Form.", True),
    ("run.comment_missing", ("comment",),
     lambda t: "Mapped comment/reply is missing" in t,
     "A copied comment is missing on the target",
     "Comment ids are unique within one file only: two copies of one Doc shared an id, and the "
     "second file found the first file's mapping.",
     "Comment mappings are keyed by file (migration_comments.reconcile); the next run copies "
     "the second file's comment.", True),
    ("run.held_for_drive", None,
     lambda t: "held: this user's Drive did not migrate" in t,
     "Mail or calendar held back for Drive",
     "Their links would keep pointing at the source while the user's Drive is unfinished.",
     "Runs on its own once the user's Drive succeeds (the next run).", True),
]
RUN_BY_ID = {k[0]: k for k in RUN_KINDS}


def classify_failure(item_type: str, text: str) -> str:
    for kind, types, matches, *_ in RUN_KINDS:
        if (types is None or item_type in types) and matches(text or ""):
            return kind
    return "unclassified"


def run_failure_summary(conn) -> dict:
    """Run failures by kind: open (audit_log still FAILED) and fixed (item_retries: a FAILED
    row later replaced by anything else, and still not failed). Corpus-scoped like
    repair.survey."""
    scope = ("(NOT EXISTS (SELECT 1 FROM identity_map) OR EXISTS (SELECT 1 FROM identity_map m "
             "WHERE m.source_email={t}.source_user))")
    kinds = {k[0]: {"id": k[0], "title": k[3], "cause": k[4], "fix": k[5], "auto": k[6],
                    "open": 0, "fixed": 0} for k in RUN_KINDS}
    kinds["unclassified"] = {"id": "unclassified", "title": "Not a known kind yet", "cause": "",
                             "fix": "Read, named and given a fix after this run.", "auto": False,
                             "open": 0, "fixed": 0}
    for item_type, text in conn.execute(
            "SELECT a.item_type, a.error_message FROM audit_log a WHERE a.status='FAILED' AND "
            + scope.format(t="a")).fetchall():
        kinds[classify_failure(item_type, text or "")]["open"] += 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "item_retries" in tables:
        for item_type, text in conn.execute(
                "SELECT r.item_type, r.prev_error FROM item_retries r WHERE r.id=(SELECT MAX(id) "
                "FROM item_retries x WHERE x.source_user=r.source_user AND x.item_id=r.item_id "
                "AND x.item_type=r.item_type) AND r.new_status!='FAILED' AND NOT EXISTS (SELECT 1 "
                "FROM audit_log a WHERE a.source_user=r.source_user AND a.item_id=r.item_id AND "
                "a.item_type=r.item_type AND a.status='FAILED') AND " + scope.format(t="r")).fetchall():
            kinds[classify_failure(item_type, text or "")]["fixed"] += 1
    return {"kinds": [v for v in kinds.values() if v["open"] or v["fixed"]],
            "open": sum(v["open"] for v in kinds.values()),
            "unclassified": kinds["unclassified"]["open"]}
