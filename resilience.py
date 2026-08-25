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
import re
import threading
import time
from typing import Callable, TypeVar

import http.client
import socket
import ssl

from googleapiclient.errors import HttpError

# Imported as a module, not `from metrics import METRICS`. A by-value
# binding means a test (or anything else) that swaps the collector has to
# patch this module's global as well as metrics' own -- and the next
# module to import it silently measures into the old collector if anyone
# forgets. One indirection removes a whole class of that.
import metrics

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

# A 400 is permanent by default, and that is right: a malformed request does
# not improve on retry. This one is the exception, and it is the same
# freshly-created-account lag already documented below for 401 impersonation
# -- Drive's sharing check does not see a new Workspace account for some
# minutes after the Directory API reports it created.
#
# Measured on a from-scratch run into a tenant provisioned two hours earlier:
# 134 grants refused this way, every one naming an account that existed. As
# a permanent 400 they were never retried, so each became a failed grant that
# the post-run repair had to verify against the directory and resolve -- work
# that only exists because the original call gave up immediately.
#
# Matched on the stable fragment: Drive writes "no Google accountS ... these
# email addressES" for several grantees and "no Google account ... this email
# address" for one.
TRANSIENT_400_FRAGMENTS = ("no google account",)

# A 403 insufficientPermissions is permanent by default and usually should
# be: it is how Drive says a user may not touch a file, and retrying that
# six times per file wastes the quota real work needs.
#
# Google sends TWO different things under that one reason, and only one of
# them is a denial:
#
#   "The user does not have sufficient permissions for this file"
#       -- a real denial. Permanent, unchanged.
#   "Request had insufficient authentication scopes"
#       -- the TOKEN, not the file. Seen intermittently on a run whose
#          scope_guard reported every required scope authorised on both
#          tenants, and on files whose owner could export them perfectly
#          when the same call was repeated by hand minutes later. It tracks
#          the credential refreshes visible in the same logs.
#
# 87 files were lost to the second kind on one run -- every copy strategy
# "failed", none retried, no mapping written. The blast radius of being
# wrong here is bounded: a genuinely missing scope still fails after the
# retries, and scope_guard refuses the run before it starts.
TRANSIENT_403_MESSAGES = ("insufficient authentication scopes",)

# A freshly created Workspace account is not always immediately ready to be
# impersonated over domain-wide delegation -- confirmed live on
# seeduser382@source.rohitrokaya.com.np, created by create_until_full
# moments earlier, which failed its very first Drive call this way despite
# already being created with changePasswordAtNextLogin=False (the *other*,
# permanent cause of this exact message -- see provision.py's docstring).
# A 401 is normally treated as permanent (a bad/expired credential retrying
# will never fix), so this carve-out is narrowed to that one literal message
# rather than retrying every 401 -- an actually-revoked or never-delegated
# credential returns the same string and simply exhausts max_retries instead
# of hanging forever, surfacing as a failure the same as it always did.
_ACTIVE_SESSION_INVALID = "active session is invalid"


def _is_transient_401(reason: str, exc: HttpError) -> bool:
    return reason == "authError" and _ACTIVE_SESSION_INVALID in str(exc).lower()


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


def _service_disabled_hint(exc: Exception, reason: str) -> str:
    """Turn Google's SERVICE_DISABLED wall of text into the one line that fixes it.

    Google's own message is technically complete and practically useless: it
    quotes a project *number* ("project 881431668245"), which nobody
    recognises, buries the API name in a URL, and says nothing about the
    distinction that actually matters here -- that this is API enablement in
    the Cloud console, NOT a domain-wide delegation scope. That confusion cost
    this project a full seeding run: DWD showed 17/17 scopes live on the
    source while People and Tasks were never enabled on the project, so
    contacts and tasks produced nothing and the run reported success.

    Best-effort and additive: if the shape of the error changes, the original
    message is still there in full.
    """
    if reason not in ("SERVICE_DISABLED", "accessNotConfigured"):
        return ""
    blob = str(exc)
    api = re.search(r"([a-z0-9-]+\.googleapis\.com)", blob)
    proj = re.search(r"projects?[/ =]([A-Za-z0-9-]+)", blob)
    api_name = api.group(1) if api else "the API"
    project = proj.group(1) if proj else "<project>"
    return (f"\n  This is Cloud API ENABLEMENT, not a DWD scope -- a granted "
            f"scope does not switch the API on.\n"
            f"  Fix:  gcloud services enable {api_name} --project={project}\n"
            f"  Then: python3 ensure_apis.py --tenant <source|target>  "
            f"(re-checks, and can enable when permitted)")


