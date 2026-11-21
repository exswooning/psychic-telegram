"""pass_retry.py -- what an ordered run does with a pass's failures before it starts the next pass.

A failed item is sorted by its own words, with the classifiers that already exist (repair.py's family
patterns, resilience's rate-limit reasons, known_issues.RUN_KINDS):

  transient    a backend hiccup, a session that lapsed, a scope not yet propagated: retried now, once a round
  quota        Google's pacing or a daily cap: wait (PASS_RETRY_QUOTA_WAIT_SEC, bounded), then retried
  explained    a kind known_issues names (cause and fix on record, automatic or accepted): left alone
  unexplained  nobody has named it: that user is held (their mail and calendar do not follow a failed
               Drive, as with any failed Drive pass), the run goes on, and an incident opens

A round retries the users who still hold a transient or quota failure by re-running this pass's services
for them (the engines skip what is mapped, so only the failures are tried again). Two rounds in a row that
leave as many failures as they found stop the retrying and open an incident: the run itself goes on.
Nothing here raises into the run; it is advisory, like main._repair_between_passes.
"""
from __future__ import annotations

import copy
import logging
import os
import time

import known_issues
import repair

log = logging.getLogger("pass_retry")

# The item types a pass's services write, for counting its failures (main.SERVICE_EVIDENCE plus what a Drive
# user also writes: shortcuts, sharing grants, comments).
PASS_TYPES = {
    "drive": ("file", "folder", "shortcut", "acl", "comment"),
    "gmail": ("message", "label", "draft"),
    "calendar": ("event", "calendar"),
    "contacts": ("contact", "contact_group"),
    "tasks": ("task",),
    "chat": ("chat_space", "chat_message"),
}

QUOTA_WORDS = (repair.QUOTA, "rateLimitExceeded", "userRateLimitExceeded", "OVER_DAILY_CAP", "User-rate limit")
TRANSIENT_WORDS = (repair.BACKEND_5XX, repair.SESSION_INVALID, repair.SCOPE_403, repair.INVALID_LABEL,
                   "Internal error", "Service Unavailable", "timed out", "Connection reset")

# Named by repair.py as permanent or repaired elsewhere: explained, not retried by a pass.
EXPLAINED_WORDS = (repair.NO_ACCOUNT, repair.ORGANIZER_ROLE, repair.CONTACT_DUPLICATE_NAMES)

MAX_ROUNDS = int(os.getenv("PASS_RETRY_ROUNDS", "4"))
QUOTA_WAIT_SEC = int(os.getenv("PASS_RETRY_QUOTA_WAIT_SEC", "900"))
STALL_ROUNDS = 2


# known_issues kinds whose own fix is "run it again" (its `fix` text says so): retried here, not left.
RERUN_KINDS = {"run.backend_500", "run.coverage_incomplete"}


def classify(item_type: str, text: str | None) -> str:
    t = text or ""
    kind = known_issues.classify_failure(item_type, t)
    if any(w in t for w in QUOTA_WORDS):
        return "quota"
    if kind in RERUN_KINDS or any(w in t for w in TRANSIENT_WORDS):
        return "transient"
    if kind != "unclassified" or any(w in t for w in EXPLAINED_WORDS):
        return "explained"
    return "unexplained"


def failures(db, users: list[str], services) -> dict[str, list[tuple[str, str]]]:
    """{class: [(user, error text)]} for this pass's users still holding a FAILED row of its services."""
    types = sorted({t for s in services for t in PASS_TYPES.get(s, ())})
    out: dict[str, list[tuple[str, str]]] = {"transient": [], "quota": [], "explained": [], "unexplained": []}
    if not users or not types:
        return out
    marks = ",".join("?" for _ in users)
    tmarks = ",".join("?" for _ in types)
    for r in db.conn.execute(
            f"SELECT source_user, item_type, error_message FROM audit_log WHERE status='FAILED' "
            f"AND source_user IN ({marks}) AND item_type IN ({tmarks})", (*users, *types)).fetchall():
        out[classify(r["item_type"], r["error_message"])].append((r["source_user"], r["error_message"] or ""))
    return out


