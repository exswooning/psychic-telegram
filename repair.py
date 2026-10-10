"""Find the failure families a run leaves behind, and fix the fixable ones.

A finished migration's failure count is not one number. On a live 201-user
run it was 119,600, and the three causes behind it needed three different
responses:

  55,807  ACL grants refused because the grantee "has no Google account" --
          recorded during a 21-minute window when the target accounts had
          been deleted. The accounts exist now. Dead records describing a
          state that no longer holds.
  27,597  ACL grants refused for quota. Precedent says most landed and only
          the response was throttled: a previous reconcile resolved 124,303
          of 127,852 as already present on the target.
  32,967  Gmail messages rejected as "Invalid label", because label_map
          pointed at label ids in a mailbox that had been recreated.

Only the middle one needs the network. The first is answerable from the
ledger and the accounts, and the third is repaired at its source in
gmail_engine.sync_labels() -- listed here so a report names it rather than
leaving 33,000 failures looking permanent.

Nothing here deletes an audit row. A resolved failure is marked resolved,
which keeps the record of what happened and why it stopped mattering.
"""
from __future__ import annotations

import copy
import logging

log = logging.getLogger("repair")

# The grantee-missing 400 is quoted verbatim by Drive. Matched on the stable
# fragment only: Drive writes "no Google accountS ... these email addressES"
# for several grantees and "no Google account ... this email address" for
# one, and a pattern written from a single observed message caught the
# plural alone -- leaving 2,900 rows of the same cause in "unclassified",
# where they read as an unknown problem rather than a known one.
NO_ACCOUNT = "no Google account"
QUOTA = "Quota exceeded"
INVALID_LABEL = "Invalid label"
# "Request had insufficient authentication scopes" on a copy. The token is
# short a scope for a moment after a delegation change, not permanently: 77
# files failed this way on one run while the identical copy succeeded on the
# next. Retried on its own budget (resilience.SCOPE_RETRY_BUDGET) rather than
# the standard ladder, which gave up after six attempts.
SCOPE_403 = "insufficient authentication scopes"
# "Active session is invalid" -- the impersonation session, not the item.
SESSION_INVALID = "Active session is invalid"
# Drive-level roles applied as a per-file ACL. Permanent and structural, not
# a stumble: organizer/fileOrganizer only exist on shared drives, so the
# grant can never succeed on a My Drive file. drive_engine skips these now,
# which means every surviving row is history from before that guard. They
# were the whole of this tenant's failure list and matched no family, so 32
# failures read as 32 unknown problems.
ORGANIZER_ROLE = "organizerOnNonTeamDriveNotSupported"
# Google's own backend, briefly. "Authentication backend unavailable" behind
# a 503 is the one thing in this list a retry actually fixes.
BACKEND_5XX = "backendError"

# Whether re-running could change the outcome. The point of the split is
# that "32 failures" and "1 worth retrying, 31 that cannot move" are very
# different instructions, and only the second one is actionable.
RETRYABLE_FAMILIES = {"acl_quota", "drive_scope_403", "auth_session_invalid",
                      "gmail_invalid_label", "transient_backend", "drive_stragglers",
                      "shared_drives"}

# A shared drive whose create failed and that still has no mapping -- the whole
# drive (members and contents) is missing on the target. Tenant-level, so NOT
# corpus-scoped by user the way survey()'s n() is: the row belongs to the admin.
# Live: SEEDED-SD-10's create hit a 409 "requestId has already been used" on a
# transport resend, no drive resulted, and nothing ever tried it again.
_UNMAPPED_FAILED_SHARED_DRIVES = (
    "SELECT DISTINCT a.item_id FROM audit_log a WHERE a.status='FAILED' "
    "AND a.item_type='shared_drive' AND NOT EXISTS (SELECT 1 FROM id_mapping m "
    "WHERE m.source_id = a.item_id AND m.type = 'shared_drive')")

# A file/folder/shortcut that permanently failed its copy -- not 'acl' (a grant
# failure, handled separately: the file and its mapping already exist) and not
# 'user' (the whole user errored, which demote_false_done/resolve_users handle).
DRIVE_STRAGGLER_TYPES = ("file", "folder", "shortcut")


