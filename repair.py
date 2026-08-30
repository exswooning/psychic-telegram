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
        "user_stale": db.conn.execute(
            """SELECT COUNT(*) c FROM audit_log a
                 JOIN identity_map i ON i.source_email = a.source_user
                WHERE a.status = 'FAILED' AND a.item_type = 'user'
                  AND i.status = 'DONE'""").fetchone()["c"],
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
    return db.conn.execute(
        """SELECT a.source_user, a.item_id FROM audit_log a
             JOIN identity_map i ON i.source_email = a.source_user
            WHERE a.status = 'FAILED' AND a.item_type = 'user'
              AND i.status = 'DONE'""").fetchall()


def resolve_users(db, rows, dry_run: bool = True) -> int:
    """Same preservation rule as resolve(): status changes, error kept."""
    if dry_run:
        return len(rows)
    for r in rows:
        db.log_audit(r["source_user"], r["item_id"], "user",
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
    if not s.get("total"):
        return "no failed items recorded"
    parts = [f"{s['total']:,} failed item(s)"]
    if result.get("resolved"):
        parts.append(f"{result['resolved']:,} resolved (grantee recreated)")
    if result.get("reconciled"):
        parts.append(f"{result['reconciled']:,} resolved (already on target)")
    if result.get("stranded_retried"):
        parts.append(f"{result['stranded_retried']:,} stranded item(s) "
                     f"re-imported")
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
