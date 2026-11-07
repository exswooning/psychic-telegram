"""
oversight.py -- watches what no single run watches, and says so through the incident feed.

Each check reads state Bitport already keeps (ledgers, job logs, run_events, the admission table) or
asks one outside source (readiness, Google's status page) and returns the conditions that hold RIGHT
NOW. `sweep()` opens an incident for each (run_watch.open_incident: the feed, the UI, notifications)
and closes an open one of a check's kinds once that check has run again and no longer finds it --
evidence, never time. A check that fails to run closes nothing.

Why each exists (the causes of the errors seen on account 3, 7 Nov 2026):
  watch_repair_loop     a finding repaired 3+ times still open: a trigger and its handler disagree
                        (info@'s 168 held messages went 8 days that way)
  watch_slow_file       one file far past its budget, or a user with no progress for 10 min
                        (education@'s Doc ran 11+ min inside one counted call)
  watch_verdict_flip    a one-to-one verdict that changed with no run or repair in between: a reader
                        bug, not a migration change (stale FAILED rows, old mappings)
  watch_unclassified    failures known_issues cannot name: Bitport met something new
  watch_source_changed  the source tenant changed under a run (SOURCE_CHANGED from _source_tripwire)
  watch_box_busy        the box had no idle gap for hours, so no deploy could land
  watch_tenant_ready    a tenant that WAS ready is not any more (delegation revoked, a scope or an
                        admin gone) -- asked of Google, at most hourly, only for recently active accounts
  watch_google_status   an open Google Workspace incident on a product Bitport uses
  watch_isolation       one account's ledger or logs naming another account's domain

Deploy drift needs git, so it lives in monitor/watch.py (the operator's side), not here.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

log = logging.getLogger("oversight")

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "logs", "oversight", "state.json")
FP = "oversight"

REPAIR_LOOP_RUNS = int(os.getenv("OVERSIGHT_REPAIR_LOOP_RUNS", "3"))
SLOW_FILE_FACTOR = float(os.getenv("OVERSIGHT_SLOW_FILE_FACTOR", "2"))
NO_PROGRESS_SEC = int(os.getenv("OVERSIGHT_NO_PROGRESS_SEC", "600"))
BUSY_HOURS = float(os.getenv("OVERSIGHT_BUSY_HOURS", "3"))
BUSY_GAP_SEC = int(os.getenv("OVERSIGHT_BUSY_GAP_SEC", "60"))
READY_EVERY_SEC = int(os.getenv("OVERSIGHT_READY_EVERY_SEC", "3600"))
ACTIVE_HOURS = int(os.getenv("OVERSIGHT_ACTIVE_HOURS", "48"))
STATUS_URL = os.getenv("OVERSIGHT_STATUS_URL", "https://www.google.com/appsstatus/dashboard/incidents.json")
WATCHED_PRODUCTS = ("gmail", "drive", "docs", "sheets", "slides", "forms", "chat", "calendar",
                    "admin console", "contacts", "tasks", "workspace")
# A verify, a tally or a scan only reads: a verdict that changes across one of those alone is unexplained.
READ_ONLY_JOBS = ("verify", "user-tally", "tally", "discover", "readiness")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    # The ledger writes "...T...Z"; sqlite's datetime() writes a space and would match everything.
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def finding(kind, account_id, key, title, summary, severity="warn") -> dict:
    return {"kind": kind, "account_id": account_id, "key": str(key), "title": title,
            "summary": summary, "severity": severity}


def _ro(path: str):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


class Context:
    """Everything a check reads, injectable so each check is testable without a box."""

    def __init__(self, *, accounts, ledger_path, active_jobs=lambda: [], run_events=lambda since: [],
                 readiness=None, fetch_status=None, logs_root=os.path.join(HERE, "logs", "jobs"),
                 state=None, now=None):
        self.accounts = accounts            # [{"id", "source_domain", "target_domain"}]
        self.ledger_path = ledger_path      # aid -> path | None
        self.active_jobs = active_jobs      # () -> [{"account_id", "job_name", ...}]
        self.run_events = run_events        # since_iso -> [{"account_id","job_name","event","pid","at"}]
        self.readiness = readiness          # (aid, side) -> {"ready": bool|None, "checks": [...]}
        self.fetch_status = fetch_status    # () -> list of Google status incidents
        self.logs_root = logs_root
        self.state = state if state is not None else {}
        self.now = now or _now()

    def ledgers(self):
        for a in self.accounts:
            p = self.ledger_path(a["id"])
            if p and os.path.isfile(p):
                yield a["id"], p


# -- checks ------------------------------------------------------------------------------------------
def watch_repair_loop(ctx: Context) -> list[dict]:
    """Only findings a repair is MEANT to clear count: a kind with an automatic fix (known_issues.ISSUES).
    Duplicates kept by policy, documented exceptions and unnamed kinds are never cleared by a repair,
    so counting them read every user with one as a loop (first live sweep, 7 Nov 2026: 10 of 10)."""
    import known_issues
    automatic = {i[0] for i in known_issues.ISSUES if i[6]}
    out = []
    for aid, path in ctx.ledgers():
        with _ro(path) as c:
            if not {"verification_repair_queue", "verification_repairs"} <= _tables(c):
                continue
            cols = {r[1] for r in c.execute("PRAGMA table_info(verification_repair_queue)")}
            if "issue" not in cols:
                continue
            per: dict[str, dict] = {}
            for r in c.execute("SELECT source_user u, first_seen, issue FROM verification_repair_queue "
                               "WHERE resolved_at IS NULL"):
                if r["issue"] not in automatic:
                    continue
                p = per.setdefault(r["u"], {"first": r["first_seen"], "n": 0, "issues": set()})
                p["first"], p["n"] = min(p["first"], r["first_seen"]), p["n"] + 1
                p["issues"].add(r["issue"])
            for user, p in per.items():
                runs = c.execute("SELECT COUNT(*) FROM verification_repairs WHERE source_user=? AND "
                                 "state!='RUNNING' AND finished_at > ?", (user, p["first"])).fetchone()[0]
                if runs >= REPAIR_LOOP_RUNS:
                    issues = ", ".join(sorted(p["issues"]))
                    out.append(finding(
                        "watch_repair_loop", aid, user, f"Repairs are not converging for {user}",
                        f"{p['n']} finding(s) of kinds with an automatic fix ({issues}) open since {p['first']} "
                        f"after {runs} repair runs. A repair that runs and never clears its finding usually "
                        f"means the rule that triggers it and the handler that fixes it disagree about which "
                        f"items apply."))
    return out


def _tail(path: str, size: int = 65536) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            start = max(0, fh.tell() - size)
            fh.seek(start)
            lines = fh.read().decode("utf-8", "replace").splitlines()
            return lines[1:] if start else lines      # the first line of a mid-file read is partial
    except OSError:
        return []


def _seconds(line: str, field: str) -> int:
    m = re.search(rf"\b{field}=(\d+)s", line)
    return int(m.group(1)) if m else 0


def watch_slow_file(ctx: Context) -> list[dict]:
    budget = float(os.getenv("VERIFY_DOC_BUDGET_SEC", "300"))
    out = []
    for job in ctx.active_jobs():
        aid, name = job.get("account_id"), (job.get("job_name") or "").split(":")[0].strip()
        if not name:
            continue
        lines = _tail(os.path.join(ctx.logs_root, "_none" if aid is None else str(aid), f"{name}.log"))[-40:]
        latest = {}
        for ln in lines:
            m = re.search(r"heartbeat (\S+@\S+) \|", ln)
            if m:
                latest[m.group(1)] = ln
        for user, ln in latest.items():
            doc = re.search(r"currentDocument=(\S+)", ln)
            elapsed, stalled = _seconds(ln, "currentDocumentElapsed"), _seconds(ln, "noItemProgress")
            if doc and elapsed > SLOW_FILE_FACTOR * budget:
                out.append(finding("watch_slow_file", aid, f"{name}:{user}:{doc.group(1)}",
                                   f"{name}: one file has run {elapsed}s for {user}",
                                   f"Document {doc.group(1)} has been compared for {elapsed}s, over "
                                   f"{SLOW_FILE_FACTOR:g}x its {budget:g}s budget. A step inside it has no "
                                   f"deadline of its own.\n\nLast heartbeat:\n{ln[:1500]}", "error"))
            elif stalled >= NO_PROGRESS_SEC:
                out.append(finding("watch_slow_file", aid, f"{name}:{user}:stalled",
                                   f"{name}: no progress for {stalled}s on {user}",
                                   f"Last heartbeat:\n{ln[:1500]}", "error"))
    return out


def _activity_between(ctx: Context, aid, user, start: str, end: str, conn) -> bool:
    for ev in ctx.run_events(start):
        if ev.get("account_id") == aid and start <= (ev.get("at") or "") <= end \
                and not any((ev.get("job_name") or "").startswith(j) for j in READ_ONLY_JOBS):
            return True
    t = _tables(conn)
    if "verification_repairs" in t and conn.execute(
            "SELECT 1 FROM verification_repairs WHERE source_user=? AND COALESCE(finished_at,started_at) "
            "BETWEEN ? AND ? LIMIT 1", (user, start, end)).fetchone():
        return True
    if "finding_repair_actions" in t and conn.execute(
            "SELECT 1 FROM finding_repair_actions WHERE source_user=? AND checked_at BETWEEN ? AND ? LIMIT 1",
            (user, start, end)).fetchone():
        return True
    return False


def watch_verdict_flip(ctx: Context) -> list[dict]:
    seen = ctx.state.setdefault("verdicts", {})
    flips = ctx.state.setdefault("flips", {})
    for aid, path in ctx.ledgers():
        prev, acct_flips = seen.setdefault(str(aid), {}), flips.setdefault(str(aid), {})
        with _ro(path) as c:
            if "user_verification" not in _tables(c):
                continue
            for r in c.execute("SELECT source_user, service, verdict, verified_at FROM user_verification"):
                key = f"{r['source_user']}|{r['service']}"
                old = prev.get(key)
                prev[key] = [r["verdict"], r["verified_at"]]
                if not old or old[1] == r["verified_at"]:
                    continue
                if old[0] == r["verdict"]:
                    acct_flips.pop(key, None)          # checked again, same answer: stable now
                    continue
                if "IDENTICAL" not in (old[0], r["verdict"]):
                    continue
                if _activity_between(ctx, aid, r["source_user"], old[1], r["verified_at"], c):
                    acct_flips.pop(key, None)
                    continue
                acct_flips[key] = {"from": old[0], "to": r["verdict"], "was": old[1], "now": r["verified_at"]}
    out = []
    for aid, acct in flips.items():
        for key, f in acct.items():
            user, service = key.split("|", 1)
            out.append(finding("watch_verdict_flip", int(aid), key,
                               f"{user} {service}: {f['from']} -> {f['to']} with nothing run in between",
                               f"Checked at {f['was']} as {f['from']} and at {f['now']} as {f['to']}, with no "
                               f"migration, repair or mirror run for this account in between. A verdict that "
                               f"moves on its own means the checker read something stale or inconsistent."))
    return out


def watch_unclassified(ctx: Context) -> list[dict]:
    import known_issues
    import run_watch
    since = _iso(ctx.now - timedelta(hours=24))
    out = []
    for aid, path in ctx.ledgers():
        with _ro(path) as c:
            fam: dict[str, list] = {}
            for r in c.execute("SELECT item_type, error_message FROM audit_log WHERE status='FAILED' "
                               "AND timestamp >= ?", (since,)):
                if known_issues.classify_failure(r["item_type"], r["error_message"] or "") != "unclassified":
                    continue
                k = run_watch.normalise(r["error_message"] or "(no message)")[:120]
                fam.setdefault(k, [0, r["item_type"], r["error_message"] or ""])[0] += 1
        for k, (n, item_type, sample) in sorted(fam.items(), key=lambda x: -x[1][0])[:5]:
            out.append(finding("watch_unclassified", aid, k,
                               f"{n} failure(s) of a kind Bitport cannot name yet ({item_type})",
                               f"Last 24h: {n} FAILED {item_type} row(s) that no known_issues.RUN_KINDS entry "
                               f"matches. Name it, give it a cause and a fix, and say whether it is automatic."
                               f"\n\nExample: {sample[:600]}"))
    return out


def watch_source_changed(ctx: Context) -> list[dict]:
    since = _iso(ctx.now - timedelta(hours=24))
    out = []
    for aid, path in ctx.ledgers():
        with _ro(path) as c:
            rows = c.execute("SELECT source_user, item_id, error_message FROM audit_log WHERE "
                             "status='SOURCE_CHANGED' AND timestamp >= ? LIMIT 200", (since,)).fetchall()
        if rows:
            ex = "\n".join(f"- {r['source_user']} {r['item_id']}: {(r['error_message'] or '')[:160]}" for r in rows[:10])
            out.append(finding("watch_source_changed", aid, "24h",
                               f"The source tenant changed during a run ({len(rows)} item(s))",
                               f"Files appeared or went on the source between the before- and after-listing "
                               f"of a user's Drive pass. Either a person worked on the source, or a copy made "
                               f"Google write there (a Form's 'Copy of'). Read each before calling it ours.\n\n{ex}",
                               "error"))
    return out


def watch_box_busy(ctx: Context) -> list[dict]:
    start = ctx.now - timedelta(hours=BUSY_HOURS)
    events = sorted(ctx.run_events(_iso(start - timedelta(hours=24))), key=lambda e: e.get("at") or "")
    open_: dict[tuple, float] = {}
    spans = []
    for e in events:
        k, at = (e.get("account_id"), e.get("job_name"), e.get("pid")), _epoch(e.get("at"))
        if at is None:
            continue
        if e.get("event") == "started":
            open_[k] = at
        elif k in open_:
            spans.append((open_.pop(k), at))
    spans += [(s, ctx.now.timestamp()) for s in open_.values()]
    if not ctx.active_jobs():
        return []
    cursor, end = start.timestamp(), ctx.now.timestamp()
    for s, e in sorted(spans):
        if s - cursor >= BUSY_GAP_SEC:
            return []
        cursor = max(cursor, e)
    if end - cursor >= BUSY_GAP_SEC:
        return []
    jobs = ", ".join(sorted({f"{j.get('account_id')}:{j.get('job_name')}" for j in ctx.active_jobs()}))
    return [finding("watch_box_busy", None, "busy",
                    f"The box has had no idle minute for {BUSY_HOURS:g}h",
                    f"No gap of {BUSY_GAP_SEC}s between jobs since {_iso(start)}, so no deploy could land "
                    f"(the deploy gate waits for idle). Running now: {jobs}.")]


def watch_tenant_ready(ctx: Context) -> list[dict]:
    if ctx.readiness is None:
        return []
    last = ctx.state.setdefault("ready", {})
    recent = {e.get("account_id") for e in ctx.run_events(_iso(ctx.now - timedelta(hours=ACTIVE_HOURS)))}
    recent |= {j.get("account_id") for j in ctx.active_jobs()}
    out = []
    for a in ctx.accounts:
        aid = a["id"]
        if aid not in recent:
            continue
        for side in ("source", "target"):
            if not a.get(f"{side}_domain"):
                continue
            key = f"{aid}:{side}"
            cached = last.get(key)
            if cached and ctx.now.timestamp() - cached["at"] < READY_EVERY_SEC:
                res = cached
            else:
                r = ctx.readiness(aid, side) or {}
                failed = [f"{c.get('name')}: {c.get('detail', '')}"[:200] for c in r.get("checks", [])
                          if c.get("ok") is False]
                res = {"at": ctx.now.timestamp(), "ready": r.get("ready"), "failed": failed}
                last[key] = res
            if res.get("ready") is False and res.get("failed"):
                out.append(finding("watch_tenant_ready", aid, side,
                                   f"{a[f'{side}_domain']} ({side}) is not ready any more",
                                   "Asked of Google as that domain's admin: " + "; ".join(res["failed"][:6])
                                   + ". A run started now fails on this; one already running may be failing.",
                                   "error"))
    return out


def watch_google_status(ctx: Context) -> list[dict]:
    if ctx.fetch_status is None:
        return []
    out = []
    for inc in ctx.fetch_status() or []:
        if inc.get("end"):
            continue
        names = [p.get("title", "") for p in inc.get("affected_products") or []] + [inc.get("service_name", "")]
        hit = [n for n in names if n and any(w in n.lower() for w in WATCHED_PRODUCTS)]
        if not hit:
            continue
        upd = ((inc.get("most_recent_update") or {}).get("text") or inc.get("external_desc") or "")[:800]
        out.append(finding("watch_google_status", None, inc.get("id") or ",".join(hit),
                           f"Google reports an open incident: {', '.join(sorted(set(hit)))}",
                           f"Begun {inc.get('begin')}; severity {inc.get('severity')}. Failures on these "
                           f"products may be Google's, not Bitport's.\n\n{upd}"))
    return out


def _domains(a: dict) -> set[str]:
    return {d.strip().lower() for d in (a.get("source_domain"), a.get("target_domain")) if d}


def _domain_re(domains) -> re.Pattern | None:
    if not domains:
        return None
    # Bounded so "gorkhatweed.com" does not match inside "new.gorkhatweed.com".
    return re.compile(r"(?<![\w.-])(" + "|".join(re.escape(d) for d in sorted(domains, key=len, reverse=True))
                      + r")(?![\w-])", re.I)


def watch_isolation(ctx: Context) -> list[dict]:
    every = set().union(*(_domains(a) for a in ctx.accounts)) if ctx.accounts else set()
    out = []
    for a in ctx.accounts:
        aid = a["id"]
        others = every - _domains(a)
        rx = _domain_re(others)
        if rx is None:
            continue
        path = ctx.ledger_path(aid)
        if path and os.path.isfile(path):
            with _ro(path) as c:
                if "identity_map" in _tables(c):
                    hits = {}
                    for r in c.execute("SELECT source_email, target_email FROM identity_map"):
                        for v in (r["source_email"], r["target_email"]):
                            m = rx.search(v or "")
                            if m:
                                hits.setdefault(m.group(1).lower(), v)
                    if hits:
                        out.append(finding("watch_isolation", aid, "identity_map:" + ",".join(sorted(hits)),
                                           f"Account {aid}'s identity map names another account's domain",
                                           "Mapped addresses on a domain configured on a different account: "
                                           + ", ".join(f"{d} ({v})" for d, v in sorted(hits.items()))
                                           + ". Each account's tenants must be its own.", "error"))
        hits = {}
        for f in glob.glob(os.path.join(ctx.logs_root, str(aid), "*.log")):
            for ln in _tail(f):
                m = rx.search(ln)
                if m:
                    hits.setdefault(m.group(1).lower(), f"{os.path.basename(f)}: {ln[:200]}")
        if hits:
            out.append(finding("watch_isolation", aid, "logs:" + ",".join(sorted(hits)),
                               f"Account {aid}'s job logs name another account's domain",
                               "\n".join(f"- {d}: {s}" for d, s in sorted(hits.items()))))
    return out


CHECKS = (watch_repair_loop, watch_slow_file, watch_verdict_flip, watch_unclassified, watch_source_changed,
          watch_box_busy, watch_tenant_ready, watch_google_status, watch_isolation)


# -- the sweep ---------------------------------------------------------------------------------------
def load_state(path: str = STATE_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: str = STATE_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)


def fingerprint(f: dict) -> str:
    return f"{FP}:{f['kind']}:{f['account_id']}:{f['key']}"


def sweep(ctx: Context, *, open_incident=None, open_incidents=None, resolve=None, checks=CHECKS) -> dict:
    """Run every check; open what holds, close what a check that ran no longer finds."""
    import run_watch
    open_incident = open_incident or run_watch.open_incident
    resolve = resolve or run_watch.resolve
    open_incidents = open_incidents or (lambda: run_watch.list_incidents("open", limit=500)
                                        + run_watch.list_incidents("acknowledged", limit=500))
    found, ran, failed = [], set(), {}
    for check in checks:
        try:
            found += check(ctx)
            ran.add(check.__name__)
        except Exception as exc:      # noqa: BLE001 - one broken check must not hide the others
            failed[check.__name__] = f"{type(exc).__name__}: {exc}"
            log.warning("oversight check %s failed: %s", check.__name__, exc)
    live = set()
    for f in found:
        fp = fingerprint(f)
        live.add(fp)
        open_incident(kind=f["kind"], title=f["title"], summary=f["summary"], account_id=f["account_id"],
                      job_name="oversight", fingerprint=fp, severity=f["severity"])
    closed = []
    for inc in open_incidents():
        fp = inc.get("fingerprint") or ""
        kind = inc.get("kind") or ""
        if fp.startswith(FP + ":") and kind in ran and fp not in live:
            resolve(inc["id"], f"{kind} ran again at {_iso(ctx.now)} and no longer finds it")
            closed.append(inc["id"])
    return {"found": len(found), "closed": closed, "ran": sorted(ran), "failed": failed}


def fetch_google_status(url: str = STATUS_URL, timeout: int = 15) -> list:
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read(8 * 1024 * 1024).decode("utf-8"))


def box_context(state: dict) -> Context:
    """The live context on the box: the control plane, every account's ledger, Google."""
    import account_context
    import accounts_auth
    import control_plane_db as cpdb
    import job_admission

    def run_events(since: str) -> list[dict]:
        with cpdb.ro() as c:
            return [dict(r) for r in c.execute("SELECT account_id, job_name, event, pid, at FROM run_events "
                                               "WHERE at >= ? ORDER BY id", (since,))]

    def readiness(aid, side):
        import readiness as rd
        from config import Settings
        return rd.check(Settings(account_id=aid), side, "migrate")

    return Context(accounts=accounts_auth.list_accounts(), ledger_path=account_context.db_path,
                   active_jobs=job_admission.list_active, run_events=run_events, readiness=readiness,
                   fetch_status=fetch_google_status, state=state)


def run_once() -> dict:
    state = load_state()
    result = sweep(box_context(state))
    save_state(state)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(run_once(), indent=2))