def survey(db) -> dict:
    """Count the failure families without touching the network."""
    # Corpus-scoped: audit_log keeps a previous run's FAILED rows for users
    # deleted in a reseed, and counting them made a clean run's survey read
    # "4 failures, 2 impersonation-session-invalid" against people who no
    # longer exist. Only rows whose user is still in identity_map count --
    # UNLESS there is no corpus at all (a bare ledger), where scoping to an
    # empty set would hide every failure, so all of them count instead.
    def n(where: str, params=()) -> int:
        return db.conn.execute(
            f"SELECT COUNT(*) c FROM audit_log a WHERE a.status='FAILED' AND {where} "
            "AND (NOT EXISTS (SELECT 1 FROM identity_map) "
            "     OR EXISTS (SELECT 1 FROM identity_map m "
            "                WHERE m.source_email = a.source_user))",
            params).fetchone()["c"]

    return {
        "total": n("1=1"),
        "false_done": db.conn.execute(
            """SELECT COUNT(*) c FROM identity_map i
                WHERE i.status = 'DONE'
                  AND NOT EXISTS (SELECT 1 FROM id_mapping m
                                   WHERE m.source_user = i.source_email)
                  AND EXISTS (SELECT 1 FROM audit_log a
                               WHERE a.source_user = i.source_email
                                 AND a.status = 'FAILED')""").fetchone()["c"],
        "user_stale": len(stale_user_failures(db)),
        "shared_drives": len(db.conn.execute(_UNMAPPED_FAILED_SHARED_DRIVES).fetchall()),
        "acl_no_account": n("item_type='acl' AND error_message LIKE ?",
                            (f"%{NO_ACCOUNT}%",)),
        "acl_quota": n("item_type='acl' AND error_message LIKE ?",
                       (f"%{QUOTA}%",)),
        "gmail_invalid_label": n("item_type='message' AND error_message LIKE ?",
                                 (f"%{INVALID_LABEL}%",)),
        "drive_scope_403": n("item_type='file' AND error_message LIKE ?",
                             (f"%{SCOPE_403}%",)),
        "auth_session_invalid": n("error_message LIKE ?",
                                  (f"%{SESSION_INVALID}%",)),
        "acl_organizer_role": n("error_message LIKE ?", (f"%{ORGANIZER_ROLE}%",)),
        "transient_backend": n("error_message LIKE ?", (f"%{BACKEND_5XX}%",)),
        # NOT the two patterns above: those are file-type failures too, and already
        # counted under their own, more specific family -- counting them again here
        # would inflate triage()'s "retryable" total by double-counting the same rows.
        "drive_stragglers": n(
            f"item_type IN ({','.join('?' for _ in DRIVE_STRAGGLER_TYPES)}) "
            "AND error_message NOT LIKE ? AND error_message NOT LIKE ?",
            (*DRIVE_STRAGGLER_TYPES, f"%{SCOPE_403}%", f"%{BACKEND_5XX}%")),
    }


def triage(db) -> dict:
    """The survey split into what a retry could fix and what it could not.

    A failures page listing 32 rows of equal weight is how the one that
    matters hides behind thirty-one that cannot move. On this tenant that is
    literally the shape: 29 organizer-role grants that can never succeed, and
    a single 503 from Google's auth backend that a retry clears.
    """
    s = survey(db)
    counts = {k: v for k, v in s.items()
              if k not in ("total", "false_done", "user_stale")}
    retryable = sum(v for k, v in counts.items() if k in RETRYABLE_FAMILIES)
    permanent = sum(v for k, v in counts.items() if k not in RETRYABLE_FAMILIES)
    return {
        "total": s["total"],
        "retryable": retryable,
        "permanent": permanent,
        # Named rather than folded into "other": an unclassified failure is a
        # family nobody has looked at yet, which is a different thing from a
        # known-permanent one and must not be counted as either.
        "unclassified": max(0, s["total"] - retryable - permanent),
        "families": {k: v for k, v in counts.items() if v},
    }


def stale_grantee_failures(db, directory=None) -> list:
    """ACL failures blaming a grantee that has an account now.

    Verified against the directory when one is given, because "the mailbox
    exists today" is the entire claim being made. Without it the rows are
    left alone: guessing that a failure is obsolete is how a real one gets
    hidden.
    """
    rows = db.conn.execute(
        "SELECT source_user, item_id, timestamp FROM audit_log "
        "WHERE status='FAILED' AND item_type='acl' AND error_message LIKE ?",
        (f"%{NO_ACCOUNT}%",)).fetchall()
    if directory is None:
        return []
    # The grantee is the half after the colon in the audit key.
    seen: dict = {}
    out = []
    for r in rows:
        grantee = r["item_id"].split(":", 1)[1] if ":" in r["item_id"] else ""
        if not grantee:
            continue
        if grantee not in seen:
            try:
                directory.users().get(userKey=grantee,
                                      fields="primaryEmail").execute()
                seen[grantee] = True
            except Exception:      # noqa: BLE001
                # ONLY a successful lookup counts as "this account exists".
                #
                # The first version treated anything that was not a 404 as
                # confirmation, which turned a 403 -- an address this admin
                # may not query -- into "the account is back", and would have
                # marked a real, current failure resolved on the strength of
                # a permission error. Live, the directory pass emitted a 403.
                # Not knowing and knowing-it-exists must not lead to the same
                # place.
                seen[grantee] = False
        if seen[grantee]:
            out.append(r)
    return out


