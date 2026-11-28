"""Per-user DMS progress: each mailbox's message count on the target against the source.

Google's console reports one total for a whole import, read by scraping it (dms_migrate --metrics). This
reads the mailboxes themselves (Gmail getProfile.messagesTotal, one call per mailbox and side, the same
count dms_user_verify uses to decide a mailbox has landed), so a bar per user can move every minute.
Read-only on both tenants. A count is approximate by design: a new target account also holds Google's
own welcome mail, and a mailbox still receiving new mail on the source moves its own goal.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
FRESH_SEC = 60          # a page polling every minute costs at most one count per mailbox and side
WORKERS = 8


def _path(account_id: int) -> str:
    return os.path.join(HERE, "logs", "dms", str(account_id), "per-user.json")


def _count(gmail) -> int | None:
    try:
        n = gmail.users().getProfile(userId="me").execute().get("messagesTotal")
        return int(n) if n is not None else None
    except Exception:      # noqa: BLE001 - an unreadable mailbox is unknown, never zero
        return None


def count(auth, pairs: list[tuple[str, str, str]]) -> list[dict]:
    """pairs: (source, target, status). Source and target counted side by side, WORKERS at a time."""
    def one(p):
        src, tgt, status = p
        s, t = _count(auth.source_gmail(src)), _count(auth.target_gmail(tgt))
        share = None if not s or t is None else round(min(1.0, t / s), 4)
        return {"source": src, "target": tgt, "status": status, "sourceCount": s, "targetCount": t,
                "share": 1.0 if s == 0 and t is not None else share}
    with ThreadPoolExecutor(max_workers=max(1, min(WORKERS, len(pairs) or 1))) as pool:
        return list(pool.map(one, pairs))


def view(account_id: int, auth_factory, pairs_factory, now=time.time) -> dict:
    """The cached counts, refreshed when older than FRESH_SEC. auth_factory/pairs_factory are called only
    on a refresh, so a cached read touches neither Google nor the ledger."""
    path = _path(account_id)
    try:
        with open(path, encoding="utf-8") as fh:
            cached = json.load(fh)
        if now() - cached.get("at", 0) < FRESH_SEC:
            return cached
    except (OSError, ValueError):
        pass
    users = count(auth_factory(), pairs_factory())
    out = {"at": now(), "users": users,
           "note": "Mailbox totals as Gmail reports them; the target also holds Google's welcome mail."}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    os.replace(tmp, path)
    return out
