"""
resilience.py
=============
Retry/backoff, rate limiting, and the persisted daily upload quota guard.

403 is not one error
---------------------
`rateLimitExceeded`, `userRateLimitExceeded` and `quotaExceeded` are transient
and must be retried. `insufficientPermissions`, `storageQuotaExceeded`,
`cannotDownloadFile`, `domainPolicy`, and any reason we don't recognise are
permanent — retrying them burns quota and hides real bugs behind a wall of log
noise. We branch on the `reason` field inside the error body, not the HTTP
status code, because the status code alone (403) cannot tell these apart.

Full jitter, not partial
------------------------
Delay is `random.uniform(0, base * 2**n)`. When many threads collide on the
same quota bucket, partial jitter (e.g. `base/2 + random.uniform(0, base/2)`)
leaves them synchronised enough to re-collide on the next attempt.
"""

from __future__ import annotations

import functools
import json
import logging
import random
import threading
import time
from typing import Callable, TypeVar

from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

T = TypeVar("T")


class PermanentAPIError(Exception):
    """Raised for errors retrying will never fix. Callers should give up."""


class QuotaExhausted(Exception):
    """Raised when a reservation would exceed the daily per-user upload cap."""


# ======================================================================
# Retry / backoff
# ======================================================================
TRANSIENT_403_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _extract_reason(exc: HttpError) -> str:
    try:
        content = exc.content
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        body = json.loads(content)
        errors = (body.get("error") or {}).get("errors") or []
        if errors and errors[0].get("reason"):
            return errors[0]["reason"]
        return (body.get("error") or {}).get("status", "") or ""
    except Exception:  # noqa: BLE001 - malformed error bodies happen
        return ""


def _status_of(exc: HttpError) -> int:
    try:
        return int(exc.resp.status)
    except Exception:  # noqa: BLE001
        return 0


def _is_permanent(status: int, reason: str) -> bool:
    if status == 403:
        return reason not in TRANSIENT_403_REASONS
    if status in RETRYABLE_STATUSES:
        return False
    return True


def _retry_after_seconds(exc: HttpError) -> float | None:
    resp = getattr(exc, "resp", None)
    if resp is None:
        return None
    val = resp.get("retry-after") or resp.get("Retry-After")
    if not val:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def retry_on_google_error(
    max_retries: int = 6, base_delay: float = 1.0, max_delay: float = 60.0
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator: retry transient Google API failures with full-jitter exponential
    backoff; raise `PermanentAPIError` immediately for anything that will never
    succeed on retry; raise `RuntimeError` once retries are exhausted.
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            attempt = 0
            while True:
                try:
                    return fn(*args, **kwargs)
                except HttpError as exc:
                    status = _status_of(exc)
                    reason = _extract_reason(exc)
                    if _is_permanent(status, reason):
                        raise PermanentAPIError(
                            f"HTTP {status} ({reason or 'unknown reason'}): {exc}"
                        ) from exc

                    attempt += 1
                    if attempt > max_retries:
                        raise RuntimeError(
                            f"exhausted {max_retries} retries on HTTP {status} "
                            f"({reason or 'unknown reason'}): {exc}"
                        ) from exc

                    retry_after = _retry_after_seconds(exc)
                    if retry_after is not None:
                        delay = min(retry_after, max_delay)
                    else:
                        delay = random.uniform(
                            0, min(max_delay, base_delay * (2 ** (attempt - 1)))
                        )
                    log.debug(
                        "retrying after HTTP %s (%s): attempt %d/%d in %.2fs",
                        status, reason, attempt, max_retries, delay,
                    )
                    time.sleep(delay)

        return wrapper

    return decorator


# ======================================================================
# Rate limiting — a thread-safe token bucket, one per (user, service).
# ======================================================================
class RateLimiter:
    def __init__(self, rate_per_sec: float, burst: int = 1):
        self.rate = max(float(rate_per_sec), 0.001)
        self.capacity = max(int(burst), 1)
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            time.sleep(wait)


# ======================================================================
# Daily upload quota guard — persisted, so a process restart does not
# forget how much of the 750 GB/day cap has already been spent.
# ======================================================================
class DailyQuotaGuard:
    def __init__(self, db, target_user: str, cap_bytes: int):
        self.db = db
        self.target_user = target_user
        self.cap_bytes = cap_bytes
        self._lock = threading.Lock()

    def remaining(self) -> int:
        return max(0, self.cap_bytes - self.db.bytes_sent_today(self.target_user))

    def reserve(self, n: int) -> None:
        with self._lock:
            remaining = self.remaining()
            if n > remaining:
                raise QuotaExhausted(
                    f"{self.target_user}: {n:,} bytes exceeds remaining "
                    f"{remaining:,} of {self.cap_bytes:,} daily cap"
                )
            self.db.add_bytes_sent(self.target_user, n)

    def refund(self, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self.db.add_bytes_sent(self.target_user, -n)