def reapply_owed_grants(auth, db, settings, apply: bool = False) -> dict:
    """Grant the shares that were owed to a colleague with no target account, now
    that the account exists.

    A run for a few users shares their files with colleagues who are not on the
    target yet (live: fiona's and seeduser160's files, shared with 67 colleagues
    Delete users had removed). Nothing put those back when the colleagues were
    migrated later. Each one is granted through the engine's own _sync_acls, limited
    to the owed keys, so the role, expiry and inheritance are translated exactly as
    the migration translates them. Older runs recorded these as
    SKIPPED_GRANTEE_NOT_ON_GOOGLE; a colleague's are taken too. Only a successful
    directory lookup counts as "the account exists". Never raises.
    """
    from config import OWED_GRANT
    from drive_engine import DriveMigrator, _in_target_domain
    from resilience import DailyQuotaGuard

    out = {"owed": 0, "ready": 0, "granted": 0, "errors": []}
    by_user: dict[str, dict[str, set[str]]] = {}
    for r in db.conn.execute(
            "SELECT source_user, item_id FROM audit_log WHERE item_type='acl' AND status IN (?,?)",
            (OWED_GRANT, "SKIPPED_GRANTEE_NOT_ON_GOOGLE")):
        sid, _, grantee = r["item_id"].partition(":")
        if "@" not in grantee or not _in_target_domain(grantee, settings):
            continue                       # an outsider: no directory to ask
        out["owed"] += 1
        by_user.setdefault(r["source_user"], {}).setdefault(sid, set()).add(r["item_id"])
    delegates = db.conn.execute(
        "SELECT source_user, item_id FROM audit_log WHERE item_type='delegate' AND status=?",
        (OWED_GRANT,)).fetchall()
    out["owed"] += len(delegates)
    if not by_user and not delegates:
        return out
    try:
        directory = auth.directory("target")
    except Exception as exc:      # noqa: BLE001
        out["errors"].append(f"directory: {str(exc)[:160]}")
        return out
    exists: dict[str, bool] = {}

    def has_account(email: str) -> bool:
        if email not in exists:
            try:
                directory.users().get(userKey=email, fields="primaryEmail").execute()
                exists[email] = True
            except Exception:      # noqa: BLE001 - not found, or could not ask
                exists[email] = False
        return exists[email]

    migrators: dict[str, DriveMigrator] = {}
    attempted: list[tuple[str, str, str, set[str]]] = []
    for user, files in by_user.items():
        for sid, keys in files.items():
            ready = {k for k in keys if has_account(k.partition(":")[2])}
            out["ready"] += len(ready)
            target_id = db.target_for_source_id(sid) if ready else None
            if not apply or not target_id:
                continue
            try:
                if user not in migrators:
                    target_user = db.resolve_identity(user) or user
                    migrators[user] = DriveMigrator(auth, db, settings, user, target_user,
                                                    DailyQuotaGuard(db, target_user,
                                                                    settings.effective_upload_cap()))
                out["granted"] += migrators[user].reapply_acls(sid, target_id, only=ready)
                attempted.append((user, sid, target_id, ready))
            except Exception as exc:      # noqa: BLE001
                out["errors"].append(f"{user} {sid}: {str(exc)[:160]}")
    # Second pass, after every folder above has had its grants: a share the file
    # inherits from its folder is never granted on the file itself (the engine leaves
    # it to the folder), so its row stayed "owed" forever. Live: 27,633 such rows
    # after a repair granted the folders -- 30 of 30 sampled already had access.
    out["covered"] = 0
    for user, sid, target_id, ready in attempted:
        still = [k for k in ready
                 if (lambda row: row is not None and row["status"] == OWED_GRANT)(
                     db.get_audit(user, k, "acl"))]
        if not still:
            continue
        try:
            dm = migrators[user]
            perms = dm._retry(lambda t=target_id: dm.tgt.permissions().list(
                fileId=t, supportsAllDrives=True,
                fields="permissions(emailAddress)").execute()).get("permissions", [])
        except Exception as exc:      # noqa: BLE001
            out["errors"].append(f"{user} {sid}: {str(exc)[:160]}")
            continue
        have = {(x.get("emailAddress") or "").lower() for x in perms}
        for k in still:
            if k.partition(":")[2].lower() in have:
                db.log_audit(user, k, "acl", "SUCCESS",
                             "has access on the target -- inherited from its folder")
                out["covered"] += 1
    # A mail delegate owed the same way: item_id is the delegate's target address.
    for r in delegates:
        if not has_account(r["item_id"]):
            continue
        out["ready"] += 1
        if not apply:
            continue
        try:
            owner = db.resolve_identity(r["source_user"]) or r["source_user"]
            auth.target_gmail(owner).users().settings().delegates().create(
                userId="me", body={"delegateEmail": r["item_id"]}).execute()
            db.log_audit(r["source_user"], r["item_id"], "delegate", "SUCCESS")
            out["granted"] += 1
        except Exception as exc:      # noqa: BLE001 - stays owed for the next repair
            out["errors"].append(f"{r['source_user']} delegate {r['item_id']}: {str(exc)[:160]}")
    return out


def stranded_drive_users(db) -> list[str]:
    """Source users with a file/folder/shortcut that permanently failed its copy,
    scoped to the current corpus and excluding the two families that already have
    their own repair path (scope-403 and the transient backend error) -- same
    exclusion as the `drive_stragglers` count in survey(), so the two never disagree
    about how many there are.
    """
    marks = ",".join("?" for _ in DRIVE_STRAGGLER_TYPES)
    rows = db.conn.execute(
        f"""SELECT DISTINCT source_user FROM audit_log a
             WHERE status='FAILED' AND item_type IN ({marks})
               AND error_message NOT LIKE ? AND error_message NOT LIKE ?
               AND EXISTS (SELECT 1 FROM identity_map m
                            WHERE m.source_email = a.source_user)""",
        (*DRIVE_STRAGGLER_TYPES, f"%{SCOPE_403}%", f"%{BACKEND_5XX}%")).fetchall()
    return [r["source_user"] for r in rows]


