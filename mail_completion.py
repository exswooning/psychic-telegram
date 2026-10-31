"""Cheap mailbox accounting. Counts never certify message content or resolve findings."""
from __future__ import annotations

import json


def full_mail_check(settings) -> bool:
    # An explicit full verification remains available independently of migration.
    mode = getattr(settings, "mail_completion_check", "counts")
    if mode not in ("counts", "full"):
        raise ValueError("MAIL_COMPLETION_CHECK must be counts or full")
    return mode == "full"


def compare_mail_counts(auth, db, settings, progress=print, source_users=()):
    """Two profile reads per mapped user; never fetch message bodies or modify mail.

    Absolute totals include drafts/spam/trash and existing target mail. Extras
    are reported, not silently subtracted: equal counts can still hide gaps.
    """
    from db import utc_now
    from resilience import retry_on_google_error
    out = {"scope": "mailbox counts only; not 1:1 content, labels or identity verification",
           "checkedAt": utc_now(), "users": [], "blocked": 0}
    for row in db.all_identities():
        if row["entity_type"] != "user":
            continue
        source, target = row["source_email"], row["target_email"]
        if source_users and source not in source_users:
            continue
        if (source.rsplit("@", 1)[-1].lower() != settings.source_domain.lower()
                or target.rsplit("@", 1)[-1].lower() != settings.target_domain.lower()):
            raise ValueError("Mail count identity is outside the configured tenant pair")
        item = {"source": source, "target": target}
        try:
            def count(api):
                value = retry_on_google_error(max_retries=3)(
                    lambda: api.users().getProfile(userId="me").execute())().get("messagesTotal")
                if type(value) is not int or value < 0:
                    raise ValueError("Mailbox message count is unavailable")
                return value
            s, t = count(auth.source_gmail(source)), count(auth.target_gmail(target))
            item.update(sourceMessages=s, targetMessages=t, difference=t-s,
                        verdict="COUNTS_MATCH" if s == t else "TARGET_HAS_EXTRAS" if t > s else "TARGET_SHORT")
            if s != t:
                out["blocked"] += 1
        except Exception as exc:  # missing access is unknown, never zero
            item.update(verdict="UNKNOWN", error=str(exc)[:200])
            out["blocked"] += 1
        out["users"].append(item)
        progress("Mail count comparison: " + json.dumps(item))
    if not out["users"]:
        raise ValueError("No mapped mailbox users to compare")
    with db.write() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS mail_count_checks "
                     "(checked_at TEXT NOT NULL, report TEXT NOT NULL)")
        conn.execute("INSERT INTO mail_count_checks VALUES (?,?)",
                     (out["checkedAt"], json.dumps(out)))
    return out
