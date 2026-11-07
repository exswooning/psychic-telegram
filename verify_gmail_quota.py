"""Weighted Gmail read pacing shared by verification threads and processes.

New projects get 6,000 units/min/user: metadata get costs 20, raw get 60.
Keep 20% headroom by default. A batch is charged for EVERY inner request,
including retries. Runtime state contains hashed bucket IDs, never credentials.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import logging
import math
import os
from pathlib import Path
import sqlite3
import time

from resilience import _extract_reason, _retry_after_seconds

log = logging.getLogger(__name__)


def _positive_env(name, default):
    try:
        value = float(os.getenv(name, str(default)))
        return value if math.isfinite(value) and value > 0 else default
    except ValueError:
        return default


def is_quota_error(exc):
    status = getattr(getattr(exc, "resp", None), "status", None)
    return status == 429 or (status == 403 and _extract_reason(exc) in
                            {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"})


class MailboxBudget:
    def __init__(self, path, key, ceiling=80.0, clock=time.time, sleep=time.sleep):
        self.path, self.key, self.ceiling = str(path), key, ceiling
        self.clock, self.sleep = clock, sleep
        # Each transaction opens its own connection: callers can be different threads.
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS budgets ("
                       "bucket TEXT PRIMARY KEY, next_at REAL, cooldown REAL, rate REAL, changed REAL)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            yield db
        finally:
            db.close()

    def _state(self, db, now):
        row = db.execute("SELECT next_at,cooldown,rate,changed FROM budgets WHERE bucket=?",
                         (self.key,)).fetchone()
        return row or (now, 0.0, self.ceiling, now)

    def acquire(self, cost, stop=lambda: None):
        while True:
            stop()
            now = self.clock()
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                due, cooldown, rate, changed = self._state(db, now)
                rate = min(rate, self.ceiling)
                # Recover slowly only after a full clean minute; never exceed the budget.
                if now - changed >= 60 and now >= cooldown:
                    rate = min(self.ceiling, rate + self.ceiling * 0.05)
                    changed = now
                wait = max(due, cooldown) - now
                if wait <= 0:
                    db.execute("INSERT OR REPLACE INTO budgets VALUES (?,?,?,?,?)",
                               (self.key, now + cost / rate, cooldown, rate, changed))
                db.commit()
            if wait <= 0:
                return
            # Never hold a database lock while waiting. Recheck shared cooldown and stop.
            self.sleep(min(wait, 0.5))

    def penalise(self, exc):
        if not is_quota_error(exc):
            return
        now = self.clock()
        retry_after = _retry_after_seconds(exc) or 0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            due, cooldown, rate, changed = self._state(db, now)
            # One rejected batch can have 10 quota errors and many siblings in flight.
            # They trigger one reduction, not ten consecutive halvings.
            fresh = now >= cooldown
            if fresh:
                rate = max(self.ceiling * 0.1, min(rate, self.ceiling) / 2)
                changed = now
            cooldown = max(cooldown, now + max(60, retry_after))
            db.execute("INSERT OR REPLACE INTO budgets VALUES (?,?,?,?,?)",
                       (self.key, due, cooldown, rate, changed))
            db.commit()
        if fresh:
            log.warning("Gmail verification quota cooldown: %.0fs; budget %.1f units/s; "
                        "successful messages retained", cooldown - now, rate)


def for_mailbox(auth, settings, side, user):
    # Resolve the effective delegated subject, including domain-handover aliases.
    creds = auth._credentials(side, user)
    project = (getattr(creds, "quota_project_id", None) or getattr(creds, "project_id", None)
               or getattr(creds, "service_account_email", None) or getattr(creds, "client_id", None))
    if not project:
        project = getattr(settings, side + "_sa_key", None) or str(getattr(settings, "account_id", "default")) + side
    subject = getattr(creds, "_subject", None) or user
    key = hashlib.sha256(f"{project}\n{subject.lower()}".encode()).hexdigest()
    root = Path.home() / ".cache" / "bitport-gmail-verification"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    path = root / "quota.sqlite3"
    # Create with restrictive permissions before sqlite opens it.
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    path.chmod(0o600)
    ceiling = _positive_env("VERIFY_GMAIL_UNITS_PER_MINUTE", 6000) * 0.8 / 60
    return MailboxBudget(path, key, ceiling)


def read_cost(fmt):
    # Override only when the actual project's legacy quota/costs are confirmed.
    return _positive_env("VERIFY_GMAIL_RAW_UNITS" if fmt == "raw" else "VERIFY_GMAIL_METADATA_UNITS",
                         60 if fmt == "raw" else 20)
