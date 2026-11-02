#!/usr/bin/env python3
"""
monitor/watch.py -- an unattended error feed for running jobs, spending tokens only on news.

    python3 monitor/watch.py            # run forever (nohup / launchd); one poll a minute
    python3 monitor/watch.py --once     # one poll, then exit
    python3 monitor/watch.py --brief    # what an interactive session needs after a context reset

Zero-LLM path first: the box's incident feed is read over SSH; a repeat (same fingerprint) is dropped,
a RESOLVED line closes its incident, and a kind known_issues marks automatic is only logged. What is
left is triaged by the cheapest model, and only an "escalate" reaches the strong one, which works in
its own git worktree and never deploys. Each running job is its own LANE (account:job): lanes run in
parallel, a lane is serial, so two jobs mean two feeds.

Everything it knows lives in monitor/state.json (offset, fingerprints, attempts, spend), and every
model call starts fresh from monitor/CHARTER.md plus that state, so a reset of anyone's context loses
nothing. Caps (per fingerprint, per day, dollars per day) stop it from becoming the cost it watches.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATE = os.path.join(HERE, "state.json")
CHARTER = os.path.join(HERE, "CHARTER.md")
INBOX = os.path.join(HERE, "INBOX.md")

TRIAGE_MODEL = os.getenv("MONITOR_TRIAGE_MODEL", "haiku")
FIX_MODEL = os.getenv("MONITOR_FIX_MODEL", "opus")
POLL_SEC = int(os.getenv("MONITOR_POLL_SEC", "60"))
MAX_ATTEMPTS = int(os.getenv("MONITOR_MAX_ATTEMPTS", "2"))           # per fingerprint
MAX_ESCALATIONS_DAY = int(os.getenv("MONITOR_MAX_ESCALATIONS_DAY", "6"))
MAX_USD_DAY = float(os.getenv("MONITOR_MAX_USD_DAY", "5"))
TRIAGE_USD, FIX_USD = float(os.getenv("MONITOR_TRIAGE_USD", "0.05")), float(os.getenv("MONITOR_FIX_USD", "3"))
BRIEF_CHARS = int(os.getenv("MONITOR_BRIEF_CHARS", "2000"))
BOX_CMD = os.getenv("BITPORT_BOX_CMD", "/tmp/claude-501/box.sh")     # runs one command on the box
FEED = os.getenv("MONITOR_FEED_PATH", "logs/incidents/feed.log")

LINE = re.compile(r"^(?P<ts>\S+) (?P<what>INCIDENT|RESOLVED) #(?P<id>\d+)(?: (?P<sev>error|warn|info|critical) (?P<kind>\w+))?(?: automatically)?"
                  r"(?: account=(?P<account>\d+))?(?: job=(?P<job>.*?))?(?: :: (?P<msg>.*?))?(?: -> brief: (?P<brief>\S+))?\s*$")


def parse(line: str) -> dict | None:
    m = LINE.match(line.strip())
    return {k: v for k, v in m.groupdict().items() if v is not None} if m else None


def lane_of(ev: dict) -> str:
    return f"{ev.get('account', '?')}:{(ev.get('job') or 'job').split(':')[0].strip().lower()}"


def fingerprint(ev: dict) -> str:
    """The same failure reads the same: numbers, ids and times are blanked."""
    msg = re.sub(r"\b[0-9a-f]{8,}\b|\d+", "#", (ev.get("msg") or "").lower())
    return hashlib.sha1(f"{lane_of(ev)}|{ev.get('kind')}|{msg}".encode()).hexdigest()[:12]


# -- state -------------------------------------------------------------------------------------------
_LOCK = threading.Lock()


def load(path: str = STATE) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"fps": {}, "days": {}}


def save(state: dict, path: str = STATE) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def day(state: dict) -> dict:
    return state["days"].setdefault(today(), {"usd": 0.0, "escalations": 0, "triages": 0})


# -- the model calls (injected in tests) -------------------------------------------------------------
def run_claude(model: str, prompt: str, cwd: str | None = None, tools: str = "", budget: float = 0.05,
               timeout: int = 900, extra: list[str] | None = None) -> tuple[str, float]:
    """One headless call. Fresh session every time -- nothing carries over except what we put in."""
    cmd = ["claude", "-p", "--model", model, "--output-format", "json", "--no-session-persistence",
           "--max-budget-usd", str(budget), "--tools", tools] + (extra or [])
    out = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd or ROOT, timeout=timeout)
    try:
        body = json.loads(out.stdout)
        return str(body.get("result", "")), float(body.get("total_cost_usd", 0) or 0)
    except ValueError:
        return out.stdout[-500:] + out.stderr[-300:], 0.0


def json_of(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None


def charter() -> str:
    with open(CHARTER, encoding="utf-8") as fh:
        return fh.read()


def box(command: str) -> str:
    return subprocess.run([BOX_CMD, command], capture_output=True, text=True, timeout=120).stdout


def fetch_feed(offset: int) -> tuple[list[str], int]:
    """New feed lines since `offset` bytes. A shrunk file (rotated) starts over."""
    out = box(f"s=$(stat -c %s {FEED}); echo SIZE=$s; tail -c +{offset + 1} {FEED} | head -c 200000")
    head, _, body = out.partition("\n")
    size = int(head.split("=")[1]) if head.startswith("SIZE=") else offset
    if size < offset:
        return fetch_feed(0)
    return body.splitlines(), offset + len(body.encode())


def fetch_brief(path: str) -> str:
    return box(f"head -c {BRIEF_CHARS} {path}")


# -- the pipeline ------------------------------------------------------------------------------------
def known_auto(ev: dict) -> str | None:
    """The id of a known_issues kind Bitport already fixes on its own that this incident's words fit."""
    sys.path.insert(0, ROOT)
    import known_issues
    text = ev.get("msg", "")
    for issue_id, _svc, matches, *_rest in known_issues.ISSUES:
        if known_issues.BY_ID[issue_id][6] and matches(text):
            return issue_id
    return None