def retry_drive_stragglers(auth, db, settings, apply: bool = False,
                          users: list[str] | None = None, _migrate_user=None) -> dict:
    """Re-run just Drive for users with a stranded file/folder/shortcut.

    Unlike Gmail and Calendar (retry_failed.py's whole reason for existing), Drive
    lists every file on every run and skips anything already mapped -- drive_engine's
    own get_target_id check, at the top of both the folder and file branches of the
    walk. So a plain re-run of Drive for just the affected users retries exactly what
    is still missing, at the cost of walking their tree again to find it; no bespoke
    per-item re-fetch needed, unlike the message/event case.

    Deliberately reuses main.migrate_user (services={'drive'}) rather than
    constructing DriveMigrator directly, so this reads and writes the SAME
    identity_map status the ordered pass loop itself depends on. That is only
    correct because every caller here runs it at a PASS BOUNDARY (see
    main._repair_between_passes and run_all below) -- after the previous pass's
    own worker pool has fully drained and before the next one starts, never
    concurrently with another pass touching the same user.
    """
    from main import migrate_user as _default_migrate_user
    migrate_user = _migrate_user or _default_migrate_user
    targets = dict(db.conn.execute(
        "SELECT source_email, target_email FROM identity_map").fetchall())
    stats = {"attempted": 0, "retried": 0, "unmapped_users": 0, "errors": []}
    for src in (users if users is not None else stranded_drive_users(db)):
        tgt = targets.get(src)
        if not tgt:
            # No target account: nothing for a re-walk to write into.
            stats["unmapped_users"] += 1
            continue
        stats["attempted"] += 1
        if not apply:
            continue
        st = settings
        if settings is not None and settings.transfer_mode != "move" and db.conn.execute(
                "SELECT 1 FROM id_mapping WHERE source_user=? AND type='file' "
                "AND source_id = target_id LIMIT 1", (src,)).fetchone():
            # Its files were MOVED: what is left goes the same way, never copied instead
            # (the end-of-run repair runs in the API's own mode, a copy).
            st = copy.copy(settings)
            st.transfer_mode = "move"
        try:
            result = migrate_user(auth, db, st, src, tgt, {"drive"},
                                  delta=False, delta_days=0)
            if result.get("status") not in ("FAILED", "BLOCKED"):
                stats["retried"] += 1
        except Exception as exc:      # noqa: BLE001 - report, never abort the caller
            stats["errors"].append(f"{src} drive: {str(exc)[:120]}")
    return stats


def broken_folder_grants(db, since: str | None = None) -> dict:
    """Folder shares that failed, and how many files sit behind them.

    Once inherited grants are folder-derived, a folder's share is the ONLY
    thing granting access to everything inside it. A failed folder grant
    therefore takes every file in that folder with it -- where the old
    per-file recreation would have left each file holding its own copy.

    That makes a failed folder grant categorically more serious than a
    failed file grant, and the raw failure count says nothing about the
    difference: live, 265 folder-grant failures across 147 folders sat in
    the same total as 142 file-grant failures, while accounting for 1,050
    inaccessible files against those files' own 142.

    Blast radius is counted from direct children only. A folder tree could
    be walked transitively, but parent_target_id gives the direct answer
    cheaply and understates rather than overstates -- which is the right
    direction for a number that decides how alarmed to be.

    `since` scopes this to failures the CURRENT run produced, and matters
    more than it looks. audit_log survives a wipe on purpose, so old FAILED
    rows persist; as a re-run re-creates folders, those stale rows start
    finding a matching mapping and the count climbs on its own. Live it went
    1 -> 9 -> 98 with a "952 files at risk" warning attached, and every
    folder sampled turned out to HOLD the grant it was reported as missing.
    They are satisfied by inheritance from a parent, so nothing wrote an
    explicit SUCCESS row to overwrite the old failure.

    Without `since` this still reports everything, which is what a
    post-run repair wants; the live dashboard passes the run start so it
    warns about what this run actually broke.
    """
    where = ""
    params: list = []
    if since:
        where = " AND a.timestamp >= ?"
        params = [since]
    folders = [r["src"] for r in db.conn.execute(
        f"""SELECT DISTINCT substr(a.item_id, 1, instr(a.item_id, ':') - 1) AS src
             FROM audit_log a
            WHERE a.item_type = 'acl' AND a.status = 'FAILED'
              AND instr(a.item_id, ':') > 0{where}
              AND EXISTS (SELECT 1 FROM id_mapping m
                           WHERE m.source_user = a.source_user
                             AND m.source_id = substr(a.item_id, 1,
                                                      instr(a.item_id, ':') - 1)
                             AND m.type = 'folder')""", params) if r["src"]]
    out = {"folders": len(folders), "grants": 0, "files_behind": 0}
    if not folders:
        return out
    marks = ",".join("?" * len(folders))
    out["grants"] = db.conn.execute(
        f"""SELECT COUNT(*) c FROM audit_log
             WHERE item_type='acl' AND status='FAILED'
               AND substr(item_id, 1, instr(item_id, ':') - 1) IN ({marks})""",
        folders).fetchone()["c"]
    targets = [r["target_id"] for r in db.conn.execute(
        f"SELECT target_id FROM id_mapping WHERE type='folder' "
        f"AND source_id IN ({marks})", folders)]
    if targets:
        m2 = ",".join("?" * len(targets))
        out["files_behind"] = db.conn.execute(
            f"SELECT COUNT(*) c FROM id_mapping WHERE type='file' "
            f"AND parent_target_id IN ({m2})", targets).fetchone()["c"]
    return out


