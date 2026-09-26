"""
run_watch.py -- notice what happened to a run, write it down, and hand a
problem to whoever can fix it.

"Keep an eye on the job" used to mean a person (or a Claude session) polling,
which stops the moment they close the window and leaves no record of what was
seen. This does it continuously, on the server, and keeps the answer.

How it works, and why it is built this way
------------------------------------------
It OBSERVES, it does not hook. Every heavy job -- a seed launched by webui.py, a
migration launched by api_server.py -- registers a row in the admission table
(job_admission), and that table is the one place both processes already write.
The watcher reads it: a process that appears is a run that started, one that
disappears is a run that finished. Observing means nothing in the launchers had
to change, so watching a seed needs no restart of the service running it.

State lives in the database (run_events), not in memory. A run is "open" while
it has a started event and no finished one, so a restart of the watcher finds
its open runs where it left them and notices any that ended in the meantime.

An exit code is recorded only where one was actually observed. A run whose exit
was not seen has rc NULL, and NULL is never treated as success: the report says
"exit unknown", and the benchmark for it comes back unknown.

What it raises an incident for
------------------------------
  crashed       a finished run with a non-zero exit (a signal death is negative)
  verdict_fail  a run that exited 0 but whose report failed a benchmark -- the
                quiet failure, which is the one nobody was watching for
  failing       a burst of new failures WHILE a migration runs, so a bad
                deploy or a quota wall is seen in minutes, not at the end
  stalled       the supervisor ended a wedged run
  traceback     a seed's log grew a Traceback

One problem recurring is one incident with a count (same fingerprint inside the
dedupe window), never a hundred rows. Each new incident gets a brief written
for whoever fixes it -- see write_brief -- and a line in a feed file that a
session can tail.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import control_plane_db as cpdb
import notify

log = logging.getLogger("run_watch")

HERE = os.path.dirname(os.path.abspath(__file__))
INCIDENT_DIR = os.path.join(HERE, "logs", "incidents")

DEDUPE_HOURS = 6
# New FAILED/BLOCKED rows between two looks that count as a burst. A handful of
# failures is normal on a real tenant; a run that starts failing dozens per
# minute is not.
BURST_MIN_FAILURES = 25
BURST_ERROR_FAILURES = 200
SEED_WARN_BURST = 15

# A tally is deliberately absent: it measures the tenants for a report, it is not
# a run to be judged (it moves nothing, so it would score as "migrated nothing").
MIGRATION_JOBS = {"migrate": "migration", "delta": "migration"}


def report_kind(job_name: str) -> str | None:
    """Which kind of report a finished job earns, or None for jobs (setup,
    reset, provisioning) that have no report."""
    if (job_name or "").startswith("seed"):
        return "seed"
    return MIGRATION_JOBS.get(job_name)


def _now_iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _epoch(iso: str | None) -> float | None:
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
def record_started(account_id, job_name: str, pid=None, started_at=None) -> None:
    with cpdb.rw() as c:
        c.execute("INSERT INTO run_events(account_id, job_name, event, pid, started_at) "
                  "VALUES(?,?,?,?,?)", (account_id, job_name, "started", pid, started_at))


def record_finished(account_id, job_name: str, rc, pid=None, started_at=None, detail: str = "") -> None:
    """One finished event per run. A second call for the same run (the launcher
    saw the exit and so did the observer) is ignored rather than doubled -- and
    the first one wins, which is the launcher's, because it holds the real exit
    code and the observer only knows the process is gone."""
    same = "job_name=? AND COALESCE(account_id,-1)=COALESCE(?,-1) AND COALESCE(pid,-1)=COALESCE(?,-1)"
    key = (job_name, account_id, pid)
    with cpdb.rw() as c:
        start = c.execute(f"SELECT MAX(id) m FROM run_events WHERE event='started' AND {same}", key).fetchone()["m"]
        if start is not None and c.execute(
                f"SELECT 1 FROM run_events WHERE event='finished' AND {same} AND id > ?",
                (*key, start)).fetchone():
            return
        c.execute("INSERT INTO run_events(account_id, job_name, event, pid, rc, started_at, detail) "
                  "VALUES(?,?,?,?,?,?,?)", (account_id, job_name, "finished", pid, rc, started_at, detail))


def open_runs() -> list[dict]:
    """Runs that started and have not been seen to finish."""
    with cpdb.ro() as c:
        rows = c.execute(
            "SELECT s.* FROM run_events s WHERE s.event='started' AND NOT EXISTS ("
            " SELECT 1 FROM run_events f WHERE f.event='finished' AND f.job_name=s.job_name"
            " AND COALESCE(f.account_id,-1)=COALESCE(s.account_id,-1)"
            " AND COALESCE(f.pid,-1)=COALESCE(s.pid,-1) AND f.id > s.id)").fetchall()
    return [dict(r) for r in rows]


def unhandled_finished() -> list[dict]:
    with cpdb.ro() as c:
        rows = c.execute("SELECT * FROM run_events WHERE event='finished' AND handled_at IS NULL "
                         "ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def mark_handled(event_id: int) -> None:
    with cpdb.rw() as c:
        c.execute("UPDATE run_events SET handled_at=? WHERE id=?", (_now_iso(), event_id))


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------
def feed_path() -> str:
    return os.path.join(INCIDENT_DIR, "feed.log")


def _feed(line: str) -> None:
    try:
        os.makedirs(INCIDENT_DIR, exist_ok=True)
        with open(feed_path(), "a", encoding="utf-8") as fh:
            fh.write(f"{_now_iso()} {line}\n")
    except OSError as exc:      # noqa: BLE001 - a feed that cannot be written is not worth a crash
        log.warning("incident feed unwritable: %s", exc)


def normalise(msg: str) -> str:
    """A failure message with the parts that vary stripped, so the same problem
    on different users or ids has one fingerprint."""
    # Drive and Gmail ids are long base64-ish tokens, not hex.
    m = re.sub(r"\b[\w-]{20,}\b", "<id>", msg or "")
    m = re.sub(r"[\w.+-]+@[\w.-]+", "<user>", m)
    m = re.sub(r"\d+", "N", m)
    return m[:80]


def open_incident(*, kind: str, title: str, summary: str, account_id, job_name: str,
                  fingerprint: str, severity: str = "error", run_id: str | None = None,
                  brief_text: str | None = None) -> tuple[int, bool]:
    """Open an incident, or bump the open one with the same fingerprint.
    Returns (id, is_new). Only a NEW incident writes a brief and notifies."""
    cutoff = _now_iso(datetime.now(timezone.utc) - timedelta(hours=DEDUPE_HOURS))
    with cpdb.rw() as c:
        row = c.execute("SELECT id, severity FROM incidents WHERE fingerprint=? AND status IN "
                        "('open','acknowledged') AND last_seen_at >= ? ORDER BY id DESC LIMIT 1",
                        (fingerprint, cutoff)).fetchone()
        if row:
            worse = severity == "error" and row["severity"] != "error"
            c.execute("UPDATE incidents SET occurrences=occurrences+1, last_seen_at=?, summary=?,"
                      " severity=CASE WHEN ? THEN 'error' ELSE severity END WHERE id=?",
                      (_now_iso(), summary, 1 if worse else 0, row["id"]))
            return row["id"], False
        cur = c.execute("INSERT INTO incidents(account_id, job_name, kind, severity, fingerprint,"
                        " title, summary, run_id) VALUES(?,?,?,?,?,?,?,?)",
                        (account_id, job_name, kind, severity, fingerprint, title, summary, run_id))
        inc_id = cur.lastrowid
    brief_path = None
    text = brief_text or f"# Incident {inc_id}: {title}\n\n{summary}\n"
    try:
        os.makedirs(INCIDENT_DIR, exist_ok=True)
        brief_path = os.path.join(INCIDENT_DIR, f"{inc_id}.md")
        with open(brief_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        with cpdb.rw() as c:
            c.execute("UPDATE incidents SET brief_path=? WHERE id=?", (brief_path, inc_id))
    except OSError as exc:      # noqa: BLE001 - the incident stands without its file
        log.warning("brief unwritable for incident %s: %s", inc_id, exc)
    _feed(f"INCIDENT #{inc_id} {severity} {kind} account={account_id} job={job_name} :: {title}"
          f" -> brief: {brief_path or '(unwritable)'}")
    try:
        notify.send(f"[{severity}] {title}", f"{summary[:400]}\nIncident #{inc_id}", severity,
                    issue_body=text)
    except Exception as exc:      # noqa: BLE001 - never let a notification break the watcher
        log.warning("notify failed: %s", exc)
    return inc_id, True


def list_incidents(status: str | None = None, account_id=None, limit: int = 100) -> list[dict]:
    q, args = "SELECT * FROM incidents", []
    where = []
    if status:
        where.append("status=?"); args.append(status)
    if account_id is not None:
        where.append("account_id=?"); args.append(account_id)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY last_seen_at DESC LIMIT ?"
    args.append(limit)
    with cpdb.ro() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def get_incident(incident_id: int) -> dict | None:
    with cpdb.ro() as c:
        row = c.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
    return dict(row) if row else None


def set_status(incident_id: int, status: str, note: str = "") -> bool:
    if status not in ("open", "acknowledged", "resolved"):
        raise ValueError(f"bad status {status!r}")
    with cpdb.rw() as c:
        cur = c.execute("UPDATE incidents SET status=?, note=CASE WHEN ?='' THEN note ELSE ? END,"
                        " resolved_at=CASE WHEN ?='resolved' THEN ? ELSE NULL END WHERE id=?",
                        (status, note, note, status, _now_iso(), incident_id))
        return cur.rowcount > 0


def read_brief(incident_id: int) -> str | None:
    inc = get_incident(incident_id)
    if not inc or not inc.get("brief_path"):
        return None
    try:
        with open(inc["brief_path"], encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# The hand-off
# ---------------------------------------------------------------------------
CLAUDE_STEPS = """\
## For whoever fixes this (written for Claude Code)

