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

# The grantee-missing 400 is quoted verbatim by Drive; matched on the stable
# half of the sentence rather than the address list it interpolates.
NO_ACCOUNT = "no Google accounts associated with these email addresses"
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
            except Exception as exc:      # noqa: BLE001
                text = str(exc)
                seen[grantee] = not ("404" in text or "notFound" in text
                                     or "Resource Not Found" in text)
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
