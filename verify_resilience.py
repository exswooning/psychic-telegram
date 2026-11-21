"""
verify_resilience.py -- a Google outage is not a verdict.

One Docs 503 held the Drive verification of info@s. at item 326 of 1,403 for 27 minutes. This is
the part that keeps that from happening again without ever turning "could not read" into a pass:

  * classify(exc)   what a failed read means: a transient Google fault (retry later), a permanent
                    one (403/404/...), or an object that is gone from one side.
  * CircuitBreaker  stops a run hammering an endpoint family that is down; an open breaker makes the
                    next read fail at once as CIRCUIT_OPEN, which classifies as transient -- so the
                    document is queued and the run goes on with other work. It never returns a
                    result, so it can never make an unread document look verified.
  * UnverifiedStore the documents still unverified, in the account's own ledger
                    (verification_unverified): survives a restart, is retried on the next pass, and is
                    deleted only by a comparison that really happened.

Outcomes of one object: IDENTICAL, DIFFERENT, MISSING_SOURCE, MISSING_TARGET (the compare ran), and
UNVERIFIED_TRANSIENT_ERROR / UNVERIFIED_PERMANENT_ERROR (it did not). Only the first two
describe content; the unverified two keep the service INCOMPLETE (verify_sample.verdict_of).
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

IDENTICAL = "IDENTICAL"
DIFFERENT = "DIFFERENT"
MISSING_SOURCE = "MISSING_SOURCE"
MISSING_TARGET = "MISSING_TARGET"
UNVERIFIED_TRANSIENT = "UNVERIFIED_TRANSIENT_ERROR"
UNVERIFIED_PERMANENT = "UNVERIFIED_PERMANENT_ERROR"
UNVERIFIED = (UNVERIFIED_TRANSIENT, UNVERIFIED_PERMANENT)

TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class CircuitOpen(RuntimeError):
    """The endpoint family is cooling down; nothing was sent."""

    def __init__(self, family: str, retry_in: float):
        super().__init__(f"circuit open for {family}: not sent, cooling down for {retry_in:.0f}s more")
        self.family, self.retry_in = family, retry_in


@dataclass
class Failure:
    state: str                 # UNVERIFIED_TRANSIENT_ERROR | UNVERIFIED_PERMANENT_ERROR | MISSING_*
    status: int | None
    attempts: int
    endpoint: str
    detail: str

    @property
    def transient(self) -> bool:
        return self.state == UNVERIFIED_TRANSIENT


def _status(exc: BaseException) -> int | None:
    import re
    from resilience import _status_of
    try:
        st = _status_of(exc)
    except Exception:         # noqa: BLE001 - not an HttpError
        st = None
    if st:
        return int(st)
    m = re.search(r"HTTP (\d{3})", str(exc))
    return int(m.group(1)) if m else None


def classify(exc: BaseException) -> Failure:
    """What a failed read means. A 404 on the SOURCE or TARGET read (the reader tags the side:
    native_fidelity._side) is MISSING_*; every other 4xx is permanent; 5xx/429/transport/an open
    circuit are transient. Anything unrecognised is permanent -- never retried into a pass."""
    from resilience import PermanentAPIError, RetryBudgetExceeded, TransportExhausted
    endpoint = getattr(exc, "bitport_endpoint", "") or ""
    side = getattr(exc, "bitport_side", "")
    detail = str(exc)[:300]
    status = getattr(exc, "status", None) if isinstance(exc, RetryBudgetExceeded) else None
    status = status or _status(exc)
    attempts = int(getattr(exc, "attempts", 0) or 0)
    if isinstance(exc, (CircuitOpen, TransportExhausted)) or isinstance(exc, RetryBudgetExceeded):
        return Failure(UNVERIFIED_TRANSIENT, status, attempts, endpoint, detail)
    if isinstance(exc, PermanentAPIError) or status is not None:
        if status == 404 and side in ("source", "target"):
            return Failure(MISSING_SOURCE if side == "source" else MISSING_TARGET, 404, attempts, endpoint, detail)
        if status in TRANSIENT_STATUSES:
            return Failure(UNVERIFIED_TRANSIENT, status, attempts, endpoint, detail)
        return Failure(UNVERIFIED_PERMANENT, status, attempts, endpoint, detail)
    return Failure(UNVERIFIED_PERMANENT, None, attempts, endpoint, detail)


class CircuitBreaker:
    """closed -> open after `threshold` transient failures inside `window` seconds; open for
    `cooldown`; then ONE probe (half-open): success closes it, failure re-opens it with the
    cooldown doubled (up to `max_cooldown`). Thread-safe; one per endpoint family."""

    def __init__(self, family: str, threshold: int = 3, window: float = 120.0, cooldown: float = 60.0,
                 max_cooldown: float = 600.0, clock=time.monotonic):
        self.family, self.threshold, self.window = family, threshold, window
        self.base_cooldown, self.max_cooldown, self._clock = cooldown, max_cooldown, clock
        self._lock = threading.Lock()
        self.state, self._failures, self._opened_at = "closed", [], 0.0
        self._cooldown, self._probing = cooldown, False

    def before(self) -> None:
        with self._lock:
            if self.state == "closed":
                return
            left = self._cooldown - (self._clock() - self._opened_at)
            if self.state == "open" and left <= 0:
                self.state, self._probing = "half_open", False
            if self.state == "half_open" and not self._probing:
                self._probing = True          # this caller is the probe
                return
            raise CircuitOpen(self.family, max(left, 0.0))

    def success(self) -> None:
        with self._lock:
            self.state, self._failures, self._probing = "closed", [], False
            self._cooldown = self.base_cooldown

    def failure(self) -> None:
        with self._lock:
            now = self._clock()
            if self.state == "half_open":
                self._cooldown = min(self._cooldown * 2, self.max_cooldown)
                self.state, self._opened_at, self._probing = "open", now, False
                return
            self._failures = [t for t in self._failures if now - t <= self.window] + [now]
            if len(self._failures) >= self.threshold:
                self.state, self._opened_at, self._probing = "open", now, False

    def release(self) -> None:
        """A probe that ended in something that says nothing about the service (a 404): let the
        next caller probe instead of leaving the breaker waiting for a result that never comes."""
        with self._lock:
            self._probing = False


BREAKERS: dict[str, CircuitBreaker] = {}
_REG = threading.Lock()


def breaker(family: str) -> CircuitBreaker:
    with _REG:
        if family not in BREAKERS:
            BREAKERS[family] = CircuitBreaker(
                family, threshold=int(os.getenv("VERIFY_BREAKER_FAILURES", "3")),
                cooldown=float(os.getenv("VERIFY_BREAKER_COOLDOWN_SEC", "60")))
        return BREAKERS[family]


def guarded(family: str, fn):
    """Run one document-level read through its family's breaker. Transient failure counts toward
    opening it; a 404/403 says nothing about the service and does not."""
    b = breaker(family)
    b.before()
    try:
        out = fn()
    except Exception as exc:
        if classify(exc).transient:
            b.failure()
        else:
            b.release()
        raise
    b.success()
    return out


def retry_delay(attempts: int, base: float = 120.0, cap: float = 1800.0) -> float:
    """How long a document waits before its next pass: 2, 4, 8 ... minutes, capped."""
    return min(cap, base * (2 ** max(attempts - 1, 0)))


def _iso(offset: float = 0.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset))


class UnverifiedStore:
    """verification_unverified in the account's ledger. Every write is one transaction."""

    def __init__(self, db):
        self.db = db

    def record(self, source_user: str, service: str, source_id: str, target_id: str, mime: str,
               failure: Failure) -> int:
        """Insert or bump a row; returns its attempt count. Attempts accumulate across runs."""
        now = _iso()
        with self.db.write() as c:
            row = c.execute("SELECT attempts FROM verification_unverified WHERE source_user=? AND service=? "
                            "AND source_id=?", (source_user, service, source_id)).fetchone()
            attempts = (row[0] if row else 0) + max(failure.attempts, 1)
            nxt = _iso(retry_delay(attempts)) if failure.transient else None
            c.execute(
                "INSERT INTO verification_unverified (source_user, service, source_id, target_id, mime, state,"
                " http_status, endpoint, error, attempts, first_seen, last_attempt, next_retry_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(source_user, service, source_id) DO UPDATE SET target_id=excluded.target_id,"
                " mime=excluded.mime, state=excluded.state, http_status=excluded.http_status,"
                " endpoint=excluded.endpoint, error=excluded.error, attempts=excluded.attempts,"
                " last_attempt=excluded.last_attempt, next_retry_at=excluded.next_retry_at",
                (source_user, service, source_id, target_id, mime, failure.state, failure.status,
                 failure.endpoint, failure.detail, attempts, now, now, nxt))
        return attempts

    def resolve(self, source_user: str, service: str, source_id: str) -> None:
        """A comparison really happened: the row goes."""
        with self.db.write() as c:
            c.execute("DELETE FROM verification_unverified WHERE source_user=? AND service=? AND source_id=?",
                      (source_user, service, source_id))

    def pending(self, source_user: str, service: str | None = None, due_only: bool = False) -> list[dict]:
        q = "SELECT * FROM verification_unverified WHERE source_user=?"
        a: list = [source_user]
        if service:
            q += " AND service=?"; a.append(service)
        if due_only:
            q += " AND state=? AND (next_retry_at IS NULL OR next_retry_at <= ?)"; a += [UNVERIFIED_TRANSIENT, _iso()]
        return [dict(r) for r in self.db.conn.execute(q + " ORDER BY first_seen", a)]

    def counts(self, source_user: str) -> dict[str, int]:
        return {r[0]: r[1] for r in self.db.conn.execute(
            "SELECT state, COUNT(*) FROM verification_unverified WHERE source_user=? GROUP BY state",
            (source_user,))}
