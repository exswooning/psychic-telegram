"""Re-attempt failures that no later pass would ever revisit.

Drive's delta enumerates files and notices a missing mapping, so a file that
failed once gets picked up again on its own. Gmail and Calendar do not work
that way: they list by the ITEM's own date -- `newer_than:Nd` and
`updatedMin` -- so a message or event that failed to import is never offered
again unless somebody edits it. Its FAILED row then sits in the ledger
permanently, and every later run reports it while doing nothing about it.

Live, that left four failures stranded across two runs and a repair: two
task lists, one calendar event and one message, all still carrying their
original timestamps while everything around them had been retried.

Task lists need nothing here -- that engine re-lists everything each run, so
enabling the service is enough (see main._enable_selected_services).
"""
from __future__ import annotations

import logging

log = logging.getLogger("retry_failed")

# Only what a retry can actually reach. A failed 'user' row means the whole
# user errored, which is a migration's job, not this pass's.
RETRYABLE_TYPES = ("message", "event")


def failed_items(db, item_types=RETRYABLE_TYPES) -> dict:
    """{source_user: {item_type: [item_id, ...]}} for retryable failures."""
    marks = ",".join("?" for _ in item_types)
    rows = db.conn.execute(
        f"""SELECT source_user, item_type, item_id FROM audit_log
             WHERE status = 'FAILED' AND item_type IN ({marks})""",
        tuple(item_types)).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["source_user"], {}).setdefault(
            r["item_type"], []).append(r["item_id"])
    return out


def _targets(db) -> dict:
    return dict(db.conn.execute(
        "SELECT source_email, target_email FROM identity_map").fetchall())


def retry(auth, db, settings, apply: bool = False, limit: int | None = None,
          _gmail_cls=None, _calendar_cls=None) -> dict:
    """Re-attempt each stranded item, one call per item.

    Reports what it would do when apply is False, so the survey can name a
    number without touching either tenant.
    """
    from gmail_engine import GmailMigrator
    from calendar_engine import CalendarMigrator
    gmail_cls = _gmail_cls or GmailMigrator
    calendar_cls = _calendar_cls or CalendarMigrator

    targets = _targets(db)
    stats = {"messages": 0, "events": 0, "retried": 0, "unmapped_users": 0,
             "errors": []}
    for src_user, by_type in failed_items(db).items():
        tgt_user = targets.get(src_user)
        if not tgt_user:
            # No target account: the item cannot go anywhere, and saying it
            # was retried would be a lie the next run has to correct.
            stats["unmapped_users"] += 1
            continue

        msgs = by_type.get("message", [])
        events = by_type.get("event", [])
        if limit is not None:
            msgs = msgs[:limit]
            events = events[:limit]

        if msgs:
            stats["messages"] += len(msgs)
            if apply:
                try:
                    gm = gmail_cls(auth, db, settings, src_user, tgt_user)
                    gm.sync_labels()
                    for mid in msgs:
                        gm._migrate_one_message({"id": mid})
                        stats["retried"] += 1
                except Exception as exc:      # noqa: BLE001 - report, never abort
                    stats["errors"].append(f"{src_user} gmail: {str(exc)[:120]}")

        if events:
            stats["events"] += len(events)
            if apply:
                try:
                    cm = calendar_cls(auth, db, settings, src_user, tgt_user)
                    for eid in events:
                        item = cm.fetch_event(eid)
                        if item is None:
                            continue
                        cm.migrate_event(item)
                        stats["retried"] += 1
                except Exception as exc:      # noqa: BLE001
                    stats["errors"].append(f"{src_user} calendar: "
                                           f"{str(exc)[:120]}")
    return stats