def false_done_users(db) -> list:
    """Users marked DONE that migrated nothing and recorded failures.

    "Done" has to mean the work happened. Live, seeduser382 finished with
    zero id_mapping rows, zero SUCCESS rows and one HTTP 401 -- and the
    report read "201 done, 0 users failed". A user whose every attempt
    failed was being counted as a success, in the one number an operator
    trusts to decide a migration is finished.

    Both conditions are required. A genuinely empty mailbox migrates nothing
    and that is a correct DONE; what makes this wrong is nothing migrated
    AND something failed.
    """
    return db.conn.execute(
        """SELECT i.source_email, i.target_email FROM identity_map i
            WHERE i.status = 'DONE'
              AND NOT EXISTS (SELECT 1 FROM id_mapping m
                               WHERE m.source_user = i.source_email)
              AND EXISTS (SELECT 1 FROM audit_log a
                           WHERE a.source_user = i.source_email
                             AND a.status = 'FAILED')""").fetchall()


def demote_false_done(db, rows, dry_run: bool = True) -> int:
    """Put them back to FAILED, carrying the reason that actually stopped them.

    Not reopened to PENDING: that would hide the problem behind a retry that
    is very likely to fail the same way, and the operator would learn nothing
    until the next run finished. FAILED with the real error is the honest
    state, and re-running is still available afterwards.
    """
    if dry_run:
        return len(rows)
    n = 0
    for r in rows:
        why = db.conn.execute(
            "SELECT error_message FROM audit_log WHERE source_user=? "
            "AND status='FAILED' ORDER BY timestamp DESC LIMIT 1",
            (r["source_email"],)).fetchone()
        db.set_identity_status(
            r["source_email"], "FAILED",
            "marked done but migrated nothing and recorded failures: "
            + ((why["error_message"] if why else "") or "")[:400])
        n += 1
    return n


def stale_user_failures(db) -> list:
    """User-level failures for users that subsequently migrated.

    A per-user failure is recorded when the whole user could not be started
    -- almost always because impersonation failed. Live, 175 of those read
    "invalid_grant: Invalid email or User ID", every one written while that
    target account was deleted. The user migrated fine on a later pass and
    is DONE now, but the row stayed and kept the user in the report's
    did-not-migrate list.

    identity_map.status is the authority on whether a user migrated -- it is
    what the engine itself writes when the user finishes -- so no network
    call is needed to answer this.
    """
    # Service-level rows too (item_id is the user, item_type the service): live,
    # yara's 09-26 'drive' unauthorized_client outlived 2,389 files migrated
    # after it, and sat in every failure count and chart since. Only once that
    # service is actually recorded done for the user.
    rows = db.conn.execute(
        """SELECT a.source_user, a.item_id, a.item_type FROM audit_log a
             JOIN identity_map i ON i.source_email = a.source_user
            WHERE a.status = 'FAILED' AND a.item_id = a.source_user
              AND i.status = 'DONE'""").fetchall()
    return [r for r in rows if r["item_type"] == "user"
            or r["item_type"] in db.services_done(r["source_user"])]


def resolve_users(db, rows, dry_run: bool = True) -> int:
    """Same preservation rule as resolve(): status changes, error kept."""
    if dry_run:
        return len(rows)
    for r in rows:
        db.log_audit(r["source_user"], r["item_id"], r["item_type"],
                     "SKIPPED_USER_LATER_MIGRATED",
                     "this user failed to start on an earlier pass and has "
                     "since migrated successfully")
    return len(rows)


def resolve(db, rows, status: str, note: str, dry_run: bool = True) -> int:
    """Mark failures resolved, preserving what they were.

    Not a delete. The audit row is the record that this was attempted, and a
    migration that erases its own history cannot explain itself later -- so
    the status changes and the original error is kept in the note.
    """
    if dry_run:
        return len(rows)
    n = 0
    for r in rows:
        db.log_audit(r["source_user"], r["item_id"], "acl", status, note)
        n += 1
    return n


def retry_failed_shared_drives(auth, db, settings, apply: bool = False) -> int:
    """Re-run the shared-drive migration for just the drives still missing.

    The same SharedDriveMigrator the run uses, so a retry is exactly a first
    attempt: create (fresh requestId), members, contents. Returns how many now
    have a mapping (or, dry, how many would be tried).
    """
    failed = {r["item_id"] for r in db.conn.execute(_UNMAPPED_FAILED_SHARED_DRIVES)}
    if not failed or not apply:
        return len(failed)
    import shared_drives
    mig = shared_drives.SharedDriveMigrator(auth, db, settings, settings.source_admin,
                                            settings.target_admin)
    fixed = 0
    for d in (d for d in mig.list_source_drives(True) if d["id"] in failed):
        mig._safe_migrate_one(d)
        if db.get_target_id(settings.source_admin, d["id"], "shared_drive"):
            db.log_audit(settings.source_admin, d["id"], "shared_drive", "SUCCESS",
                         "created on a repair retry")
            fixed += 1
    return fixed


