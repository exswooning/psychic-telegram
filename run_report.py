"""
run_report.py -- one saved report per run, judged against benchmarks.

The tool could already answer most of "how did it go" -- verify.py reconciles
the tenants, benchmark_run.judge() gates a rehearsal, repair.survey() groups
the failures -- but each answered on a terminal and was gone. The Final Report
tab showed a handful of counts and a "verification success rate" that was
just the share of users not marked FAILED. This gathers what the ledger
knows into one document per run, keeps it, and judges it (benchmarks.py).

Two audiences, one set of facts. The human report leads with the verdict and
what to do. The Claude report leads with what a fix needs: the failing
benchmarks with their exact values, the error families with real messages and
the users they hit, the configuration and environment, and the tail of the
run's own log.

Every section is gathered independently and a failure in one is recorded in
`errors`, never swallowed and never fatal: a report missing its metrics is
still worth having, and saying so is better than not producing one.
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone

import benchmarks
from config import DEFERRED_TO_DMS

HERE = os.path.dirname(os.path.abspath(__file__))

# The id is a filename. A route that accepts one from a URL must not be able
# to walk out of the reports directory, so it is matched, never sanitised.
RUN_ID_RE = re.compile(r"^(migration|seed)-\d{8}T\d{6}Z$")
TRANSCRIPT_LINES = 80

CONFIG_KEYS = (
    "source_domain", "target_domain", "auth_mode", "transfer_mode", "user_workers",
    "drive_file_workers", "max_retries", "migrate_comments", "migrate_secondary_calendars",
    "migrate_calendar_acls", "migrate_gmail_settings",
)


def reports_dir(account_id) -> str:
    return os.path.join(HERE, "logs", "reports", str(account_id) if account_id else "legacy")


def report_file(account_id, run_id: str, ext: str) -> str:
    if not RUN_ID_RE.match(run_id or ""):
        raise ValueError(f"not a report id: {run_id!r}")
    if ext not in ("json", "human.pdf", "claude.pdf"):
        raise ValueError(f"not a report file type: {ext!r}")
    return os.path.join(reports_dir(account_id), f"{run_id}.{ext}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_run_id(kind: str, when: datetime | None = None) -> str:
    return f"{kind}-{(when or _now()).strftime('%Y%m%dT%H%M%SZ')}"


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------
def _guard(errors: list, section: str, fn, default):
    try:
        return fn()
    except Exception as exc:      # noqa: BLE001 - recorded, not swallowed
        errors.append({"section": section, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        return default


def _users(conn) -> dict:
    by = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM identity_map WHERE entity_type='user' GROUP BY status")}
    total = sum(by.values())
    failed = by.get("FAILED", 0)
    return {"total": total, "done": by.get("DONE", 0), "failed": failed,
            "running": by.get("RUNNING", 0), "pending": by.get("PENDING", 0),
            # None, not 0, for an empty ledger: "no users" must not pass a
            # benchmark that asks how many failed.
            "failedShare": (failed / total) if total else None}


def _ledger(conn) -> dict:
    # audit_rollup holds the counts of SUCCESS rows pruned for finished users;
    # counting audit_log alone would read a pruned user as having moved nothing.
    try:
        rows = conn.execute(
            "SELECT item_type, status, SUM(n) n FROM ("
            " SELECT item_type, status, COUNT(*) n FROM audit_log GROUP BY item_type, status"
            " UNION ALL"
            " SELECT item_type, status, SUM(n) n FROM audit_rollup GROUP BY item_type, status"
            ") GROUP BY item_type, status").fetchall()
    except Exception:      # noqa: BLE001 - a ledger from before rollups
        rows = conn.execute(
            "SELECT item_type, status, COUNT(*) n FROM audit_log "
            "GROUP BY item_type, status").fetchall()
    by_type: dict[str, dict] = {}
    tot = {"succeeded": 0, "failed": 0, "blocked": 0, "skipped": 0, "inProgress": 0, "deferred": 0}
    for r in rows:
        st, n = r["status"] or "", r["n"] or 0
        bucket = ("succeeded" if st == "SUCCESS" else "failed" if st == "FAILED"
                  else "blocked" if st == "BLOCKED" else "inProgress" if st == "IN_PROGRESS"
                  # Left for the DMS: owed, not declined. Counted apart so "skipped on
                  # purpose" never includes mail the target does not have yet.
                  else "deferred" if st == DEFERRED_TO_DMS
                  else "skipped")
        t = by_type.setdefault(r["item_type"], {"succeeded": 0, "failed": 0, "blocked": 0,
                                                "skipped": 0, "inProgress": 0, "deferred": 0})
        t[bucket] += n
        tot[bucket] += n
    # "Attempts" excludes deliberate skips (a decision, not a failure) and
    # anything still in flight.
    attempts = tot["succeeded"] + tot["failed"] + tot["blocked"]
    return {**tot, "attempts": attempts,
            "failureRate": ((tot["failed"] + tot["blocked"]) / attempts) if attempts else None,
            "byType": by_type}


def _failure_families(conn) -> list[dict]:
    return [{"itemType": r["item_type"], "message": r["msg"], "count": r["n"], "users": r["users"]}
            for r in conn.execute(
                "SELECT item_type, substr(COALESCE(error_message,'(no message)'),1,160) msg,"
                " COUNT(*) n, COUNT(DISTINCT source_user) users FROM audit_log"
                " WHERE status IN ('FAILED','BLOCKED') GROUP BY item_type, msg"
                " ORDER BY n DESC LIMIT 15")]


def _metrics(conn) -> dict | None:
    row = conn.execute("SELECT recorded_at, payload FROM run_metrics ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    m = json.loads(row["payload"])
    calls, retries = m.get("calls"), m.get("retries")
    limiters = m.get("limiters") or {}
    return {
        "recordedAt": row["recorded_at"], "calls": calls, "retries": retries,
        "failures": m.get("failures"), "workers": m.get("workers"),
        "retryRate": (retries / calls) if calls and retries is not None else None,
        "p50": m.get("p50"), "p95": m.get("p95"), "p99": m.get("p99"),
        "requestsPerSec": m.get("requests_per_sec"),
        "pushbacks": sum(int(l.get("backoffs", 0)) for l in limiters.values()) if limiters else None,
        "limiters": limiters,
    }


def _ledger_span(conn) -> tuple[str | None, str | None]:
    # id order is chronological and O(1); MIN/MAX(timestamp) would scan a
    # ledger that can hold ten million rows.
    a = conn.execute("SELECT timestamp FROM audit_log ORDER BY id ASC LIMIT 1").fetchone()
    b = conn.execute("SELECT timestamp FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    return (a["timestamp"] if a else None, b["timestamp"] if b else None)


def _items_since(conn, started: str | None) -> int | None:
    """SUCCESS rows written since `started`, or None when that is not known.
    audit_log rows are only ever collapsed by a manual retention pass and only
    for finished users, so a run's own rows are still there to count."""
    when = _parse_iso(started)
    if not when:
        return None
    # Same shape as the column's own default -- a differently-shaped string
    # would sort wrongly against it.
    stamp = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return conn.execute("SELECT COUNT(*) c FROM audit_log WHERE status='SUCCESS' AND timestamp>=?",
                        (stamp,)).fetchone()["c"]