def _is_transient_400(exc: HttpError | None) -> bool:
    """Is this 400 the freshly-created-account lag rather than a bad request?"""
    if exc is None:
        return False
    blob = str(exc).lower()
    return any(f in blob for f in TRANSIENT_400_FRAGMENTS)


def _is_transient_403(exc: HttpError | None) -> bool:
    """Is this 403 about the token rather than about the file?"""
    if exc is None:
        return False
    blob = str(exc).lower()
    return any(f in blob for f in TRANSIENT_403_MESSAGES)


def _is_permanent(status: int, reason: str, exc: HttpError | None = None) -> bool:
    if status == 403:
        if _is_transient_403(exc):
            return False
        return reason not in TRANSIENT_403_REASONS
    if status == 401 and exc is not None and _is_transient_401(reason, exc):
        return False
    if status == 400 and _is_transient_400(exc):
        return False
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
    label: str | None = None,
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
                # Timed here because every Google call in the engine passes
                # through this decorator, so instrumenting it needs no change
                # at any call site -- and a call site that forgot to
                # instrument itself would silently skew the distribution the
                # concurrency work is going to be sized against.
                started = time.monotonic()
                try:
                    result = fn(*args, **kwargs)
                    metrics.METRICS.record(label or "api", time.monotonic() - started,
                                   ok=True, retried=attempt > 0)
                    return result
                except HttpError as exc:
                    metrics.METRICS.record(label or "api", time.monotonic() - started,
                                   ok=False)
                    status = _status_of(exc)
                    reason = _extract_reason(exc)
                    if _is_permanent(status, reason, exc):
                        raise PermanentAPIError(
                            f"HTTP {status} ({reason or 'unknown reason'}): {exc}"
                            + _service_disabled_hint(exc, reason)
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
                    metrics.METRICS.record(label or "api", time.monotonic() - started,
                                   ok=False)
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

    def acquire(self, cost: float = 1.0) -> None:
        """Take `cost` tokens, waiting until they are available.

        `cost` exists because a batched API call is one round trip and many
        operations, and the quota counts the operations. A 20-grant Drive
        permissions batch charged as a single token let the real rate run 20x
        over whatever this was configured to allow -- which is why a limiter
        that was working correctly by its own reckoning still produced
        hundreds of thousands of quota rejections.

        Deliberately allowed to exceed `capacity`: a batch may legitimately
        cost more than the bucket's burst, and refusing it (or silently
        charging less) would put the cap back exactly where it was.
        """
        cost = max(float(cost), 0.0)
        if cost == 0.0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                # Ceiling is max(capacity, cost), not capacity: a batch may
                # cost more than the bucket's burst, and capping accumulation
                # at `capacity` there means the balance can never reach the
                # cost and acquire() spins forever. Found by hanging.
                self._tokens = min(max(self.capacity, cost),
                                   self._tokens + elapsed * self.rate)
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                wait = (cost - self._tokens) / self.rate
            time.sleep(wait)


class AdaptiveRateLimiter(RateLimiter):
    """A bucket that finds the real ceiling instead of being told it.

    The fixed limiter had to be handed a number, and every number was a
    guess about someone else's project. Guessing low is invisible -- it
    just runs slow forever, and nothing in the logs says the quota was
    never the binding constraint. Guessing high produced the 127k failed
    ACL run. Neither error announces itself, which is exactly why this
    should not be a constant.

    So: additive increase, multiplicative decrease -- the same control law
    TCP uses on a link whose capacity it cannot ask about, for the same
    reason. Climb by `step` each clean interval, halve the moment Google
    says the word "quota", and never leave [floor, ceiling].

    Multiplicative decrease is what makes overshoot cheap. Backing off by
    a step would spend many more rejections walking down from a rate that
    is already too high, and every one of those is a real failed
    operation on someone's tenant, not a retry counter.

    The floor matters as much as the ceiling: a burst of 429s from a
    genuinely unrelated cause must not be able to drive the rate to zero
    and wedge the migration.
    """

    def __init__(self, rate_per_sec: float, *, floor: float, ceiling: float,
                 step: float | None = None, growth: float = 0.10,
                 probe_after: float = 20.0,
                 burst: int = 1, on_change=None, decrease: float = 0.7):
        super().__init__(rate_per_sec, burst=burst)
        self.floor = max(float(floor), 0.001)
        self.ceiling = max(float(ceiling), self.floor)
        # Proportional, not a fixed increment.
        #
        # A flat +2/sec needs 380 probes -- over two hours at a 20s interval
        # -- to walk from 40 to 800, so a migration measured in hours would
        # spend most of itself below a rate it could have sustained the whole
        # time. Slow convergence on a controller is not a safety property; it
        # is just a slower way to arrive at the same place.
        #
        # A fraction of the current rate climbs geometrically where there is
        # obvious headroom and takes small steps near the top, which is the
        # shape wanted in both regions. `step`, when given, pins it flat --
        # the tests use that to assert exact arithmetic.
        self.step = float(step) if step is not None else None
        self.growth = max(float(growth), 0.0)
        self.probe_after = float(probe_after)
        # Bounded: >= 1 would never back off, <= 0 would stall the migration.
        self.decrease = min(max(float(decrease), 0.05), 0.95)
        self._on_change = on_change
        self.rate = min(max(self.rate, self.floor), self.ceiling)
        self._last_change = time.monotonic()
        self._rejections = 0
        self._backoffs = 0

    def penalise(self) -> float:
        """Called when the service rejected a call for quota.

        Backs off by `decrease`, not by half.

        Halving is TCP's factor, chosen where a dropped packet may mean the
        path is collapsing and overshoot is expensive. Here a 403
        rateLimitExceeded is retried and lands: measured across 41 hours of a
        live run, 5,050 of them produced zero failed items. Overshoot costs a
        retry; undershoot costs throughput -- and halving pays the expensive
        one to avoid the cheap one.

        What that looked like: 1,281 backoffs, one every two minutes, each
        halving a rate that had just been shown sustainable. Climbing back
        from 40 to 80/s takes about seven probes at 20-second intervals, so
        the limiter spent most of its life below a rate it had already
        proven, and 17 of 201 users finished in two days.

        0.7 keeps the multiplicative decrease that makes AIMD stable while
        cutting recovery to roughly three probes. Still multiplicative, so a
        genuinely over-driven rate still collapses quickly: three
        consecutive rejections take it to a third.
        """
        with self._lock:
            self._rejections += 1
            before = self.rate
            self.rate = max(self.floor, self.rate * self.decrease)
            self._last_change = time.monotonic()
            if self.rate < before:
                self._backoffs += 1
            changed = self.rate != before
        if changed and self._on_change:
            self._on_change("backoff", before, self.rate)
        return self.rate

    def acquire(self, cost: float = 1.0) -> None:
        # Probe upward only from inside the wait path, so an idle process
        # never drifts its rate up on the strength of having done nothing.
        with self._lock:
            if (self.rate < self.ceiling
                    and time.monotonic() - self._last_change >= self.probe_after):
                before = self.rate
                inc = (self.step if self.step is not None
                       else max(1.0, self.rate * self.growth))
                self.rate = min(self.ceiling, self.rate + inc)
                self._last_change = time.monotonic()
                grew = (before, self.rate)
            else:
                grew = None
        if grew and self._on_change:
            self._on_change("probe", *grew)
        super().acquire(cost)

    def stats(self) -> dict:
        with self._lock:
            return {"rate": round(self.rate, 2), "floor": self.floor,
                    "ceiling": self.ceiling, "rejections": self._rejections,
                    "backoffs": self._backoffs, "decrease": self.decrease}


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
