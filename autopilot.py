"""Keep a migration moving when nobody is watching: a fixed playbook, a local model only to summarise.

Runs on the box (unit `bitport-autopilot`). Reads new lines of the incident feed and the parity loop's log
once a minute and decides with `decide()` -- a fixed list of actions, each guarded:

  run crashed (INCIDENT ... migrate exited with code N)   resume the run as launched (code 3, the memory
                                                         pause: one process fewer); at most 3 a day
  run STOPPED by anyone                                  hold -- a stop is a person's decision, never undone
  parity: <user>: drive: drive mtime xN                  repair.fix_modified_times for that user (target
                                                         writes only; repair's own guard stands down
                                                         beside another Drive job)
  the parity loop is not running while a run is          restart it from the run's start
  anything else                                          needs Claude: a local model (Ollama, if present)
                                                         writes a short summary; nothing is executed

Every decision goes to logs/autopilot/INBOX.md and, for anything not handled, an `ERROR [autopilot]
needs Claude:` line logwatch picks up. The model never chooses an action: it only summarises.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(HERE, "logs", "autopilot")
STATE = os.path.join(DIR, "state.json")
INBOX = os.path.join(DIR, "INBOX.md")
OLLAMA = os.getenv("AUTOPILOT_OLLAMA", "http://127.0.0.1:11434")
MODEL = os.getenv("AUTOPILOT_MODEL", "qwen2.5-coder:7b")
MAX_RESUMES_DAY = 3

CRASH = re.compile(r"INCIDENT #\d+ \w+ crashed account=(\d+) job=migrate :: migrate exited with code (-?\d+)")
STOPPED = re.compile(r"STOPPED account=(\d+) job=migrate .* by (.+?)(?: report=|$)")
PARITY = re.compile(r"\[parity\] parity: (\S+@\S+): (\w[\w-]*): (.+)$")


def decide(line: str, state: dict, account: int) -> dict | None:
    """The playbook. A line -> one action dict, or None (nothing to do)."""
    m = CRASH.search(line)
    if m and int(m.group(1)) == account:
        if state.get("held"):
            return {"do": "hold", "why": f"run held since {state['held']}; a crash does not lift a person's stop"}
        today = time.strftime("%Y-%m-%d", time.gmtime())
        n = sum(1 for d in state.get("resumes", []) if d.startswith(today))
        if n >= MAX_RESUMES_DAY:
            return {"do": "needs_claude", "why": f"crashed again (code {m.group(2)}) after {n} resumes today"}
        return {"do": "resume", "fewer_processes": m.group(2) == "3", "code": int(m.group(2))}
    m = STOPPED.search(line)
    if m and int(m.group(1)) == account:
        stamp = line[:20]                            # the feed's own time, e.g. 2026-11-10T06:33:36Z
        if operator_launched_after(stamp, fetch=fetch_actions):
            return None                              # the operator stopped it to restart it: not a hold
        return {"do": "hold", "by": m.group(2).strip(), "why": "a stop is a person's decision"}
    m = PARITY.search(line)
    if m and " ERROR " in line:
        user, stage, why = m.group(1), m.group(2), m.group(3)
        if stage == "drive" and re.fullmatch(r"drive mtime x\d+", why.strip()):
            return {"do": "fix_mtime", "user": user}
        return {"do": "needs_claude", "why": f"{user}: {stage}: {why}"}
    if "account=" in line and f"account={account} " not in line:
        return None                                  # another account's: not ours to act on or raise
    if " ERROR " in line or "INCIDENT" in line and " error " in line:
        return {"do": "needs_claude", "why": line.strip()[:300]}
    return None


def fetch_actions(limit: int = 60) -> list:
    """The API's audit log of operator actions, newest first."""
    sess = open("/root/.bp_session").read().strip()
    out = subprocess.run(["curl", "-s", "-m", "30", "-H", f"Cookie: bp_session={sess}",
                          f"http://localhost:8090/api/v2/actions?limit={limit}"], capture_output=True, text=True)
    return json.loads(out.stdout or "[]")


