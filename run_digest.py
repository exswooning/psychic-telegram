#!/usr/bin/env python3
"""
run_digest.py -- everything one experiment produced, folded into a page a person (or a model) can
read in one go.

    run_digest.py start  --account 3 --name full47      # bookmark every log and the ledger, now
    run_digest.py digest --account 3 --name full47      # read only what was appended since; write
                                                        # logs/experiments/full47/digest.{json,md}

Read-only. It never writes to a tenant, the ledger or a log it did not create.

Why a digest and not the logs: a 47-user run writes hundreds of thousands of lines, nearly all of
them the same dozen failures with different ids. Each line is reduced to a SIGNATURE (emails, Drive
and Gmail ids, numbers and quoted names blanked), counted, and kept with two raw examples. Ledger
failures are named by known_issues (RUN_KINDS / ISSUES). What nothing names is listed as
"unclassified": that list is what Bitport has not learned yet, and the next fix starts there.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_GLOBS = ("logs/jobs/{a}/*.log", "logs/dms/*.log", "logs/regression/*/*.log", "logs/incidents/feed.log",
             "logs/*.log", "logs/{a}/*.err")
INTERESTING = re.compile(r"Traceback|Error|ERROR|Exception|FAILED|FAIL\b|HTTP [45]\d\d|HttpError|refused|REFUSED|"
                         r"timed out|Timeout|WARNING|exhausted|NOT READY|INCIDENT|stalled|blocked|BLOCKED|"
                         r"UNVERIFIED|INCOMPLETE|DIFFERENCES|could not", re.I)
EXAMPLES = 2


def signature(line: str) -> str:
    """The same failure reads the same: what varies per item is blanked."""
    s = line.strip()
    s = re.sub(r"^\S*\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d\S*\s*", "", s)           # leading timestamp
    s = re.sub(r"\[[^\]]{0,40}\]\s*", "", s, count=1) if s.startswith("[") else s
    s = re.sub(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", "<email>", s)
    s = re.sub(r"https?://\S+", "<url>", s)
    # Quoted text is kept: it is the error's own words ("Entity Already Exists" vs "User creation is
    # not complete" both read <q> once, 2026-11-02). Ids, emails and numbers inside it are blanked.
    s = re.sub(r"\b[A-Za-z0-9_-]{20,}\b", "<id>", s)
    s = re.sub(r"\b[0-9a-f]{12,}\b", "<id>", s)
    s = re.sub(r"\b\d+(\.\d+)?\b", "<n>", s)
    s = re.sub(r"\s+", " ", s)
    return s[:260]


def _files(account: int) -> list[str]:
    out = set()
    for g in LOG_GLOBS:
        out.update(glob.glob(os.path.join(HERE, g.format(a=account))))
    return sorted(out)


def _ledger(account: int) -> str:
    return os.path.join(HERE, "data", "accounts", str(account), "migration.db")


def _ro(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _dir(name: str) -> str:
    d = os.path.join(HERE, "logs", "experiments", name)
    os.makedirs(d, exist_ok=True)
    return d


def start(account: int, name: str) -> dict:
    mark = {"account": account, "name": name, "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "files": {f: os.path.getsize(f) for f in _files(account)}}
    with open(os.path.join(_dir(name), "bookmark.json"), "w", encoding="utf-8") as fh:
        json.dump(mark, fh, indent=1)
    return mark


def scan_logs(mark: dict, account: int) -> dict:
    """Only what each log gained since the bookmark (a file born after it is read whole)."""
    sigs: dict[str, dict] = {}
    lines_read = 0
    for f in _files(account):
        start_at = mark["files"].get(f, 0)
        if os.path.getsize(f) < start_at:      # rotated or truncated: read it all
            start_at = 0
        kind = os.path.basename(f)
        with open(f, "rb") as fh:
            fh.seek(start_at)
            tb = None
            for raw in fh:
                lines_read += 1
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("Traceback"):
                    tb = []
                    continue
                if tb is not None:
                    if line.startswith(" "):
                        tb.append(line.strip())
                        continue
                    where = next((x for x in reversed(tb) if x.startswith("File ")), "")
                    m = re.match(r'File "([^"]+)", line \d+, in (\S+)', where)
                    at = f"{os.path.basename(m.group(1))}:{m.group(2)}" if m else "?"
                    line = f"TRACEBACK {line.strip()} at {at}"
                    tb = None
                if not INTERESTING.search(line):
                    continue
                key = f"{kind}: {signature(line)}"
                s = sigs.setdefault(key, {"log": kind, "count": 0, "examples": []})
                s["count"] += 1
                if len(s["examples"]) < EXAMPLES:
                    s["examples"].append(line.strip()[:300])
    return {"linesRead": lines_read, "signatures": sorted(sigs.items(), key=lambda kv: -kv[1]["count"])}


def scan_ledger(mark: dict, account: int) -> dict:
    sys.path.insert(0, HERE)
    import known_issues
    path = _ledger(account)
    if not os.path.exists(path):
        return {"error": f"no ledger at {path}"}
    c = _ro(path)
    since = mark["startedAt"]
    out: dict = {"byStatus": {}, "kinds": {}, "unclassified": {}}
    for r in c.execute("SELECT item_type, status, COUNT(*) n FROM audit_log WHERE timestamp >= ? "
                       "GROUP BY 1, 2 ORDER BY 3 DESC", (since,)):
        out["byStatus"][f"{r['item_type']}/{r['status']}"] = r["n"]
    for r in c.execute("SELECT item_type, status, error_message FROM audit_log WHERE timestamp >= ? AND "
                       "status NOT IN ('SUCCESS','SKIPPED_NO_DRIVE_LINK','DELIVERED_BY_DMS')", (since,)):
        text = r["error_message"] or r["status"]
        kind = known_issues.classify_failure(r["item_type"], text)
        if kind == "unclassified":
            key = f"{r['item_type']}/{r['status']}: {signature(text)}"
            u = out["unclassified"].setdefault(key, {"count": 0, "example": text[:300]})
            u["count"] += 1
        else:
            out["kinds"][kind] = out["kinds"].get(kind, 0) + 1
    users = [dict(r) for r in c.execute("SELECT source_email, target_email, status FROM identity_map "
                                        "WHERE entity_type='user' ORDER BY source_email")]
    verdicts = {}
    for r in c.execute("SELECT source_user, service, verdict, checked, identical, sampled_of, verified_at "
                       "FROM user_verification"):
        verdicts.setdefault(r["source_user"], {})[r["service"]] = {
            "verdict": r["verdict"], "checked": r["checked"], "identical": r["identical"],
            "sampled": r["sampled_of"] is not None, "fresh": r["verified_at"] >= since}
    tallies = {r["source_user"]: {"parity": r["count_parity"], "fresh": r["recorded_at"] >= since}
               for r in c.execute("SELECT source_user, count_parity, recorded_at FROM user_tally")}
    try:
        unverified = {f"{r[0]}/{r[1]}": r[2] for r in c.execute(
            "SELECT service, state, COUNT(*) FROM verification_unverified GROUP BY 1, 2")}
    except sqlite3.OperationalError:
        unverified = {}
    out.update(users=users, verdicts=verdicts, tallies=tallies, unverified=unverified)
    out["unclassified"] = dict(sorted(out["unclassified"].items(), key=lambda kv: -kv[1]["count"]))
    return out


def scan_runs(mark: dict, account: int) -> list[dict]:
    """Every job of this account since the bookmark, paired started/finished, with its duration."""
    sys.path.insert(0, HERE)
    try:
        import control_plane_db as cpdb
        with cpdb.ro() as c:
            rows = c.execute("SELECT at, job_name, event, pid, rc FROM run_events WHERE account_id=? AND at >= ? "
                             "ORDER BY id", (account, mark["startedAt"])).fetchall()
    except Exception as exc:      # noqa: BLE001 - a digest without timings is still a digest
        return [{"error": str(exc)[:200]}]
    open_, out = {}, []
    for r in rows:
        key = (r["job_name"], r["pid"])
        if r["event"] == "started":
            open_[key] = r["at"]
        else:
            began = open_.pop(key, None)
            secs = None
            if began:
                fmt = "%Y-%m-%dT%H:%M:%S"
                secs = int(time.mktime(time.strptime(r["at"][:19], fmt)) - time.mktime(time.strptime(began[:19], fmt)))
            out.append({"job": r["job_name"], "pid": r["pid"], "rc": r["rc"], "started": began,
                        "finished": r["at"], "seconds": secs})
    out += [{"job": j, "pid": p, "rc": None, "started": at, "finished": None, "seconds": None}
            for (j, p), at in open_.items()]
    return out


def render(d: dict) -> str:
    """The page: what ran and how long, verdicts per user, failures by kind, and -- first among the
    problems -- what nothing in Bitport names yet."""
    L = [f"# Experiment {d['name']} (account {d['account']}, since {d['since']})", ""]
    L += ["## Jobs", "| job | rc | started | minutes |", "|---|---|---|---|"]
    L += [f"| {r['job']} | {r['rc']} | {r['started']} | {round(r['seconds'] / 60, 1) if r['seconds'] else '-'} |"
          for r in d["runs"] if "job" in r]
    led = d["ledger"]
    L += ["", "## Ledger since start", "| item/status | n |", "|---|---|"]
    L += [f"| {k} | {v:,} |" for k, v in list(led.get("byStatus", {}).items())[:25]]
    L += ["", "## Failures by known kind", "| kind | n |", "|---|---|"]
    L += [f"| {k} | {v:,} |" for k, v in sorted(led.get("kinds", {}).items(), key=lambda kv: -kv[1])]
    L += ["", "## NOT YET LEARNED (no known_issues kind matches)", "| n | signature | example |", "|---|---|---|"]
    L += [f"| {v['count']:,} | {k[:110]} | {v['example'][:140]} |" for k, v in list(led.get("unclassified", {}).items())[:40]]
    L += ["", "## Per user", "| user | target | status | services (verdict, fresh?) | tally parity |", "|---|---|---|---|---|"]
    for u in led.get("users", []):
        v = led["verdicts"].get(u["source_email"], {})
        vs = ", ".join(f"{s}:{x['verdict']}{'' if x['fresh'] else '(old)'}{'(sample)' if x['sampled'] else ''}"
                       for s, x in sorted(v.items())) or "NOT VERIFIED"
        t = led["tallies"].get(u["source_email"])
        tp = (f"{t['parity']}" + ("" if t["fresh"] else " (old)")) if t else "NOT TALLIED"
        L.append(f"| {u['source_email']} | {u['target_email']} | {u['status']} | {vs} | {tp} |")
    if led.get("unverified"):
        L += ["", "## Unverified objects (not identical, not different: unread)",
              *[f"- {k}: {v:,}" for k, v in led["unverified"].items()]]
    logs = d["logs"]
    L += ["", f"## Log signatures ({logs['linesRead']:,} new lines read)", "| n | log | signature |", "|---|---|---|"]
    L += [f"| {v['count']:,} | {v['log']} | {k.split(': ', 1)[1][:150]} |" for k, v in logs["signatures"][:60]]
    return "\n".join(L) + "\n"


def digest(account: int, name: str) -> dict:
    with open(os.path.join(_dir(name), "bookmark.json"), encoding="utf-8") as fh:
        mark = json.load(fh)
    d = {"name": name, "account": account, "since": mark["startedAt"],
         "builtAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "runs": scan_runs(mark, account), "ledger": scan_ledger(mark, account), "logs": scan_logs(mark, account)}
    out = _dir(name)
    with open(os.path.join(out, "digest.json"), "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1, default=str)
    with open(os.path.join(out, "digest.md"), "w", encoding="utf-8") as fh:
        fh.write(render(d))
    return d


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("action", choices=("start", "digest"))
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--name", required=True)
    a = ap.parse_args(argv)
    if a.action == "start":
        m = start(a.account, a.name)
        print(f"bookmarked {len(m['files'])} log(s) at {m['startedAt']}")
    else:
        d = digest(a.account, a.name)
        print(f"digest: logs/experiments/{a.name}/digest.md -- {len(d['logs']['signatures'])} signature(s), "
              f"{len(d['ledger'].get('unclassified', {}))} unclassified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
