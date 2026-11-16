"""campaign.py -- run the scenarios of campaign_scenarios.py, one result line each (fableplan2 P2.1).

    python campaign.py --account-id ID --name NAME [--group G] [--only id,id] [--redo] [--dry-run]

Runs on the box, as root, so it needs no password: it makes its own session for the superadmin the way the
tests do. Every request is the one the UI's button sends, with `reason: "campaign <name> <id>"`. It never
wipes a tenant. A result is PASS, FAIL, UNVERIFIED (a check nobody could make, or a scenario with no `act`
yet -- never a pass), SKIPPED (the operator did not allow it) or COVERED (cannot be caused live; its unit
test stands). Results append to logs/campaigns/<name>/results.jsonl and RESULTS.md is rewritten.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import campaign_scenarios as S

HERE = os.path.dirname(os.path.abspath(__file__))
PASSING = {"PASS", "COVERED"}


def results_dir(name: str) -> str:
    return os.path.join(HERE, "logs", "campaigns", name)


def read_results(name: str) -> dict:
    """{scenario id: its latest result line}."""
    out = {}
    try:
        with open(os.path.join(results_dir(name), "results.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    row = json.loads(line)
                    out[row["id"]] = row
    except FileNotFoundError:
        pass
    return out


def append_result(name: str, row: dict) -> None:
    os.makedirs(results_dir(name), exist_ok=True)
    with open(os.path.join(results_dir(name), "results.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def render_md(name: str, scenarios: list, results: dict) -> str:
    """One table row per scenario: its latest status, or NOT RUN."""
    tally = {}
    for sc in scenarios:
        st = results.get(sc.id, {}).get("status", "NOT RUN")
        tally[st] = tally.get(st, 0) + 1
    lines = [f"# Campaign {name}", "", ", ".join(f"{n} {k}" for k, n in sorted(tally.items())), "",
             "| Group | Scenario | Status | Why |", "|---|---|---|---|"]
    for sc in scenarios:
        r = results.get(sc.id, {})
        lines.append(f"| {sc.group} | {sc.id} | {r.get('status', 'NOT RUN')} | {str(r.get('reason', ''))[:110]} |")
    return "\n".join(lines) + "\n"


class Driver:
    """What a scenario needs from the live system. LiveDriver is the box; tests use a fake."""
    def take_twin(self) -> str: raise NotImplementedError
    def prepare(self, fixtures: tuple) -> None: raise NotImplementedError
    def api(self, method: str, path: str, body: dict | None) -> dict: raise NotImplementedError
    def inject(self, inject: dict) -> None: raise NotImplementedError
    def wait_idle(self, timeout: int) -> bool: raise NotImplementedError
    def facts(self, user: str | None) -> dict: raise NotImplementedError   # what judges read; see judge()


def judge(expect: dict, facts: dict) -> tuple:
    """(status, reason) from what the scenario expected and what the driver could read. A fact the
    driver could not read (missing key) makes that check UNVERIFIED, never a pass."""
    unread = []
    for key, want in expect.items():
        if key in ("ends_as", "closes_on", "outcome") or want is None:
            continue
        if key == "finding_classified":
            have = facts.get("findings")
            if have is None:
                unread.append(key)
            elif want not in have:
                return "FAIL", f"{want} was never classified (saw {sorted(have)[:6]})"
        elif key == "finding_absent":
            have = facts.get("findings")
            if have is None:
                unread.append(key)
            elif want in have:
                return "FAIL", f"{want} came back"
        elif key == "equal_to_control":
            if "equal_to_control" not in facts:
                unread.append(key)
            elif not facts["equal_to_control"]:
                return "FAIL", "counts or verdict differ from the control twin"
        elif key == "no_item_twice":
            if "duplicates" not in facts:
                unread.append(key)
            elif facts["duplicates"]:
                return "FAIL", f"{facts['duplicates']} item(s) landed twice"
        elif key == "benchmark":
            have = (facts.get("benchmarks") or {}).get(want)
            if have is None:
                unread.append(key)
            else:
                ok_set = {"PASS"} if expect.get("outcome") == "PASS" else {"FAIL", "UNVERIFIED"}
                if str(have).upper() not in ok_set:
                    return "FAIL", f"benchmark {want} read {have}, wanted {sorted(ok_set)}"
        elif key == "incident":
            seen = (facts.get("incidents") or {}).get(want)
            for phase in ("opens", "closes"):
                if expect.get(phase):
                    if seen is None:
                        unread.append(f"incident {want}")
                    elif phase not in seen:
                        return "FAIL", f"incident {want} never {phase[:-1]}ed"
        elif key in ("opens", "closes"):
            continue
        else:
            unread.append(key)
    if unread:
        return "UNVERIFIED", "could not read: " + ", ".join(sorted(set(unread)))
    return "PASS", ""


def run_scenario(sc: S.Scenario, driver: Driver, name: str, allow: set, wait: int = 4 * 3600) -> dict:
    row = {"id": sc.id, "group": sc.group, "how": sc.how, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if sc.needs and not set(sc.needs) <= allow:
        return {**row, "status": "SKIPPED", "reason": f"the operator did not allow {', '.join(sc.needs)}"}
    if sc.how == "unseedable":
        return {**row, "status": "COVERED" if sc.unit_test else "UNVERIFIED",
                "reason": f"cannot be caused live; unit test {sc.unit_test}" if sc.unit_test else "unseedable with no unit test named"}
    if not sc.act and sc.how != "check_only":
        return {**row, "status": "UNVERIFIED", "reason": "the scenario has no `act` yet: define it before its first live run (P3.4)"}
    try:
        user = driver.take_twin()
        driver.prepare(sc.fixtures)
        reply = driver.api(sc.act.get("method", "POST"), sc.act["path"], {**sc.act.get("body", {}), "reason": f"campaign {name} {sc.id}"}) if sc.act else {}
        if sc.inject:
            driver.inject(sc.inject)
        if not driver.wait_idle(wait):
            return {**row, "status": "FAIL", "reason": f"still running after {wait} s", "reply": reply}
        status, why = judge(sc.expect, driver.facts(user))
    except Exception as exc:      # noqa: BLE001 - a broken scenario is a result, not a crash of the campaign
        return {**row, "status": "FAIL", "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return {**row, "status": status, "reason": why, "user": user, "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def select(scenarios: list, group: str | None, only: set | None) -> list:
    return [s for s in scenarios if (not group or s.group == group) and (not only or s.id in only)]


def run(name: str, driver: Driver, group: str | None = None, only: set | None = None, redo: bool = False,
        allow: set | None = None, dry_run: bool = False, out=print) -> dict:
    scenarios = S.all_scenarios()
    done = read_results(name)
    todo = [s for s in select(scenarios, group, only) if redo or done.get(s.id, {}).get("status") not in PASSING | {"SKIPPED"}]
    out(f"{len(todo)} scenario(s) to run of {len(select(scenarios, group, only))} selected")
    if dry_run:
        for s in todo:
            out(f"  {s.id}  [{s.how}]" + (f"  needs {','.join(s.needs)}" if s.needs else "") + ("" if s.act or s.how in ("check_only", "unseedable") else "  (no act yet)"))
        return done
    for s in todo:
        row = run_scenario(s, driver, name, allow or set())
        append_result(name, row)
        done[s.id] = row
        out(f"  {row['status']:<10} {s.id}  {row.get('reason', '')[:90]}")
    with open(os.path.join(results_dir(name), "RESULTS.md"), "w", encoding="utf-8") as fh:
        fh.write(render_md(name, scenarios, done))
    return done


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-id", type=int, required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--group", choices=S.GROUPS)
    ap.add_argument("--only", help="comma-separated scenario ids")
    ap.add_argument("--redo", action="store_true", help="run again what already passed")
    ap.add_argument("--allow", default="", help="operator permissions given: net,reboot")
    ap.add_argument("--dry-run", action="store_true", help="print what would run; touch nothing")
    args = ap.parse_args(argv)
    only = {x.strip() for x in args.only.split(",") if x.strip()} if args.only else None
    if args.dry_run:
        run(args.name, Driver(), args.group, only, args.redo, set(args.allow.split(",")) - {""}, dry_run=True)
        return 0
    print("the live driver is built at P3.4 against the first tenant (LiveDriver); nothing was run", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