def _incident(settings, kind: str, title: str, summary: str, fingerprint: str) -> None:
    try:
        import run_watch
        run_watch.open_incident(kind=kind, title=title, summary=summary, severity="warning",
                                account_id=getattr(settings, "account_id", None), job_name="migrate",
                                fingerprint=fingerprint)
    except Exception as exc:      # noqa: BLE001 - an incident that cannot be written must not stop the run
        log.warning("could not open the %s incident: %s", kind, str(exc)[:160])


def _wait(seconds: float, stop) -> None:
    end = time.time() + seconds
    while time.time() < end and not stop():
        time.sleep(min(5.0, max(0.0, end - time.time())))


def end_of_pass(db, auth, settings, services, pass_results: list[dict], migrate_user, *,
                stop=lambda: False, wait=None) -> dict:
    """Retry by class, hold the unexplained, report. `migrate_user` is main.migrate_user (passed in: main imports
    this module). Returns what it did, for the log and the tests."""
    wait = wait or _wait
    done = {"rounds": 0, "retried": 0, "held": [], "stalled": False, "left": {}}
    if getattr(settings, "dry_run", False) or not getattr(settings, "auto_repair", True):
        return done
    pairs = {r["source"]: r.get("target") for r in pass_results if r.get("source") and r.get("target")}
    users = sorted(pairs)
    st = copy.copy(settings)             # a re-run for stragglers, not a migration: no acceptance check again
    st.first_pass_acceptance = False
    st.verify_on_complete = False
    st.tally_on_complete = False
    still = None
    flat = 0
    try:
        while done["rounds"] < MAX_ROUNDS and not stop():
            f = failures(db, users, services)
            retry_users = sorted({u for c in ("transient", "quota") for u, _ in f[c]})
            if not retry_users:
                break
            n = len(f["transient"]) + len(f["quota"])
            flat = flat + 1 if still is not None and n >= still else 0
            still = n
            if flat >= STALL_ROUNDS:
                done["stalled"] = True
                _incident(settings, "pass_retry_stalled", f"{n} failed item(s) of the {'/'.join(sorted(services))} pass "
                          f"did not move in {STALL_ROUNDS} retry rounds",
                          f"{len(retry_users)} user(s) still hold transient or quota failures after {done['rounds']} "
                          f"round(s): {', '.join(retry_users[:10])}. The run goes on; a person reads them.",
                          f"pass_retry_stalled:{','.join(sorted(services))}")
                break
            if f["quota"]:
                log.info("pass retry: %d quota failure(s); waiting up to %ds for the limit to reset", len(f["quota"]), QUOTA_WAIT_SEC)
                wait(QUOTA_WAIT_SEC, stop)
            done["rounds"] += 1
            log.info("pass retry round %d: %d transient, %d quota failure(s) for %d user(s)",
                     done["rounds"], len(f["transient"]), len(f["quota"]), len(retry_users))
            for u in retry_users:
                if stop():
                    break
                try:
                    migrate_user(auth, db, st, u, pairs[u], set(services), delta=False, delta_days=0)
                    done["retried"] += 1
                except Exception as exc:      # noqa: BLE001 - report, never abort the run
                    log.warning("pass retry of %s failed: %s", u, str(exc)[:160])
        f = failures(db, users, services)
        done["left"] = {k: len(v) for k, v in f.items() if v}
        unexplained = sorted({u for u, _ in f["unexplained"]})
        if unexplained:
            done["held"] = unexplained
            if "drive" in services:      # the same hold a failed Drive pass puts on a user's mail and calendar
                held = {x for x in os.environ.get("DRIVE_FAILED_USERS", "").split(",") if x} | set(unexplained)
                os.environ["DRIVE_FAILED_USERS"] = ",".join(sorted(held))
            import run_watch
            shape = run_watch.normalise(f["unexplained"][0][1])
            _incident(settings, "pass_unexplained",
                      f"{len(unexplained)} user(s) hold failures no known kind explains ({'/'.join(sorted(services))} pass)",
                      f"Held and passed over: {', '.join(unexplained[:10])}. First error: {f['unexplained'][0][1][:300]}",
                      f"pass_unexplained:{','.join(sorted(services))}:{shape}")
    except Exception as exc:      # noqa: BLE001 - advisory; must never break the run
        log.warning("end-of-pass retry skipped: %s", str(exc)[:200])
    log.info("pass retry done: %s", done)
    return done