def _fidelity(conn) -> dict:
    """What the tenants themselves said (written by the tally / verify pass).

    Absent until one has run -- and absence is not a pass: every fidelity
    benchmark reads UNKNOWN, and the verdict says UNVERIFIED.
    """
    have = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_fidelity'").fetchone()
    if not have:
        return {}
    row = conn.execute("SELECT recorded_at, payload FROM run_fidelity ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return {}
    out = json.loads(row["payload"])
    out["recordedAt"] = row["recorded_at"]
    return out


def _fresh_fidelity(fid: dict, run: dict) -> dict:
    """A tally taken before this run began describes some earlier state of the
    tenants, not this run's result -- so it is not used to judge it. Its
    numbers are dropped (the benchmarks then read UNKNOWN) and only the fact
    that one exists, and when, is kept for the report to say."""
    taken, began = _parse_iso(fid.get("recordedAt")), _parse_iso((run or {}).get("startedAt"))
    if fid and taken and began and taken < began:
        return {"stale": True, "recordedAt": fid["recordedAt"]}
    return fid


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=HERE,
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:      # noqa: BLE001 - a deployed tree has no .git
        return None


def _environment() -> dict:
    env = {"python": sys.version.split()[0], "platform": platform.platform(),
           "commit": _git_commit()}
    try:
        import resources
        r = resources.probe()
        env.update({"cores": r.cpu_logical, "ramTotalGb": round(r.ram_total_gb, 1),
                    "ramUsableGb": round(r.ram_usable_gb, 1)})
    except Exception:      # noqa: BLE001
        pass
    return env


def _parse_iso(s: str | None):
    """A timestamp as an aware UTC datetime, or None. The ledger writes UTC
    without an offset; the job record writes one."""
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _normalise_run(run: dict | None, fallback_span=(None, None), fallback_source: str = "ledger") -> dict:
    """The run block: the job's own record where there is one, else the best
    span the caller can offer (labelled as such)."""
    run = dict(run or {})
    started, finished, source = run.get("startedAt"), run.get("finishedAt"), "job"
    if not started:
        started, finished, source = fallback_span[0], fallback_span[1], fallback_source
    run.setdefault("returnCode", None)
    # 1 for any non-zero code, including the negative ones a signal produces.
    run["nonzeroExit"] = None if run["returnCode"] is None else int(run["returnCode"] != 0)
    run.update({"startedAt": started, "finishedAt": finished, "timingSource": source})
    a, b = _parse_iso(started), _parse_iso(finished)
    run["durationSec"] = (b - a).total_seconds() if a and b and b > a else None
    return run


def _transcript_block(transcript: list[str] | None) -> dict:
    return {
        "tail": [str(l)[:300] for l in (transcript or [])][-TRANSCRIPT_LINES:],
        "errorLines": [l for l in (str(x)[:300] for x in (transcript or []))
                       if re.search(r"Traceback|Error|FAILED|^\s*!", l)][:25],
    }


def collect_seed_facts(settings, *, run: dict | None = None, transcript: list[str] | None = None) -> dict:
    """The facts for a seed run: no ledger, so everything is read back from its
    transcript. `transcript` should be the whole of it, not a tail."""
    import seed_report
    errors: list[dict] = []
    facts: dict = {"kind": "seed", "errors": errors}
    ended = bool(run) and (run.get("returnCode") is not None or bool(run.get("finishedAt")))
    seed, families = _guard(errors, "seed", lambda: seed_report.facts(transcript or [], ended=ended), ({}, []))
    facts["seed"], facts["failures"] = seed, families
    facts["run"] = _normalise_run(run, (None, None), "transcript")
    if not facts["run"].get("durationSec") and seed.get("elapsedSec"):
        facts["run"]["durationSec"], facts["run"]["timingSource"] = seed["elapsedSec"], "transcript"
    facts["config"] = {k: getattr(settings, k, None) for k in CONFIG_KEYS
                       if isinstance(getattr(settings, k, None), (str, int, float, bool, type(None)))}
    facts["environment"] = _environment()
    facts["transcript"] = _transcript_block(transcript)
    return facts


def collect_facts(db, settings, *, kind: str = "migration", run: dict | None = None,
                  transcript: list[str] | None = None) -> dict:
    """Everything the ledger and the process can say about a run. `db` needs
    only a `.conn` -- a read-only connection is what the API passes, so a
    report never contends with a migration for the ledger's write lock. `run`
    is the job's own record (returnCode, startedAt, finishedAt) when the caller
    has it."""
    conn = db.conn
    errors: list[dict] = []
    facts: dict = {"kind": kind, "errors": errors}

    facts["users"] = _guard(errors, "users", lambda: _users(conn), {})
    facts["ledger"] = _guard(errors, "ledger", lambda: _ledger(conn), {})
    facts["failures"] = _guard(errors, "failures", lambda: _failure_families(conn), [])

    def _headline():
        import webui_spa
        return webui_spa.report_payload(conn, settings, 0.0, 0.0)
    facts["headline"] = _guard(errors, "headline", _headline, {})

    def _repair():
        import repair
        return {k: v for k, v in repair.survey(db).items() if isinstance(v, (int, float))}
    facts["repair"] = _guard(errors, "repair", _repair, {})
    facts["metrics"] = _guard(errors, "metrics", lambda: _metrics(conn), None) or {}
    facts["fidelity"] = _guard(errors, "fidelity", lambda: _fidelity(conn), {})

    # -- timing: the job's own record beats the ledger's first/last row ------
    span = _guard(errors, "timing", lambda: _ledger_span(conn), (None, None)) if not (run or {}).get("startedAt") else (None, None)
    facts["run"] = _normalise_run(run, span, "ledger")
    run, source, duration = facts["run"], facts["run"]["timingSource"], facts["run"]["durationSec"]
    facts["fidelity"] = _fresh_fidelity(facts["fidelity"], run)

    # -- performance: only from a job's own timing. A ledger's first and last
    # rows span idle days between passes, and dividing by that would report a
    # slow run that was merely a long one.
    workers = getattr(settings, "user_workers", None)
    # Items written DURING this run. The ledger's own total is every run since
    # the tenant began, so dividing it by one job's duration reports the whole
    # history's work as this run's speed -- wildly high for a resume or a delta.
    in_run = (_guard(errors, "itemsInRun", lambda: _items_since(conn, run.get("startedAt")), None)
              if duration and source == "job" else None)
    per_min = (in_run / (duration / 60)) if in_run is not None else None
    facts["perf"] = {
        "itemsInRun": in_run,
        "itemsPerMin": per_min,
        "workers": workers,
        "itemsPerMinPerWorker": (per_min / workers) if per_min is not None and workers else None,
    }

    facts["paths"] = {"ledger": getattr(settings, "db_path", None)}
    facts["config"] = {k: getattr(settings, k, None) for k in CONFIG_KEYS
                       if isinstance(getattr(settings, k, None), (str, int, float, bool, type(None)))}
    facts["environment"] = _environment()
    facts["transcript"] = _transcript_block(transcript)
    return facts


# ---------------------------------------------------------------------------
# Judgement, guidance, and where to look
# ---------------------------------------------------------------------------
# Error text -> where in the code that kind of failure comes from. A starting
# point for whoever fixes it, not a diagnosis: it names a place to look.
SUSPECTS = (
    (r"storageQuotaExceeded|quota.*exceed", ["resilience.py (DailyQuotaGuard, 750 GB/day)",
                                            "the target account's storage licence"]),
    (r"rateLimitExceeded|userRateLimit|429", ["resilience.py (AdaptiveRateLimiter, retry_on_google_error)",
                                             "the target project's API quota"]),
    (r"insufficientFilePermissions|insufficientPermissions|forbidden", [
        "drive_engine.py (server_side move / ownership)", "scope_guard.py (missing scope)"]),
    (r"unauthorized_client|invalid_scope", ["scope_guard.py", "domain-wide delegation scopes (verify_scopes.py)"]),
    (r"invalid_grant|Invalid email or User ID|Active session is invalid", [
        "provision.py (account missing or unlicensed)", "a pending password change breaking delegation"]),
    (r"412|Mail service not enabled|licen[cs]e", ["the target licence pool (job_admission / provision.py)"]),
    (r"not found|404", ["id_mapping drift (ledger_verify.py)", "the source item was deleted mid-run"]),
    (r"timed? ?out|Timeout|ConnectionReset|BrokenPipe", ["resilience.py (retry budget)", "host memory or network"]),
    (r"SKIPPED_EXPORT_TOO_LARGE|exportSizeLimitExceeded", ["drive_engine.py (native file export limit; use server_side)"]),
)


def suspected_areas(failures: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for f in failures:
        for pat, where in SUSPECTS:
            if re.search(pat, f.get("message") or "", re.I):
                for w in where:
                    e = out.setdefault(w, {"where": w, "failures": 0, "examples": []})
                    e["failures"] += f.get("count", 0)
                    if len(e["examples"]) < 2:
                        e["examples"].append((f.get("message") or "")[:120])
    return sorted(out.values(), key=lambda e: -e["failures"])


def _seed_next_steps(facts: dict, bench: dict) -> list[str]:
    steps: list[str] = []
    rc = (facts.get("run") or {}).get("returnCode")
    seed = facts.get("seed") or {}
    if rc:
        steps.append(f"The seed exited with code {rc}. Read the end of its log first (below).")
    if seed.get("neverFinished"):
        steps.append(f"{seed['neverFinished']} user(s) never reported a result. Check the warnings and the "
                     "log for those users; a re-run only touches what is missing.")
    if seed.get("failedServiceUsers"):
        steps.append(f"{seed['failedServiceUsers']} user(s) finished with a failed service "
                     f"({', '.join(sorted(seed.get('failedServices') or {}))}). Their data for that "
                     "service is missing.")
    if seed.get("fillFailedUsers"):
        why = ", ".join(f"{n} x {c}" for c, n in sorted((seed.get("fillFailures") or {}).items(), key=lambda kv: -kv[1]))
        steps.append(f"{seed['fillFailedUsers']} user(s) had their storage fill refused ({why}). "
                     + ("storageQuotaExceeded on a pooled tenant means the POOL ran out, not that those "
                        "accounts are full: free space (accounts far above their own share are the usual "
                        "cause -- Seed Wizard > Top up > Remove filler previews and trims them), then run "
                        "the same fill again; a top-up only ever adds, so it finishes the tail."
                        if "storageQuotaExceeded" in why else "Run the fill again; a top-up only ever adds."))
    if seed.get("mode") == "fill" and seed.get("fillReached") is not None and seed["fillReached"] < 0.98:
        steps.append("The fill stopped short of its target. Run it again: a top-up only ever adds.")
    if bench["verdict"] == "PASS":
        steps.append("The seed finished cleanly. It is ready to migrate from.")
    return steps


def next_steps(facts: dict, bench: dict) -> list[str]:
    if facts.get("kind") == "seed":
        return _seed_next_steps(facts, bench)
    steps: list[str] = []
    deferred = (facts.get("ledger") or {}).get("deferred") or 0
    if deferred:
        # First, because until it is done the migration is not: this mail is not on
        # the target and nothing in the ledger says otherwise.
        steps.append(f"{deferred:,} mail message(s) were deliberately left for Google's Data Migration "
                     "Service and are NOT on the target yet. Run it now (Nodes > Deploy > Start Google DMS "
                     "mail import) -- after this run, never before, or it moves link-bearing mail "
                     "without rewriting its links. Then run the tally: it counts them as still owed, "
                     "so mail parity stays short until the DMS has delivered them.")
    rc = (facts.get("run") or {}).get("returnCode")
    if rc:
        steps.append(f"The run exited with code {rc}. Read the end of its log first (below) -- "
                     "nothing else in this report can be trusted until that is explained.")
    ledger = facts.get("ledger") or {}
    if ledger.get("failed"):
        steps.append(f"{ledger['failed']:,} item(s) failed. Open the Failures tab and run Repair: "
                     "it groups them by cause and fixes the fixable ones.")
    if ledger.get("blocked"):
        steps.append(f"{ledger['blocked']:,} item(s) are blocked, usually by licences or user "
                     "limits. Fix that, then re-run -- blocked users are collected automatically.")
    if (facts.get("users") or {}).get("failed"):
        steps.append(f"{facts['users']['failed']} user(s) failed outright. Their mailbox and drive "
                     "have not moved.")
    unverified = [r for r in bench["results"] if r["status"] == "unknown" and r["required"]]
    if any(r["category"] == "fidelity" for r in unverified):
        steps.append("Fidelity has not been verified: no source-versus-target tally exists for this "
                     "run. Run the tally, then regenerate this report.")
    if any(r["category"] == "health" for r in unverified):
        steps.append("The run's own outcome was not recorded (no exit code). Regenerate this report "
                     "from the Jobs page's finished run, or it cannot say the run completed.")
    if bench["verdict"] == "PASS":
        steps.append("No benchmark failed and every required one was verified. Review the warnings "
                     "(if any) before cutover.")
    return steps


def build_report(facts: dict, *, run_id: str | None = None, account_id=None,
                 overrides: dict | None = None, when: datetime | None = None) -> dict:
    bench = benchmarks.evaluate(facts, overrides, benchmarks.for_kind(facts.get("kind", "migration")))
    now = when or _now()
    return {
        "schema": 1,
        "id": run_id or new_run_id(facts.get("kind", "migration"), now),
        "kind": facts.get("kind", "migration"),
        "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "accountId": account_id,
        "tenants": {"source": (facts.get("config") or {}).get("source_domain"),
                    "target": (facts.get("config") or {}).get("target_domain")},
        "verdict": bench["verdict"],
        "benchmarks": bench,
        "nextSteps": next_steps(facts, bench),
        "suspected": suspected_areas(facts.get("failures") or []),
        "facts": facts,
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def load_overrides(account_id) -> dict:
    """Per-account thresholds, if the operator has set any. Never fatal."""
    try:
        path = os.path.join(HERE, "data", "accounts", str(account_id), "benchmarks.json")
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_report(report: dict, account_id) -> dict:
    os.makedirs(reports_dir(account_id), exist_ok=True)
    path = report_file(account_id, report["id"], "json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, default=str)
    os.replace(tmp, path)
    files = ["json"]
    try:
        import report_pdf
        report_pdf.write_pdf(report, report_file(account_id, report["id"], "human.pdf"), "human")
        report_pdf.write_pdf(report, report_file(account_id, report["id"], "claude.pdf"), "claude")
        files += ["human.pdf", "claude.pdf"]
    except Exception as exc:      # noqa: BLE001 - the JSON is saved; say what is missing
        report.setdefault("facts", {}).setdefault("errors", []).append(
            {"section": "pdf", "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1, default=str)
    return summarise(report, files)


def summarise(report: dict, files: list[str] | None = None) -> dict:
    run = (report.get("facts") or {}).get("run") or {}
    return {"id": report["id"], "kind": report["kind"], "generatedAt": report["generatedAt"],
            "accountId": report.get("accountId"), "verdict": report["verdict"], "counts": report["benchmarks"]["counts"],
            "tenants": report.get("tenants"), "returnCode": run.get("returnCode"),
            "startedAt": run.get("startedAt"), "finishedAt": run.get("finishedAt"),
            "files": files or []}


def generate(db, settings, account_id, *, kind: str = "migration", run: dict | None = None,
             transcript: list[str] | None = None) -> dict:
    facts = (collect_seed_facts(settings, run=run, transcript=transcript) if kind == "seed"
             else collect_facts(db, settings, kind=kind, run=run, transcript=transcript))
    report = build_report(facts, account_id=account_id, overrides=load_overrides(account_id))
    return save_report(report, account_id)


def list_reports(account_id) -> list[dict]:
    d = reports_dir(account_id)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    for n in names:
        run_id = n[:-5] if n.endswith(".json") else None
        if not run_id or not RUN_ID_RE.match(run_id):
            continue
        try:
            with open(os.path.join(d, n), encoding="utf-8") as fh:
                rep = json.load(fh)
            files = [e for e in ("json", "human.pdf", "claude.pdf")
                     if os.path.isfile(os.path.join(d, f"{run_id}.{e}"))]
            out.append(summarise(rep, files))
        except (OSError, ValueError, KeyError):
            continue
    return sorted(out, key=lambda r: r["generatedAt"], reverse=True)


def list_all_reports() -> list[dict]:
    """Every account's reports, newest first, each carrying its accountId.

    For an operator: a report belongs to an account, but the person who needs
    it -- to hand to Claude Code, say -- is usually signed in as themselves,
    not as the tenant it is about. Without this the report exists and cannot
    be found.
    """
    base = os.path.dirname(reports_dir(0))
    try:
        names = os.listdir(base)
    except OSError:
        return []
    out = []
    for n in names:
        if not (n.isdigit() or n == "legacy"):
            continue
        aid = int(n) if n.isdigit() else None
        for r in list_reports(aid):
            out.append({**r, "accountId": r.get("accountId", aid) if r.get("accountId") is not None else aid})
    return sorted(out, key=lambda r: r["generatedAt"], reverse=True)


def load_report(account_id, run_id: str) -> dict | None:
    try:
        with open(report_file(account_id, run_id, "json"), encoding="utf-8") as fh:
            return json.load(fh)
    except OSError:
        return None