def fix_modified_times(auth, db, settings, apply: bool = False,
                       workers: int = 8) -> dict:
    """Put back the modifiedTime of every migrated Drive item whose copy carries
    another -- a drift no failure row records, so it has to be looked for.

    Found by the item-by-item tally: a quarter of one 300-user run's files, native
    Docs/Sheets copied server-side, kept the copy's time. Listing both sides is
    what the tally already pays (a page per 1,000 items, not a call per file),
    then only the drifted items are written. Users side by side; each patch
    spends that user's own quota. Never raises.
    """
    import tally
    from concurrent.futures import ThreadPoolExecutor

    from resilience import retry_on_google_error

    from db import utc_now
    out = {"checked": 0, "drifted": 0, "fixed": 0, "failed": 0}
    # Only users due a check: never checked, or written to since their own last one.
    # Every run ends with a repair, and re-listing all 300 users (~20 minutes) after a
    # run that touched five is work that finds nothing. Any status, not DONE only: a
    # stopped run's users are exactly the ones whose end-of-pass time check never ran.
    due = db.users_due_mtime_check()
    pairs = [(r["source_email"], r["target_email"]) for r in db.all_identities()
             if r["entity_type"] == "user" and r["source_email"] in due]
    out["users"] = len(pairs)

    def one(pair) -> dict:
        got = {"checked": 0, "drifted": 0, "fixed": 0, "failed": 0}
        started = utc_now()       # before the listing: a write during it is due next time
        try:
            src_items, tgt_items = {}, {}
            tally.count_drive(auth.source_drive(pair[0]), settings, keep=src_items)
            tally.count_drive(auth.target_drive(pair[1]), settings, keep=tgt_items)
            tgt = auth.target_drive(pair[1])
            for sid, tid in db.mapped_ids(pair[0], ("file", "folder")).items():
                s, t = src_items.get(sid), tgt_items.get(tid)
                if not s or not t:
                    continue
                got["checked"] += 1
                want = s.get("modifiedTime") or ""
                if not want or want[:19] == (t.get("modifiedTime") or "")[:19]:
                    continue
                got["drifted"] += 1
                if not apply:
                    continue
                try:
                    retry_on_google_error(max_retries=settings.max_retries)(
                        lambda f=tid, m=want: tgt.files().update(
                            fileId=f, body={"modifiedTime": m}, supportsAllDrives=True,
                            fields="id").execute())()
                    got["fixed"] += 1
                except Exception:      # noqa: BLE001 - a locked file, a deleted one
                    got["failed"] += 1
            if apply and not got["failed"]:
                db.record_mtime_check(pair[0], started)
        except Exception as exc:      # noqa: BLE001
            log.warning("[%s] modifiedTime check failed: %s", pair[0], exc)
        return got

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for got in pool.map(one, pairs):
            for k, v in got.items():
                out[k] += v
    return out


def restore_direct_grants(auth, db, settings, apply: bool = False,
                          workers: int = 8, users: list[str] | None = None,
                          stop=None, progress=None) -> dict:
    """Put back a direct grant a file held on top of an inherited one.

    Found by the one-to-one check: when a corpus shares at folder level the
    engine stops recreating inherited grants per file, and it skipped any
    permission with an inherited part -- so a direct grant on top of one
    (commenter on the file, reader through its folder) was dropped, and the
    target kept only the folder's reader. Fixed in the engine; this repairs
    what was migrated before. One permissions.list per migrated item, a create
    only where the source mixes the two -- not a re-grant of everything.
    Users side by side. Never raises.

    Owner and organizer grants are skipped exactly as the engine skips them.
    Live, nearly every file listed its OWNER as mixed (owner directly, writer
    through a folder), so the first run asked Drive to add each owner to their
    own file as owner: ~184,000 creates in three hours, every one a 403.
    `stop` (main.SHUTDOWN) is read between items, so Stop works without a kill.
    """
    from concurrent.futures import ThreadPoolExecutor

    from drive_engine import _role_rank
    from resilience import retry_on_google_error

    out = {"users": 0, "items": 0, "unreadable": 0, "mixed": 0, "granted": 0, "failed": 0}
    pairs = [(r["source_email"], r["target_email"]) for r in db.all_identities()
             if r["entity_type"] == "user" and r["status"] == "DONE"
             and (not users or r["source_email"].lower() in {u.lower() for u in users})]
    retry = retry_on_google_error(max_retries=settings.max_retries)

    def one(pair) -> dict:
        got = {"items": 0, "unreadable": 0, "mixed": 0, "granted": 0, "failed": 0}
        try:
            src, tgt = auth.source_drive(pair[0]), auth.target_drive(pair[1])
            for sid, tid in db.mapped_ids(pair[0], ("file", "folder")).items():
                if stop is not None and stop.is_set():
                    break
                got["items"] += 1
                try:
                    perms = retry(lambda f=sid: src.permissions().list(
                        fileId=f, supportsAllDrives=True,
                        fields="permissions(type,role,emailAddress,domain,permissionDetails)"
                    ).execute())().get("permissions", [])
                except Exception:      # noqa: BLE001 - gone since the migration
                    got["unreadable"] += 1
                    continue
                for p in perms:
                    if p.get("role") in ("owner", "organizer", "fileOrganizer"):
                        continue
                    details = p.get("permissionDetails") or []
                    direct = [d for d in details if not d.get("inherited")]
                    if not direct or len(direct) == len(details) or p.get("type") not in ("user", "group"):
                        continue
                    email = p.get("emailAddress") or ""
                    mapped = db.resolve_identity(email) or (
                        email if email.split("@")[-1].lower() != settings.source_domain.lower() else None)
                    if not mapped:
                        continue
                    got["mixed"] += 1
                    if not apply:
                        continue
                    role = max((d.get("role") or p["role"] for d in direct), key=_role_rank)
                    try:
                        retry(lambda f=tid, b={"type": p["type"], "role": role, "emailAddress": mapped}:
                              tgt.permissions().create(fileId=f, body=b, sendNotificationEmail=False,
                                                       supportsAllDrives=True, fields="id").execute())()
                        got["granted"] += 1
                    except Exception:      # noqa: BLE001
                        got["failed"] += 1
        except Exception as exc:      # noqa: BLE001
            log.warning("[%s] direct-grant check failed: %s", pair[0], exc)
        return got

    if progress:
        progress(f"  [0/{len(pairs)}] users checked")
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for pair, got in zip(pairs, pool.map(one, pairs)):
            out["users"] += 1
            for k, v in got.items():
                out[k] += v
            if progress:
                progress(f"  [{out['users']}/{len(pairs)}] {pair[0]}: {got['items']:,} item(s), "
                         f"{got['mixed']:,} to put back, {got['granted']:,} put back, "
                         f"{got['failed']:,} failed, {got['unreadable']:,} unreadable")
    return out


