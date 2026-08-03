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

import http.client
import socket
import ssl

from googleapiclient.errors import HttpError

# Transient failures that are not HttpError. Every one of these was observed
# permanently failing an item on a multi-hour run, because the retry decorator
# only ever caught HttpError.
#
# google.auth is imported defensively: it is a hard dependency of the client,
# but this module must not fail to import if that ever changes.
try:  # pragma: no cover - exercised implicitly by every real run
    from google.auth.exceptions import TransportError as _GoogleTransportError

    _AUTH_ERRORS: tuple[type[BaseException], ...] = (_GoogleTransportError,)
except Exception:  # noqa: BLE001
    _AUTH_ERRORS = ()

TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,          # covers ConnectionReset/Aborted/Refused, BrokenPipe
    socket.timeout,
    socket.gaierror,          # DNS blips resolve as a failed item otherwise
    ssl.SSLError,
    http.client.IncompleteRead,
    http.client.BadStatusLine,
    http.client.ResponseNotReady,
) + _AUTH_ERRORS

log = logging.getLogger(__name__)

T = TypeVar("T")


class PermanentAPIError(Exception):
    """Raised for errors retrying will never fix. Callers should give up."""


class TransportExhausted(RuntimeError):
    """
    The connection failed repeatedly and we stopped trying.

    Distinct from a plain RuntimeError because the *uncertainty* differs. An
    API that returned 500 five times told us five times that it did not do the
    thing. A socket that reset mid-write told us nothing: the write may have
    landed and only the response was lost. Anything whose duplicate a user
    would see -- a second copy of an email, most obviously -- needs to check
    before assuming it can simply try again.

    Subclasses RuntimeError so every existing handler keeps working unchanged.
    """


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


def _ask_hook(before_retry):
    """
    Run a before_retry hook without letting it become a new failure mode.

    The hook makes an API call, and it is invoked from inside an exception
    handler at the exact moment the network is known to be unwell -- which is
    the state that got us here. An unguarded call would let a second failure
    propagate out of the handler and kill the whole retry, and it would do so
    precisely when a copy of the work may already exist on the server: the
    case the hook was added to make safer.

    So a failing hook degrades to "we could not check", and the retry proceeds
    on the original terms. That accepts the duplicate risk rather than
    converting it into a certain hard failure, which is the trade this
    codebase makes everywhere else.
    """
    if before_retry is None:
        return None
    try:
        return before_retry()
    except Exception as exc:  # noqa: BLE001 - a hook must never fail the retry
        log.debug("before_retry hook failed, proceeding with the retry: %s", exc)
        return None


def retry_on_google_error(
    max_retries: int = 6, base_delay: float = 1.0, max_delay: float = 60.0,
    before_retry: Callable[[], T | None] | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator: retry transient Google API failures with full-jitter exponential
    backoff; raise `PermanentAPIError` immediately for anything that will never
    succeed on retry; raise `RuntimeError` once retries are exhausted.

    before_retry
    ------------
    Called between a failure and the next attempt, but *only* when the failure
    left it genuinely unknown whether the call landed: a transport error, or a
    5xx. If it returns a non-None value, that value is returned instead of
    re-executing.

    This exists because the dangerous case for a non-idempotent write is not
    the one that raises. It is:

        attempt 1   lands server-side, response lost to a reset socket
        attempt 2   performs the write a SECOND time, returns 200
        decorator   reports success; nothing raises; nobody is any the wiser

    A guard wrapped around the decorator cannot see that -- it only runs when
    every attempt failed, which is precisely the case where the write most
    likely did *not* land. The check has to happen before each retry, which
    means it has to live in here.

    Not called for 429/rate-limit or 403 quota failures: those mean the
    request was rejected before it was processed, so there is nothing to
    adopt and a lookup would just spend quota confirming it.
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
                    # A 5xx may have been processed before the error was
                    # generated; a 429 was rejected before it was.
                    if status is not None and status >= 500:
                        adopted = _ask_hook(before_retry)
                        if adopted is not None:
                            return adopted

                except TRANSPORT_ERRORS as exc:
                    # A multi-hour migration reliably sees connections reset,
                    # sockets time out and TLS renegotiate. None of these are
                    # HttpError, so every one of them used to permanently fail
                    # an item and cost a re-run to recover.
                    #
                    # The honest trade-off: a transport error raised mid-write
                    # may mean the call actually succeeded and the response was
                    # lost, so retrying can duplicate. That risk is not new --
                    # retrying a 500 on files.create has always carried it --
                    # and it is bounded by the same thing: id_mapping is
                    # written only after a confirmed create, so a duplicate
                    # shows up as an extra item, never as a lost one. Given the
                    # choice this codebase has made everywhere else, an
                    # occasional duplicate beats an item that silently is not
                    # there.
                    attempt += 1
                    if attempt > max_retries:
                        # A distinct type, subclassing RuntimeError so every
                        # existing `except RuntimeError` still catches it.
                        # Callers that need to tell "the network died, and the
                        # write may or may not have landed" apart from "the
                        # API refused this" can now do so -- gmail_engine uses
                        # it to check whether a message arrived before
                        # retrying and duplicating it.
                        raise TransportExhausted(
                            f"exhausted {max_retries} retries on "
                            f"{type(exc).__name__}: {exc}"
                        ) from exc
                    delay = random.uniform(
                        0, min(max_delay, base_delay * (2 ** (attempt - 1)))
                    )
                    log.debug("retrying after %s: attempt %d/%d in %.2fs",
                              type(exc).__name__, attempt, max_retries, delay)
                    time.sleep(delay)
                    adopted = _ask_hook(before_retry)
                    if adopted is not None:
                        return adopted

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
