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


def survey(db) -> dict:
    """Count the failure families without touching the network."""
    def n(where: str, params=()) -> int:
        return db.conn.execute(
            f"SELECT COUNT(*) c FROM audit_log WHERE status='FAILED' AND {where}",
            params).fetchone()["c"]

    return {
        "total": n("1=1"),
        "acl_no_account": n("item_type='acl' AND error_message LIKE ?",
                            (f"%{NO_ACCOUNT}%",)),
        "acl_quota": n("item_type='acl' AND error_message LIKE ?",
                       (f"%{QUOTA}%",)),
        "gmail_invalid_label": n("item_type='message' AND error_message LIKE ?",
                                 (f"%{INVALID_LABEL}%",)),
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
            reconcile_limit: int | None = None) -> dict:
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
    if s.get("gmail_invalid_label"):
        parts.append(f"{s['gmail_invalid_label']:,} Gmail label failure(s) "
                     f"will retry on the next pass")
    for e in result.get("errors", []):
        parts.append(f"repair step failed: {e}")
    return "; ".join(parts)