def operator_launched_after(stamp: str, fetch=fetch_actions) -> bool:
    """Did an operator's own migrate.start follow this stop? Then the stop was the operator restarting
    the run (10-11 Nov 2026: a restart re-held the run, so a crash would not have been resumed)."""
    try:
        rows = fetch()
    except Exception:      # noqa: BLE001 - cannot tell: keep the hold (the safe side)
        return False
    for a in rows:
        if a.get("action") == "migrate.start" and str(a.get("actor", "")).startswith("Administrator") \
                and str(a.get("started_at", ""))[:19] > stamp[:19]:
            return True
    return False


def summarise(text: str, ask=None) -> str:
    """A short plain summary from the local model, or '' when it is not there. Never an action."""
    prompt = ("You summarise one error from a Google Workspace migration tool for an engineer. In two "
              "sentences: what failed and the most likely cause. Do not suggest commands.\n\n" + text[:3000])
    try:
        if ask:
            return ask(prompt)
        req = urllib.request.Request(f"{OLLAMA}/api/generate", data=json.dumps(
            {"model": MODEL, "prompt": prompt, "stream": False, "options": {"num_predict": 120}}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return (json.loads(r.read()).get("response") or "").strip()
    except Exception:      # noqa: BLE001 - no model is fine: Claude reads the raw line
        return ""


def _api(path: str, body: dict) -> str:
    sess = open("/root/.bp_session").read().strip()
    out = subprocess.run(["curl", "-s", "-m", "120", "-H", f"Cookie: bp_session={sess}", "-H", "Content-Type: application/json",
                          "-X", "POST", f"http://localhost:8090{path}", "-d", json.dumps(body)],
                         capture_output=True, text=True)
    return (out.stdout or out.stderr)[:300]


def act(a: dict, state: dict, account: int) -> str:
    if a["do"] == "resume":
        body = dict(state.get("launch") or {})
        if not body:
            return "no launch recorded: needs Claude"
        if a.get("fewer_processes"):
            body["processes"] = max(1, int(body.get("processes") or 4) - 1)
            state["launch"] = body
        body["reason"] = f"autopilot: resume after the run exited with code {a['code']}"
        state.setdefault("resumes", []).append(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        out = _api("/api/v2/migrate/start", body)
        _api("/api/v2/repair-policy", {"account_id": account, "enabled": False, "reason": "autopilot: a launch switches repairs on"})
        return out
    if a["do"] == "hold":
        state["held"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return f"held ({a.get('by', '')})"
    if a["do"] == "fix_mtime":
        import repair
        from auth import AuthManager
        from config import Settings
        from db import MigrationDB
        st = Settings(account_id=account)
        r = repair.fix_modified_times(AuthManager(st), MigrationDB(st.db_path), st, apply=True, users={a["user"]})
        return json.dumps({k: v for k, v in r.items() if not isinstance(v, (list, dict))})
    return ""


REPORT = os.path.join(DIR, "REPORT.md")


def shape(text: str) -> str:
    """A message with addresses, ids and numbers taken out: the same problem on 37 users is one shape."""
    t = re.sub(r"\S+@\S+", "<u>", text.lower())
    t = re.sub(r"\b(?=[\w-]*\d)[\w-]{12,}\b", "<id>", t)
    return re.sub(r"\d+", "#", t)[:110]


def first_of_shape(state: dict, why: str, who: str = "") -> bool:
    """True the first time a shape is seen (escalate it); later ones only count (xN in the report).
    10 Nov 2026: 130 escalations were 6 shapes -- 111 were one false alarm across 37 users."""
    sh = state.setdefault("shapes", {})
    e = sh.setdefault(shape(why), {"n": 0, "ex": why[:120], "who": who.split("@")[0], "t": time.strftime("%m-%d %H:%MZ", time.gmtime())})
    e["n"] += 1
    return e["n"] == 1


def deviations(env: dict, launch: dict) -> list[str]:
    """How a RUNNING migration differs from the launch the operator chose -- e.g. one started from the
    web UI with its own defaults (10 Nov 2026: 8 processes, mail moved whole by the engine)."""
    out = []
    want_split = launch.get("mail_mode") == "split"
    if want_split != (env.get("MAIL_ONLY_WITH_LINKS", "").lower() == "true"):
        out.append(f"mail mode: wanted {launch.get('mail_mode')}, MAIL_ONLY_WITH_LINKS={env.get('MAIL_ONLY_WITH_LINKS', '')!r}")
    if launch.get("processes") and env.get("MIGRATE_PROCESSES") not in (None, str(launch["processes"])):
        out.append(f"processes: wanted {launch['processes']}, running {env.get('MIGRATE_PROCESSES')}")
    return out


def record(state: dict, kind: str, who: str = "", detail: str = "") -> None:
    """One compact event for the report; the list stays short."""
    ev = state.setdefault("events", [])
    ev.append({"t": time.strftime("%m-%d %H:%MZ", time.gmtime()), "k": kind, "who": who.split("@")[0], "d": detail[:90]})
    del ev[:-200]


def build_report(state: dict, parity: dict | None = None, bad: list | None = None, run: str = "") -> str:
    """The whole report in about 15 lines: what ran, what it fixed, what waits for Claude."""
    ev = state.get("events", [])
    fixed: dict = {}
    for e in ev:
        if e["k"] in ("fix_mtime", "resume", "parity_restart"):
            fixed.setdefault(e["k"], []).append(e["who"] or "run")
    shapes = state.get("shapes", {})
    waiting = {k: v for k, v in shapes.items() if v.get("open", True)}
    lines = [f"autopilot report {time.strftime('%m-%d %H:%MZ', time.gmtime())} | run: {run or '?'} | "
             f"held: {state.get('held', 'no')} | resumes today: "
             f"{sum(1 for d in state.get('resumes', []) if d.startswith(time.strftime('%Y-%m-%d', time.gmtime())))}"]
    if parity:
        lines.append("parity: " + "; ".join(f"{st} " + "/".join(f"{n} {v.replace('NOT_AT_PARITY', 'BAD').replace('PARITY', 'ok')}"
                                                              for v, n in sorted(vs.items())) for st, vs in sorted(parity.items())))
    if bad:
        lines.append("not at parity: " + ", ".join(u.split("@")[0] for u in bad[:12]) + (f" +{len(bad) - 12}" if len(bad) > 12 else ""))
    lines.append("fixed by itself: " + ("; ".join(f"{k} x{len(v)} ({', '.join(sorted(set(v))[:6])})" for k, v in fixed.items()) or "nothing yet"))
    lines.append(f"waiting for Claude ({len(waiting)} kinds, {sum(v['n'] for v in waiting.values())} events):" if waiting else "waiting for Claude: nothing")
    for k, v in sorted(waiting.items(), key=lambda kv: -kv[1]["n"])[:8]:
        lines.append(f"- x{v['n']} {v['t']} {v['who']}: {v['ex']}")
    return "\n".join(lines) + "\n"


def write_report(state: dict, account: int) -> None:
    parity, bad = None, []
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{HERE}/data/accounts/{account}/migration.db?mode=ro", uri=True)
        parity = {}
        for st, v, n in c.execute("SELECT stage, verdict, COUNT(*) FROM parity_user GROUP BY 1, 2"):
            parity.setdefault(st, {})[v] = n
        bad = [u for (u,) in c.execute("SELECT DISTINCT source_user FROM parity_user WHERE verdict!='PARITY'")]
    except Exception:      # noqa: BLE001 - no ledger yet: the rest of the report still stands
        pass
    run = subprocess.run(["pgrep", "-f", f"main.py --account-id {account} migrate"], capture_output=True, text=True).stdout.split()
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write(build_report(state, parity, bad, run[0] if run else "stopped"))


def _note(text: str) -> None:
    os.makedirs(DIR, exist_ok=True)
    with open(INBOX, "a", encoding="utf-8") as fh:
        fh.write(f"- {time.strftime('%Y-%m-%d %H:%M', time.gmtime())}Z {text}\n")


def _say(level: str, msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())},000 {level:<7} [autopilot] {msg}", flush=True)


def _unit_active(name: str) -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", name]).returncode == 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-id", type=int, required=True)
    ap.add_argument("--report", action="store_true", help="print the current report and exit")
    args = ap.parse_args(argv)
    os.makedirs(DIR, exist_ok=True)
    if args.report:
        print(open(REPORT, encoding="utf-8").read() if os.path.exists(REPORT) else "no report yet")
        return 0
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    feeds = {"feed": os.path.join(HERE, "logs", "incidents", "feed.log"),
             "parity": os.path.join(HERE, "logs", "jobs", str(args.account_id), "parity.log")}
    offs = state.setdefault("offsets", {})
    for k, p in feeds.items():
        offs.setdefault(k, os.path.getsize(p) if os.path.exists(p) else 0)
    _say("INFO", f"watching {', '.join(feeds.values())}")
    while True:
        for k, p in feeds.items():
            if not os.path.exists(p):
                continue
            with open(p, "rb") as fh:
                fh.seek(offs[k])
                chunk = fh.read()
            offs[k] += len(chunk)
            for line in chunk.decode("utf-8", "replace").splitlines():
                a = decide(line, state, args.account_id)
                if not a:
                    continue
                if a["do"] == "needs_claude":
                    if not first_of_shape(state, a["why"].split(": ", 1)[-1], a["why"].split(": ")[0] if "@" in a["why"][:60] else ""):
                        continue                     # the same problem again: counted, not repeated
                    record(state, "needs_claude", (a["why"].split(": ")[0] if "@" in a["why"][:60] else ""), a["why"].split(": ", 1)[-1])
                    s = summarise(a["why"])
                    _note(f"NEEDS CLAUDE: {a['why'][:300]}" + (f"\n  local model: {s[:400]}" if s else ""))
                    _say("ERROR", f"needs Claude: {a['why'][:200]}")
                    continue
                try:
                    out = act(a, state, args.account_id)
                    record(state, a["do"], a.get("user", a.get("by", "")), out[:90])
                    _note(f"{a['do']}: {json.dumps({x: y for x, y in a.items() if x != 'do'})} -> {out}")
                    _say("INFO", f"{a['do']} done: {out[:160]}")
                except Exception as exc:      # noqa: BLE001
                    _note(f"{a['do']} FAILED: {exc}")
                    _say("ERROR", f"needs Claude: {a['do']} failed: {str(exc)[:200]}")
        pids = subprocess.run(["pgrep", "-f", f"main.py --account-id {args.account_id} migrate"],
                              capture_output=True, text=True).stdout.split()
        running = bool(pids)
        if running and state.get("launch"):
            try:
                env = dict(kv.split("=", 1) for kv in open(f"/proc/{pids[0]}/environ").read().split("\0") if "=" in kv)
                dev = deviations(env, state["launch"])
                if dev and first_of_shape(state, "run deviates from the chosen launch: " + "; ".join(dev)):
                    _note("NEEDS CLAUDE: run deviates: " + "; ".join(dev))
                    _say("ERROR", "needs Claude: run deviates from the chosen launch: " + "; ".join(dev))
            except OSError:
                pass
        if running and not _unit_active("bitport-parity") and state.get("parity_since"):
            subprocess.run(["systemctl", "reset-failed", "bitport-parity"], capture_output=True)
            subprocess.run(["systemd-run", "--unit=bitport-parity", "--working-directory=" + HERE,
                            f"--setenv=MIGRATION_DB={HERE}/data/accounts/{args.account_id}/migration.db", "/bin/sh", "-c",
                            f"{HERE}/.venv/bin/python -u parity_loop.py --account-id {args.account_id} --from-start "
                            f"--since '{state['parity_since']}' --workers 2 >> logs/jobs/{args.account_id}/parity.log 2>&1"],
                           capture_output=True)
            _note("restarted the parity loop")
            record(state, "parity_restart")
        with open(STATE, "w") as fh:
            json.dump(state, fh, indent=1)
        try:
            write_report(state, args.account_id)
        except Exception:      # noqa: BLE001 - a report must never stop the autopilot
            pass
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