def triage(ev: dict, brief: str, prior: dict, run=run_claude) -> tuple[dict | None, float]:
    prompt = (f"{charter()}\n\nINCIDENT #{ev['id']} {ev.get('sev')} {ev.get('kind')} lane={lane_of(ev)}\n"
              f"{ev.get('msg', '')}\n\nBRIEF (truncated):\n{brief}\n\nEARLIER ATTEMPTS: "
              f"{json.dumps(prior.get('history', [])[-2:])}\n\nAnswer with the triage JSON only.")
    text, cost = run(TRIAGE_MODEL, prompt, budget=TRIAGE_USD)
    return json_of(text), cost


def escalate(ev: dict, brief: str, why: dict, run=run_claude, git=subprocess.run) -> tuple[dict | None, float, str]:
    """The strong model in its own worktree on its own branch. It never deploys; the watcher pushes
    the branch only after a fix is claimed, and the operator merges and deploys."""
    branch = f"monitor/incident-{ev['id']}"
    wt = os.path.join(ROOT, ".claude", "worktrees", f"monitor-{ev['id']}")
    git(["git", "worktree", "add", "-B", branch, wt, "HEAD"], cwd=ROOT, capture_output=True)
    prompt = (f"{charter()}\n\nYou are the fixer. INCIDENT #{ev['id']} lane={lane_of(ev)}: {ev.get('msg', '')}\n"
              f"Triage said: {json.dumps(why)}\n\nBRIEF:\n{brief}\n\nWork only in this worktree "
              f"({wt}). Reproduce, fix, add a test, run the relevant tests, commit on this branch. Answer "
              f"with the fixer JSON only.")
    text, cost = run(FIX_MODEL, prompt, cwd=wt, tools="Read,Grep,Glob,Edit,Write,Bash", budget=FIX_USD, timeout=3600,
                     extra=["--permission-mode", "acceptEdits", "--disallowedTools",
                            "Bash(git push *) Bash(ssh *) Bash(scp *) Bash(*sync_vps*) Bash(*systemctl*)"])
    return json_of(text), cost, branch