1. Read `CLAUDE.md` first: it explains the two Python versions in play, why
   deploys are dangerous mid-run, and what not to touch.
2. Reproduce before changing anything. The failure families above quote real
   messages; `tests/fakes.py` can usually recreate the shape without touching a
   live tenant. Write the failing test first.
3. Fix at the root, run the affected tests and then the suite, and syntax-check
   under the server's Python (3.10), which is older than a laptop's.
4. **Do not deploy without asking the operator.** A deploy restarts services and
   can kill a running seed or migration. If a run is in flight, say so and let
   them choose the moment.
5. When done: `python incidents.py resolve <id> --note "<what you changed>"`,
   and regenerate the report from the Final Report tab so it reflects the fix.
"""


def write_brief(*, incident_id: int | None, title: str, summary: str, account_id, job_name: str,
                kind: str, severity: str, report: dict | None, transcript: list[str] | None,
                rc=None) -> str:
    """The document an incident is handed over with: enough to start work
    without going back to the server to ask what happened."""
    lines = [f"# Incident{f' {incident_id}' if incident_id else ''}: {title}", "",
             f"- severity: **{severity}**   kind: `{kind}`", f"- account: {account_id}   job: `{job_name}`"
             + (f"   exit code: `{rc}`" if rc is not None else "   exit code: not observed"),
             f"- raised: {_now_iso()}", "", "## What happened", "", summary, ""]
    if report:
        f = report.get("facts") or {}
        b = report.get("benchmarks") or {}
        lines += ["## The run's report", "",
                  f"- report id: `{report.get('id')}`  (PDFs for a person and for Claude are on the "
                  "Final Report tab; JSON at `/api/v2/reports/<id>`)",
                  f"- verdict: **{report.get('verdict')}**  ({b.get('counts')})", ""]
        bad = [r for r in b.get("results", []) if r["status"] in ("fail", "warn")]
        if bad:
            lines += ["### Benchmarks not passing", ""]
            lines += [f"- **{r['status'].upper()}** `{r['id']}`: {r['display']}  (need {r['threshold']}; "
                      f"read from `{r['metric']}`)" for r in bad]
            lines.append("")
        fam = f.get("failures") or []
        if fam:
            lines += ["### Failure families", ""]
            lines += [f"- {x['count']:,} x `{x['itemType']}`, {x['users']:,} user(s): {x['message']}"
                      for x in fam[:10]]
            lines.append("")
        sus = report.get("suspected") or []
        if sus:
            lines += ["### Where to look first (pattern match, not a diagnosis)", ""]
            lines += [f"- {s['where']} ({s['failures']:,} failures)" for s in sus[:6]]
            lines.append("")
    if transcript:
        lines += ["## Log tail", "", "```", *[str(l)[:300] for l in transcript[-40:]], "```", ""]
    lines.append(CLAUDE_STEPS)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The observer
# ---------------------------------------------------------------------------
class Watcher:
    """One pass of `tick()` per poll. Every collaborator is injected, so the
    logic can be exercised without a server, a ledger or a network."""

    def __init__(self, *, list_active, is_live, make_report, ledger_path_for=None,
                 rc_for=None, transcript_for=None, log_path_for=None, now=time.time):
        self.list_active = list_active
        self.is_live = is_live
        # make_report(account_id, job_name, kind, run) -> report dict | None
        self.make_report = make_report
        self.ledger_path_for = ledger_path_for or (lambda a: None)
        # rc_for(account_id, job_name, started_epoch) -> int | None
        self.rc_for = rc_for or (lambda a, n, s: None)
        self.transcript_for = transcript_for or (lambda a, n: [])
        self.log_path_for = log_path_for or (lambda a, n: None)
        self.now = now
        self._live: dict = {}
        self._cursor: dict = {}          # account -> last audit_log id looked at
        self._offset: dict = {}          # (account, job) -> bytes of log already scanned

    # -- who is running -------------------------------------------------
    def _observe(self) -> None:
        live = {}
        for j in self.list_active():
            if self.is_live(j):
                live[(j.get("account_id"), j["job_name"], j.get("pid"))] = j
        opened = {(r["account_id"], r["job_name"], r["pid"]): r for r in open_runs()}
        for key, j in live.items():
            if key not in opened:
                record_started(key[0], key[1], key[2], j.get("started_at"))
        for key, r in opened.items():
            if key in live:
                continue
            started = _epoch(r.get("started_at") or r.get("at"))
            rc = self.rc_for(key[0], key[1], started)
            record_finished(key[0], key[1], rc, key[2], r.get("started_at") or r.get("at"),
                            "" if rc is not None else "the process ended and no exit code was observed")
        self._live = live

    # -- during a run -----------------------------------------------------
    def _watch_migration(self, account_id, job_name) -> None:
        path = self.ledger_path_for(account_id)
        if not path or not os.path.isfile(path):
            return
        with cpdb.ro(path) as c:
            top = c.execute("SELECT MAX(id) m FROM audit_log").fetchone()["m"] or 0
            last = self._cursor.get(account_id)
            self._cursor[account_id] = top
            if last is None:                    # first look: a baseline, not a burst
                return
            rows = c.execute(
                "SELECT item_type, substr(COALESCE(error_message,'(no message)'),1,160) msg,"
                " COUNT(*) n, COUNT(DISTINCT source_user) u FROM audit_log WHERE id > ? AND id <= ?"
                " AND status IN ('FAILED','BLOCKED') GROUP BY 1,2 ORDER BY n DESC LIMIT 5",
                (last, top)).fetchall()
        total = sum(r["n"] for r in rows)
        if total < BURST_MIN_FAILURES:
            return
        top_fam = rows[0]
        sev = "error" if total >= BURST_ERROR_FAILURES else "warn"
        summary = (f"{total:,} new failures since the last look while `{job_name}` runs. Largest: "
                   f"{top_fam['n']:,} x `{top_fam['item_type']}` across {top_fam['u']:,} user(s): "
                   f"{top_fam['msg']}")
        title = f"{job_name} is failing: {top_fam['msg'][:70]}"
        brief = write_brief(incident_id=None, title=title, summary=summary, account_id=account_id,
                            job_name=job_name, kind="failing", severity=sev, report=None,
                            transcript=self.transcript_for(account_id, job_name))
        open_incident(kind="failing", title=title, summary=summary, account_id=account_id,
                      job_name=job_name, severity=sev, brief_text=brief,
                      fingerprint=f"failing:{account_id}:{job_name}:{normalise(top_fam['msg'])}")

    def _watch_seed(self, account_id, job_name) -> None:
        path = self.log_path_for(account_id, job_name)
        if not path or not os.path.isfile(path):
            return
        key = (account_id, job_name)
        size = os.path.getsize(path)
        seen = self._offset.get(key)
        self._offset[key] = size
        if seen is None or size <= seen:        # baseline, or a truncated log of a new run
            return
        with open(path, "rb") as fh:
            fh.seek(seen)
            text = fh.read(size - seen).decode("utf-8", "replace")
        lines = text.splitlines()
        tb = [i for i, l in enumerate(lines) if l.startswith("Traceback")]
        if tb:
            chunk = lines[tb[0]: tb[0] + 15]
            title = f"{job_name}: Traceback in the log"
            summary = "The log grew a Traceback:\n\n    " + "\n    ".join(chunk)
            brief = write_brief(incident_id=None, title=title, summary=summary, account_id=account_id,
                                job_name=job_name, kind="traceback", severity="warn", report=None,
                                transcript=self.transcript_for(account_id, job_name))
            fp_line = next((l for l in reversed(chunk) if l and not l.startswith(" ")), chunk[-1])
            open_incident(kind="traceback", title=title, summary=summary, account_id=account_id,
                          job_name=job_name, severity="warn", brief_text=brief,
                          fingerprint=f"traceback:{account_id}:{job_name}:{normalise(fp_line)}")
        warn = [l for l in lines if re.match(r"^\s*!\s", l)]
        if len(warn) >= SEED_WARN_BURST:
            kinds: dict[str, int] = {}
            for l in warn:
                k = re.match(r"^\s*!\s+(\S+)", l).group(1)
                kinds[k] = kinds.get(k, 0) + 1
            worst, n = max(kinds.items(), key=lambda kv: kv[1])
            title = f"{job_name}: {len(warn)} new warnings, mostly `{worst}`"
            summary = f"{len(warn)} warning lines since the last look; {n} were `{worst}`. Example: {warn[0].strip()[:200]}"
            brief = write_brief(incident_id=None, title=title, summary=summary, account_id=account_id,
                                job_name=job_name, kind="failing", severity="warn", report=None,
                                transcript=self.transcript_for(account_id, job_name))
            open_incident(kind="failing", title=title, summary=summary, account_id=account_id,
                          job_name=job_name, severity="warn", brief_text=brief,
                          fingerprint=f"failing:{account_id}:{job_name}:{worst}")

    # -- when a run ends ---------------------------------------------------
    def _handle_finished(self, ev: dict) -> None:
        name, aid, rc = ev["job_name"], ev["account_id"], ev["rc"]
        kind = report_kind(name)
        run = {"returnCode": rc, "startedAt": ev.get("started_at"), "finishedAt": ev["at"], "name": name}
        report = None
        if kind:
            try:
                report = self.make_report(aid, name, kind, run)
            except Exception as exc:      # noqa: BLE001 - recorded on the incident, not lost
                log.warning("report for %s failed: %s", name, exc)
                report = None
        transcript = self.transcript_for(aid, name)
        verdict = (report or {}).get("verdict")
        run_id = (report or {}).get("id")
        if rc not in (None, 0):
            how = f"signal {-rc}" if rc < 0 else f"code {rc}"
            title = f"{name} exited with {how}"
            summary = (f"`{name}` for account {aid} ended with exit {how}. "
                       + (f"Its report ({run_id}) verdict is {verdict}." if verdict else "No report could be built."))
            brief = write_brief(incident_id=None, title=title, summary=summary, account_id=aid,
                                job_name=name, kind="crashed", severity="error", report=report,
                                transcript=transcript, rc=rc)
            open_incident(kind="crashed", title=title, summary=summary, account_id=aid, job_name=name,
                          fingerprint=f"crashed:{aid}:{name}:{rc}", severity="error", run_id=run_id,
                          brief_text=brief)
        elif verdict == "FAIL":
            failing = [r["id"] for r in report["benchmarks"]["results"] if r["status"] == "fail"]
            title = f"{name} finished but failed its benchmarks: {', '.join(failing[:3])}"
            summary = (f"`{name}` exited cleanly but the report ({run_id}) failed: {', '.join(failing)}. "
                       "A clean exit is not a good run.")
            brief = write_brief(incident_id=None, title=title, summary=summary, account_id=aid,
                                job_name=name, kind="verdict_fail", severity="error", report=report,
                                transcript=transcript, rc=rc)
            open_incident(kind="verdict_fail", title=title, summary=summary, account_id=aid, job_name=name,
                          fingerprint=f"verdict:{aid}:{name}:{','.join(sorted(failing))}",
                          severity="error", run_id=run_id, brief_text=brief)
        else:
            state = verdict or ("no report for this job" if not kind else "no report could be built")
            msg = f"{name} finished ({state}). Exit: {rc if rc is not None else 'not observed'}."
            if run_id:
                msg += f" Report {run_id} is on the Final Report tab."
            _feed(f"FINISHED account={aid} job={name} rc={rc} verdict={verdict} report={run_id}")
            if kind:                      # a success is worth one quiet note, not an incident
                try:
                    notify.send(f"{name} finished: {verdict or 'no report'}", msg, "info")
                except Exception as exc:      # noqa: BLE001
                    log.warning("notify failed: %s", exc)
        mark_handled(ev["id"])

    # -- one pass ------------------------------------------------------------
    def tick(self) -> dict:
        """Look once. Returns what it did, for the caller to log."""
        self._observe()
        watched = 0
        for (aid, name, _pid) in list(self._live):
            try:
                if name in ("migrate", "delta"):
                    self._watch_migration(aid, name); watched += 1
                elif name.startswith("seed"):
                    self._watch_seed(aid, name); watched += 1
            except Exception as exc:      # noqa: BLE001 - one bad look must not stop the next
                log.warning("watching %s for account %s failed: %s", name, aid, exc)
        finished = unhandled_finished()
        for ev in finished:
            try:
                self._handle_finished(ev)
            except Exception as exc:      # noqa: BLE001
                log.warning("handling finished run %s failed: %s", ev.get("id"), exc)
        return {"live": len(self._live), "watched": watched, "finished": len(finished)}