def run_all(db, auth, settings, apply: bool = False,
            reconcile_limit: int | None = None,
            reapply_passes: int = 6) -> dict:
    """Survey, then fix what can be fixed without guessing.

    Called automatically at the end of a migration, so the failure count an
    operator sees is the residue that actually needs a human rather than the
    raw total. On the live 201-user run those differed by 91,000.

    Two repairs run here and a third deliberately does not:

      - Stale grantee failures are resolved, each confirmed against the
        directory first.
      - Quota-refused ACL grants are reconciled against the target, which is
        one list call per affected FILE (not per grant) -- the 27,597 rows
        on the live ledger cover far fewer files than that.
      - Gmail label failures are NOT touched. They are repaired at their
        source in sync_labels(), and the messages are re-inserted by the
        next migrate or delta pass. Rewriting their audit rows here would
        report them fixed before the data had actually moved.

    Never raises. A repair pass that can break the migration it follows is
    worse than no repair pass.
    """
    out = {"survey": {}, "resolved": 0, "reconciled": 0, "errors": []}
    # Before the failure survey, and whatever it finds: a copy carrying the wrong
    # modifiedTime is not a failure row, so a run with none still needs this.
    try:
        out["mtimes"] = fix_modified_times(auth, db, settings, apply=apply)
    except Exception as exc:      # noqa: BLE001
        out["errors"].append(f"modifiedTime: {str(exc)[:160]}")
    # Also before the survey: an owed grant is not a failure row either.
    try:
        out["owed_grants"] = reapply_owed_grants(auth, db, settings, apply=apply)
    except Exception as exc:      # noqa: BLE001
        out["errors"].append(f"owed grants: {str(exc)[:160]}")
    try:
        out["survey"] = survey(db)
    except Exception as exc:      # noqa: BLE001
        out["errors"].append(f"survey: {str(exc)[:160]}")
        return out
    if not out["survey"].get("total"):
        return out

    if out["survey"].get("acl_no_account"):
        try:
            stale = stale_grantee_failures(db, auth.directory("target"))
            out["resolved"] = resolve(
                db, stale, "SKIPPED_GRANTEE_RECREATED",
                "grantee had no account when this was attempted; the account "
                "exists now, so the row describes a state that no longer holds",
                dry_run=not apply)
        except Exception as exc:      # noqa: BLE001
            out["errors"].append(f"grantee check: {str(exc)[:160]}")

    # Before the stale-user pass, which keys off status == DONE: demoting
    # first stops a user that migrated nothing from having its own failure
    # row resolved as "migrated on a later pass".
    if out["survey"].get("false_done"):
        try:
            out["demoted"] = demote_false_done(
                db, false_done_users(db), dry_run=not apply)
        except Exception as exc:      # noqa: BLE001
            out["errors"].append(f"false-done check: {str(exc)[:160]}")

    if out["survey"].get("user_stale"):
        try:
            out["users_resolved"] = resolve_users(
                db, stale_user_failures(db), dry_run=not apply)
        except Exception as exc:      # noqa: BLE001
            out["errors"].append(f"user rollup: {str(exc)[:160]}")

    # A shared-drive member FAILED under an empty key is the pre-fix no-address
    # case (a deleted account's leftover grant, sent to Google as ""). No run
    # can ever overwrite an empty key, so without this it reads as a failure
    # forever -- live, the last FAILED row on account 3.
    if apply:
        for r in db.conn.execute(
                "SELECT source_user FROM audit_log WHERE item_type='shared_drive_member' "
                "AND status='FAILED' AND item_id=''").fetchall():
            db.log_audit(r["source_user"], "", "shared_drive_member",
                         "SKIPPED_UNMAPPED_IDENTITY",
                         "member had no emailAddress (a deleted account); recorded as "
                         "FAILED before shared_drives skipped these")

    if out["survey"].get("shared_drives"):
        try:
            out["shared_drives_retried"] = retry_failed_shared_drives(
                auth, db, settings, apply=apply)
        except Exception as exc:      # noqa: BLE001
            out["errors"].append(f"shared drives: {str(exc)[:160]}")

    # Before the ACL passes below: a file ACLs would apply to has to exist first,
    # and a stranded file is exactly what an ACL grant has nothing to attach to.
    if out["survey"].get("drive_stragglers"):
        try:
            dr = retry_drive_stragglers(auth, db, settings, apply=apply)
            out["stragglers"] = dr["attempted"]
            out["stragglers_retried"] = dr["retried"]
            out["errors"].extend(dr["errors"])
        except Exception as exc:      # noqa: BLE001
            out["errors"].append(f"drive stragglers: {str(exc)[:160]}")

    # Folders first, always. Their grants are what everything inside
    # inherits, so repairing a folder can restore access to hundreds of
    # files at once -- and repairing the files first would spend the rate
    # limiter's budget on the cheaper half of the problem.
    out["doors"] = broken_folder_grants(db)

    if out["survey"].get("acl_quota"):
        try:
            import acl_reconcile
            stats = acl_reconcile.reconcile(auth, db, settings,
                                            dry_run=not apply,
                                            limit=reconcile_limit)
            out["reconciled"] = stats.get("resolved", 0)
            out["reconcile_stats"] = stats
        except Exception as exc:      # noqa: BLE001
            out["errors"].append(f"acl reconcile: {str(exc)[:160]}")

    # Items no later pass would revisit on its own. Gmail and Calendar list
    # by the item's own date, so a message or event that failed to import is
    # never offered again unless somebody edits it -- four such rows survived
    # two runs and a repair while everything around them was retried.
    try:
        import retry_failed
        rf = retry_failed.retry(auth, db, settings, apply=apply)
        out["stranded"] = rf["messages"] + rf["events"]
        out["stranded_retried"] = rf["retried"]
        out["errors"].extend(rf["errors"])
    except Exception as exc:          # noqa: BLE001
        out["errors"].append(f"stranded retry: {str(exc)[:160]}")

    # Reconciling answers "is this grant actually missing?" and stops there.
    # Re-applying the ones that ARE missing is a separate pass, and leaving
    # it out made "Repair" a misnomer: a live run finished with 447 failures,
    # the pass resolved 1, and the 273 grants it had just confirmed absent
    # stayed absent because nothing put them back. Folders first, since one
    # folder's grant is what everything inside it inherits.
    if apply and out["survey"].get("acl_quota"):
        try:
            import acl_repair
            applied = acl_repair.repair_until_settled(
                auth, db, settings, max_passes=reapply_passes)
            out["reapplied"] = applied.get("applied", 0)
            out["reapply_passes"] = applied.get("passes", 0)
        except Exception as exc:      # noqa: BLE001
            out["errors"].append(f"acl re-apply: {str(exc)[:160]}")
    return out


