"""Which users the DMS may take after a split run, and why any user is held back.

Split mode: the engine moves the mail that carries Drive links (rewritten) and leaves the rest to Google's
DMS, which runs AFTER. The DMS cannot rewrite anything, so a user is held back from it only when the DMS
could carry link mail across unrewritten -- never for anything else (operator, 13 Nov 2026):

- the user has not finished (RUNNING) or cannot receive mail yet (BLOCKED: waiting on a licence);
- a mail item the engine was meant to move failed and is not on the target;
- a source message the ledger has never seen (it arrived after the user's pass -- the source is live)
  carries a Drive link, or could not be read to prove it does not.

A user's status is NOT a reason: the migration marks every user DONE at the end, even one whose coverage
was incomplete, and a FAILED Drive item says nothing about mail. New mail without links is exactly what
the DMS is for, and its delta catches whatever arrives later. One held user never holds the rest.
"""
from __future__ import annotations

import base64
import re
from typing import Callable

DRIVE_LINK = re.compile(r"(?:drive|docs)\.google\.com", re.I)


def ledger_holds(conn) -> dict[str, str]:
    """Holds the ledger alone can see: unfinished users, and mail the engine failed to move."""
    from config import DMS_OWED_FAILURES
    holds: dict[str, str] = {}
    for user, status in conn.execute("SELECT source_email, status FROM identity_map "
                                     "WHERE entity_type='user' AND status IN ('RUNNING', 'BLOCKED')"):
        holds[user.lower()] = (f"status {status}: " +
                               ("its pass has not finished" if status == "RUNNING" else "it cannot receive mail yet"))
    # The same rows api_server._migration_progress counts as mailBlockingDms, per user.
    sql = ("SELECT a.source_user, COUNT(DISTINCT a.item_id) FROM audit_log a "
           "WHERE a.status LIKE 'FAILED%' AND a.item_type IN ('message','draft') "
           "AND EXISTS (SELECT 1 FROM identity_map m WHERE m.source_email = a.source_user) "
           "AND NOT EXISTS (SELECT 1 FROM id_mapping mp WHERE mp.source_user = a.source_user "
           "                AND mp.source_id = a.item_id AND mp.type = a.item_type) "
           + "".join(" AND COALESCE(a.error_message,'') NOT LIKE ?" for _ in DMS_OWED_FAILURES)
           + " GROUP BY a.source_user")
    for user, n in conn.execute(sql, tuple(f"%{m}%" for m in DMS_OWED_FAILURES)):
        holds.setdefault(user.lower(), f"{n} mail item(s) failed and are not on the target; "
                                       "the DMS would carry them unrewritten")
    return holds


def unknown_source_messages(gmail, conn, user: str) -> list[str]:
    """Source message ids (Spam and Trash included) the ledger knows nothing about: neither copied, nor
    deferred to the DMS, nor a draft. In practice: mail that arrived after the user's pass."""
    ids, token = [], None
    while True:
        page = gmail.users().messages().list(userId="me", maxResults=500, pageToken=token,
                                             includeSpamTrash=True).execute()
        ids += [m["id"] for m in page.get("messages", [])]
        token = page.get("nextPageToken")
        if not token:
            break
    known = {r[0] for r in conn.execute("SELECT source_id FROM id_mapping WHERE source_user=? "
                                        "AND type IN ('message','draft')", (user,))}
    known |= {r[0] for r in conn.execute("SELECT item_id FROM audit_log WHERE source_user=? "
                                         "AND item_type IN ('message','draft')", (user,))}
    return [i for i in ids if i not in known]


def risky(gmail, ids: list[str]) -> list[str]:
    """The ids that carry a Drive link, or could not be read (unread cannot be proven safe)."""
    out = []
    for i in ids:
        try:
            raw = gmail.users().messages().get(userId="me", id=i, format="raw").execute()["raw"]
            body = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
            if DRIVE_LINK.search(body):
                out.append(i)
        except Exception:      # noqa: BLE001 - an unreadable message holds its user
            out.append(i)
    return out


def assess(conn, source_gmail: Callable[[str], object]) -> tuple[dict[str, str], dict[str, str]]:
    """(held, notes) for every user in the ledger. `source_gmail(user)` returns a read-only Gmail client."""
    held = ledger_holds(conn)
    notes: dict[str, str] = {}
    for (user,) in conn.execute("SELECT source_email FROM identity_map WHERE entity_type='user' ORDER BY 1"):
        key = user.lower()
        if key in held:
            continue
        try:
            gmail = source_gmail(user)
            unknown = unknown_source_messages(gmail, conn, user)
            bad = risky(gmail, unknown) if unknown else []
        except Exception as exc:      # noqa: BLE001 - a check that cannot run holds the user
            held[key] = f"the source check could not run: {str(exc)[:160]}"
            continue
        if bad:
            held[key] = (f"{len(bad)} message(s) not on the target carry a Drive link or cannot be read: "
                         + ", ".join(bad[:5]))
        elif unknown:
            notes[key] = f"{len(unknown)} new message(s) since its pass, none with a Drive link: the DMS carries them"
    return held, notes