def handle(state: dict, ev: dict, run=run_claude, fetch=fetch_brief, git=subprocess.run) -> str:
    """One incident through the funnel; returns what happened. Pure of I/O except the injected calls
    and the state file, so every branch is testable."""
    fp, lane = fingerprint(ev), lane_of(ev)
    with _LOCK:
        rec = state["fps"].setdefault(fp, {"lane": lane, "kind": ev.get("kind"), "ids": [], "status": "new",
                                           "attempts": 0, "history": []})
        if ev["id"] not in rec["ids"]:
            rec["ids"].append(ev["id"])
        d = day(state)
        if rec["status"] in ("auto", "ignored", "fixed", "closed") or rec["attempts"] >= MAX_ATTEMPTS:
            return f"skip:{rec['status']}"
        if d["usd"] >= MAX_USD_DAY:
            return "skip:budget"
        auto = known_auto(ev)
        if auto:
            rec.update(status="auto", note=f"known kind {auto}: Bitport handles it")
            return "auto"
        rec["attempts"] += 1
        d["triages"] += 1
    brief = fetch(ev["brief"]) if ev.get("brief") else ""
    verdict, cost = triage(ev, brief, rec, run)
    with _LOCK:
        d["usd"] += cost
        rec["history"].append({"t": time.time(), "triage": verdict})
        action = (verdict or {}).get("action", "note")
        if action == "ignore":
            rec["status"] = "ignored"
            return "ignored"
        if action != "escalate" or d["escalations"] >= MAX_ESCALATIONS_DAY or d["usd"] >= MAX_USD_DAY:
            rec["status"] = "noted" if action != "escalate" else "queued-over-cap"
            return rec["status"]
        d["escalations"] += 1
    fixed, cost2, branch = escalate(ev, brief, verdict or {}, run, git)
    with _LOCK:
        d["usd"] += cost2
        rec["history"].append({"t": time.time(), "fix": fixed, "branch": branch})
        ok = (fixed or {}).get("status") == "fixed"
        rec["status"] = "fixed" if ok else "open"
        line = (f"- incident #{ev['id']} [{lane}] {'FIXED on ' + branch if ok else 'could not fix'}: "
                f"{(fixed or {}).get('summary', 'no answer')}  (review, merge, deploy at your moment)\n")
        with open(INBOX, "a", encoding="utf-8") as fh:
            fh.write(line)
        return "fixed" if ok else "open"


def close(state: dict, ev: dict) -> None:
    for rec in state["fps"].values():
        if int(ev["id"]) in [int(i) for i in rec["ids"]] and rec["status"] in ("new", "open", "noted"):
            rec["status"] = "closed"


def poll(state: dict, lines: list[str], pool: ThreadPoolExecutor, run=run_claude, fetch=fetch_brief,
         git=subprocess.run) -> list:
    """Feed lines in; one task per new incident, serialised inside a lane, parallel across lanes."""
    futures, lane_locks = [], state.setdefault("_locks", {})
    for line in lines:
        ev = parse(line)
        if not ev:
            continue
        if ev["what"] == "RESOLVED":
            close(state, ev)
            continue
        lock = lane_locks.setdefault(lane_of(ev), threading.Lock())

        def task(ev=ev, lock=lock):
            with lock:
                return handle(state, ev, run, fetch, git)
        futures.append(pool.submit(task))
    return futures


def brief(state: dict) -> str:
    """What a fresh interactive session needs to take the job back: the charter's heading, open items,
    today's spend. Printed by the SessionStart hook so a context reset never drops the duty."""
    open_ = [(fp, r) for fp, r in state["fps"].items() if r["status"] in ("open", "noted", "queued-over-cap", "new")]
    d = state["days"].get(today(), {"usd": 0, "escalations": 0})
    out = ["JOB MONITOR (monitor/CHARTER.md): an unattended watcher feeds new incidents to a cheap model and "
           "escalates to a strong one in a worktree; you never deploy. Re-read the charter if you take a case.",
           f"today: ${d['usd']:.2f} spent, {d['escalations']} escalation(s); open: {len(open_)}"]
    out += [f"  {r['lane']} {r['kind']} {r['status']} attempts={r['attempts']} incidents={r['ids'][-3:]}"
            for _, r in open_[:8]]
    if os.path.exists(INBOX):
        with open(INBOX, encoding="utf-8") as fh:
            out += ["inbox (needs your go):"] + fh.read().splitlines()[-5:]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="read the feed from the start and say what each incident would do; no model, no state")
    args = ap.parse_args(argv)
    state = load()
    if args.dry_run:
        lines, _ = fetch_feed(0)
        for ev in filter(None, map(parse, lines)):
            if ev["what"] == "INCIDENT":
                print(f"#{ev['id']:>3} lane={lane_of(ev):<22} fp={fingerprint(ev)} "
                      f"{'AUTO ' + known_auto(ev) if known_auto(ev) else 'needs triage'}  {ev.get('msg', '')[:70]}")
        return 0
    if args.brief:
        print(brief(state))
        return 0
    if "offset" not in state:
        # A first start acts on NEWS only: the feed's history is read past, not re-litigated (and not paid for).
        _, state["offset"] = fetch_feed(0)
        save(state)
    pool = ThreadPoolExecutor(max_workers=int(os.getenv("MONITOR_MAX_LANES", "4")))
    while True:
        try:
            lines, state["offset"] = fetch_feed(state["offset"])
            futures = poll(state, lines, pool)
            for f in futures:
                f.result()
        except Exception as exc:      # noqa: BLE001 - a watcher that dies watches nothing
            print(f"monitor: poll failed: {exc!r}", file=sys.stderr, flush=True)
        with _LOCK:
            save({k: v for k, v in state.items() if k != "_locks"})
        if args.once:
            return 0
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