def summarise(result: dict) -> str:
    """One line per thing that happened, for a migration's closing log."""
    s = result.get("survey") or {}
    m = result.get("mtimes") or {}
    times = (f"{m['fixed']:,} of {m['drifted']:,} drifted modified time(s) put back"
             if m.get("drifted") else "")
    og = result.get("owed_grants") or {}
    # Owed shares are not failure rows, so they are said even when nothing failed.
    owed = (f"{og.get('granted', 0):,} owed share(s) granted; "
            + (f"{og['covered']:,} already had access through their folder; " if og.get("covered") else "")
            + f"{og['owed'] - og.get('ready', 0):,} still waiting for the colleague's account"
            if og.get("owed") else "")
    if not s.get("total"):
        return "; ".join(p for p in ("no failed items recorded", times, owed) if p)
    parts = [f"{s['total']:,} failed item(s)"]
    if times:
        parts.append(times)
    if result.get("resolved"):
        parts.append(f"{result['resolved']:,} resolved (grantee recreated)")
    if owed:
        parts.append(owed)
    if result.get("reconciled"):
        parts.append(f"{result['reconciled']:,} resolved (already on target)")
    if result.get("stranded_retried"):
        parts.append(f"{result['stranded_retried']:,} stranded item(s) "
                     f"re-imported")
    if result.get("shared_drives_retried"):
        parts.append(f"{result['shared_drives_retried']:,} shared drive(s) re-created")
    if result.get("stragglers_retried"):
        parts.append(f"{result['stragglers_retried']:,}/{result.get('stragglers', 0):,} "
                     f"drive straggler(s) re-copied")
    if result.get("reapplied"):
        parts.append(f"{result['reapplied']:,} grant(s) re-applied in "
                     f"{result.get('reapply_passes', 0)} pass(es)")
    if result.get("demoted"):
        parts.append(f"{result['demoted']:,} user(s) demoted from done "
                     f"(migrated nothing)")
    if result.get("users_resolved"):
        parts.append(f"{result['users_resolved']:,} resolved (user migrated "
                     f"on a later pass)")
    if s.get("gmail_invalid_label"):
        parts.append(f"{s['gmail_invalid_label']:,} Gmail label failure(s) "
                     f"will retry on the next pass")
    doors = result.get("doors") or {}
    if doors.get("folders"):
        parts.append(f"{doors['folders']:,} folder share(s) failed, gating "
                     f"{doors['files_behind']:,} file(s)")
    for e in result.get("errors", []):
        parts.append(f"repair step failed: {e}")
    return "; ".join(parts)
