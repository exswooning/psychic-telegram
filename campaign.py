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


class Unautomated(Exception):
    """The driver cannot cause this yet: the scenario is UNVERIFIED, never a pass or a crash."""


class Driver:
    """What a scenario needs from the live system. LiveDriver is the box; tests use a fake."""
    def take_twin(self) -> str: raise NotImplementedError
    def fixture_user(self, name: str) -> str: return name          # a cohort user (c03) as an address
    def source_has(self, kind: str, user: str): return None        # a source-side probe; None = there is none
    def prepare(self, fixtures: tuple) -> None: raise NotImplementedError
    def api(self, method: str, path: str, body: dict | None) -> dict: raise NotImplementedError
    def inject(self, inject: dict) -> None: raise NotImplementedError
    def wait_idle(self, timeout: int) -> bool: raise NotImplementedError
    def facts(self, user: str | None) -> dict: raise NotImplementedError   # what judges read; see judge()


TWINS = tuple(f"t{n:02d}" for n in range(2, 21))      # t01 is the control
CONTROL = "t01"


class LiveDriver(Driver):
    """The box: requests go to the API as the superadmin, with a session made here (root needs no password);
    facts come from the account's own ledger, its reports and the incident table."""

    def __init__(self, account_id: int, name: str, api: str = "http://127.0.0.1:8090", poll: int = 30):
        import accounts_auth
        import control_plane_db as cpdb
        self.account_id, self.name, self.api_base, self.poll = account_id, name, api, poll
        with cpdb.ro() as conn:
            admin = conn.execute("SELECT id FROM accounts WHERE is_superadmin=1 ORDER BY id LIMIT 1").fetchone()
        self.token = accounts_auth.create_session(admin[0])
        self.domain = (accounts_auth.get_tenant_config(account_id, "source") or {}).get("domain", "")
        self.started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _ledger(self):
        import sqlite3
        from config import Settings
        conn = sqlite3.connect(Settings(account_id=self.account_id).db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def fixture_user(self, name: str) -> str:
        return f"{name}@{self.domain}"

    def source_has(self, kind: str, user: str):
        import campaign_evidence
        import webui
        os.environ.update(webui._dms_env(self.account_id))
        from auth import AuthManager
        from config import Settings
        st = Settings(account_id=self.account_id)
        return campaign_evidence.source_has(kind, user, AuthManager(st), st)

    def take_twin(self) -> str:
        path = os.path.join(results_dir(self.name), "twins.json")
        try:
            with open(path, encoding="utf-8") as fh:
                used = json.load(fh)
        except (OSError, ValueError):
            used = []
        free = [t for t in TWINS if t not in used]
        if not free:
            raise Unautomated("every twin is used: wipe-and-migrate the cohort before the next scenario")
        os.makedirs(results_dir(self.name), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(used + [free[0]], fh)
        self.started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return f"{free[0]}@{self.domain}"

    def prepare(self, fixtures: tuple) -> None:
        return None          # the cohort is seeded once (P3.1); a fixture names the cohort user holding it

    def api(self, method: str, path: str, body: dict | None) -> dict:
        import urllib.request
        req = urllib.request.Request(self.api_base + path, method=method, headers={
            "Content-Type": "application/json", "Cookie": f"bp_session={self.token}"},
            data=json.dumps({**(body or {}), "account_id": self.account_id}).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read() or b"{}")

    def inject(self, inject: dict) -> None:
        import failpoints
        fault = (inject.get("env") or {}).get("BITPORT_FAULT")
        if fault:
            os.makedirs(os.path.dirname(failpoints.FAULT_FILE), exist_ok=True)
            with open(failpoints.FAULT_FILE, "w", encoding="utf-8") as fh:
                fh.write(fault + "\n")
            return
        raise Unautomated(f"inject {json.dumps(inject)[:120]}")

    def disarm(self) -> None:
        import failpoints
        try:
            os.remove(failpoints.FAULT_FILE)
        except FileNotFoundError:
            pass

    def wait_idle(self, timeout: int) -> bool:
        """No job of this account for two polls in a row: a follow-on starts a moment after its run ends."""
        import job_admission
        deadline, quiet = time.time() + timeout, 0
        while time.time() < deadline:
            busy = any(j.get("account_id") == self.account_id for j in job_admission.list_active())
            quiet = 0 if busy else quiet + 1
            if quiet >= 2:
                return True
            time.sleep(self.poll)
        return False

    def facts(self, user: str | None, since: str | None = None) -> dict:
        import known_issues
        import run_watch
        out: dict = {}
        with self._ledger() as c:
            if user:
                found = {r[0] for r in c.execute(
                    "SELECT issue FROM verification_repair_queue WHERE source_user=? AND issue IS NOT NULL", (user,))}
                for r in c.execute("SELECT item_type, error_message FROM audit_log WHERE source_user=? AND timestamp>=? "
                                   "AND status LIKE 'FAILED%'", (user, self.started if since is None else since)):
                    found.add(known_issues.classify_failure(r[0], r[1] or ""))
                out["findings"] = found
                out["audit_counts"] = {(r[0], r[1]): r[2] for r in c.execute(
                    "SELECT item_type, status, COUNT(*) FROM audit_log WHERE source_user=? GROUP BY 1,2", (user,))}
                out["verdicts"] = {r[0]: r[1] for r in c.execute(
                    "SELECT service, verdict FROM user_verification WHERE source_user=?", (user,))}
                tallies = {r[0]: json.loads(r[1]) for r in c.execute(
                    "SELECT source_user, payload FROM user_tally WHERE source_user IN (?,?)",
                    (user, f"{CONTROL}@{self.domain}"))}
                mine, ctl = tallies.get(user), tallies.get(f"{CONTROL}@{self.domain}")
                if mine and ctl:
                    svc = lambda p: {k: v.get("target") for k, v in (p.get("services") or {}).items()}  # noqa: E731
                    out["equal_to_control"] = svc(mine) == svc(ctl)
                    out["duplicates"] = sum(max(0, (v.get("target") or 0) - (v.get("expected") or v.get("source") or 0))
                                            for v in (mine.get("services") or {}).values())
        reports = sorted(f for f in os.listdir(os.path.join(HERE, "logs", "reports", str(self.account_id)))
                         if f.endswith(".json") and f.startswith("migration-"))
        if reports:
            with open(os.path.join(HERE, "logs", "reports", str(self.account_id), reports[-1]), encoding="utf-8") as fh:
                rep = json.load(fh)
            out["benchmarks"] = {b["id"]: b.get("status", "").upper() for b in (rep.get("benchmarks") or {}).get("results", [])}
        incidents: dict = {}
        for inc in run_watch.list_incidents(account_id=self.account_id, limit=500):
            if (inc.get("opened_at") or "") >= self.started:
                incidents.setdefault(inc.get("kind"), set()).add("opens")
                if inc.get("status") == "resolved":
                    incidents[inc.get("kind")].add("closes")
        out["incidents"] = incidents
        return out


def judge(expect: dict, facts: dict) -> tuple:
    """(status, reason) from what the scenario expected and what the driver could read. A fact the
    driver could not read (missing key) makes that check UNVERIFIED, never a pass."""
    unread = []
    for key, want in expect.items():
        if key in ("ends_as", "closes_on", "outcome", "or_evidence", "or_source", "needs_source") or want is None:
            continue
        if key == "finding_classified":
            have = facts.get("findings")
            if have is None:
                unread.append(key)
            elif want not in have:
                ev = expect.get("or_evidence")
                if ev is None and "or_evidence" in expect and not expect.get("or_source"):
                    # a seeded case Bitport may have handled without raising anything: nothing evidences it yet
                    return "UNVERIFIED", f"no {want} finding came and no ledger evidence is defined for it"
                if expect.get("or_source") and facts.get("source_has") is not None:
                    verdicts = facts.get("verdicts") or {}
                    if not facts["source_has"]:
                        return "UNVERIFIED", f"no {want} finding and the source does not hold the case: the fixture was never made"
                    if not verdicts or any(v != "IDENTICAL" for v in verdicts.values()):
                        return "FAIL", f"{want} is on the source but the user does not read IDENTICAL: {verdicts}"
                    continue
                if expect.get("or_source"):
                    return "UNVERIFIED", f"no {want} finding and the source could not be probed for the case"
                if ev:
                    seen = (facts.get("audit_counts") or {}).get(tuple(ev), 0)
                    verdicts = facts.get("verdicts") or {}
                    if not seen:
                        return "UNVERIFIED", f"no {want} finding and the ledger shows no {ev[1]} row: the fixture may not have been met"
                    if any(v != "IDENTICAL" for v in verdicts.values()) or not verdicts:
                        return "FAIL", f"{want} was met ({seen} x {ev[1]}) but the user does not read IDENTICAL: {verdicts}"
                    continue
                return "FAIL", f"{want} was never classified (saw {sorted(have)[:6]})"
        elif key == "finding_absent":
            if expect.get("needs_source") and facts.get("source_has") is not True:
                return "UNVERIFIED", "the case was never made on the source, so its absence proves nothing"
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


READS_THE_RUN = ("check_only", "seeded", "must_not_happen")


def _read(sc, driver, row, user):
    facts = driver.facts(user, since="")
    probe = sc.expect.get("or_source") or sc.expect.get("needs_source")
    if probe and user:
        facts["source_has"] = driver.source_has(probe, user)
    status, why = judge(sc.expect, facts)
    return {**row, "status": status, "reason": why, "user": user, "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def run_scenario(sc: S.Scenario, driver: Driver, name: str, allow: set, wait: int = 4 * 3600) -> dict:
    row = {"id": sc.id, "group": sc.group, "how": sc.how, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if sc.needs and not set(sc.needs) <= allow:
        return {**row, "status": "SKIPPED", "reason": f"the operator did not allow {', '.join(sc.needs)}"}
    if sc.how == "unseedable":
        return {**row, "status": "COVERED" if sc.unit_test else "UNVERIFIED",
                "reason": f"cannot be caused live; unit test {sc.unit_test}" if sc.unit_test else "unseedable with no unit test named"}
    if not sc.act and sc.how not in READS_THE_RUN:
        return {**row, "status": "UNVERIFIED", "reason": "the scenario has no `act` yet: define it before its first live run (P3.4)"}
    try:
        if sc.how in READS_THE_RUN and not sc.act:
            # The fixture user's own history in the control run: nothing is caused, no twin is spent (there are 19).
            user = driver.fixture_user(sc.fixtures[0]) if sc.fixtures else None
            return _read(sc, driver, row, user)
        user = None if sc.how == "check_only" else driver.take_twin()
        driver.prepare(sc.fixtures)
        reply = driver.api(sc.act.get("method", "POST"), sc.act["path"], {**sc.act.get("body", {}), "reason": f"campaign {name} {sc.id}"}) if sc.act else {}
        if sc.inject:
            driver.inject(sc.inject)
        if not driver.wait_idle(wait):
            return {**row, "status": "FAIL", "reason": f"still running after {wait} s", "reply": reply}
        status, why = judge(sc.expect, driver.facts(user))
    except Unautomated as exc:
        return {**row, "status": "UNVERIFIED", "reason": f"not automated yet: {exc}"}
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
        if hasattr(driver, "disarm"):
            driver.disarm()
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
    driver = LiveDriver(args.account_id, args.name)
    try:
        run(args.name, driver, args.group, only, args.redo, set(args.allow.split(",")) - {""})
    finally:
        driver.disarm()
    return 0


if __name__ == "__main__":
    sys.exit(main())
