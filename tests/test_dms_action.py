"""The DMS import must run parallel to the migration and never take mail's
place in the engine run."""
import pytest

import json
import os
import time
import webui


def test_dms_action_exists_and_is_parallel():
    spec = webui.ACTIONS["dms_import"]
    assert spec.get("parallel") is True     # exempt from one-heavy-job admission
    assert spec.get("browser") is True      # needs DISPLAY + admin login
    assert spec.get("confirm") == "DMS"


def test_dms_argv_hands_mail_to_google_not_the_engine():
    argv = webui.ACTIONS["dms_import"]["argv"]
    assert "dms_migrate.py" in " ".join(argv)
    assert "--apply" in argv
    # it reads SOURCE_DOMAIN/TARGET_ADMIN/SOURCE_ADMIN from the env, so the
    # argv must not hard-code a tenant
    assert not any("rohitrokaya" in a for a in argv)


def test_parallel_jobs_are_separate_instances():
    a = webui.get_parallel_job(66, "dms_import")
    b = webui.get_job(66)
    assert a is not b                       # must not block the migration Job
    assert webui.get_parallel_job(66, "dms_import") is a   # stable per key


def test_dms_env_defaults_email_to_target_admin(monkeypatch, tmp_path):
    # no creds file -> DWD_EMAIL falls back to the account's target admin,
    # DISPLAY is set for the headless browser
    monkeypatch.setattr(webui, "DWD_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setattr(webui, "_account_env",
                        lambda aid, base=None: {"TARGET_ADMIN": "info@t.example"})
    env = webui._dms_env(66)
    assert env["DWD_EMAIL"] == "info@t.example"
    assert env["DISPLAY"]


def _stored_logins(monkeypatch, tmp_path, source_domain, target_domain):
    f = tmp_path / "dwd.env"
    f.write_text("DWD_EMAIL_SOURCE=info@s.example\nDWD_PASSWORD_SOURCE=src-secret\nDWD_PASSWORD_TARGET=tgt-secret\n")
    monkeypatch.setattr(webui, "DWD_ENV_FILE", str(f))
    monkeypatch.setattr(webui, "_account_env", lambda aid, base=None: {
        "SOURCE_DOMAIN": source_domain, "TARGET_DOMAIN": target_domain, "TARGET_ADMIN": "info@" + target_domain})
    return webui._dms_env(7)


def test_stored_admin_logins_are_used_for_their_own_pair(monkeypatch, tmp_path):
    env = _stored_logins(monkeypatch, tmp_path, "s.example", "t.example")
    assert env["DWD_EMAIL_SOURCE"] == "info@s.example" and env["DWD_PASSWORD"] == "tgt-secret"


def test_stored_admin_logins_never_cross_to_another_pair(monkeypatch, tmp_path):
    """8 Nov 2026: account 2's DMS (gorkhatweed) got account 3's source admin, and account 3's target password
    was typed into gorkhatweed's sign-in. A file for another pair gives this account no login at all."""
    env = _stored_logins(monkeypatch, tmp_path, "gorkhatweed.com", "new.gorkhatweed.com")
    assert "DWD_EMAIL_SOURCE" not in env and "DWD_PASSWORD" not in env and "DWD_PASSWORD_TARGET" not in env
    assert env["DWD_EMAIL"] == "info@new.gorkhatweed.com"          # its own target admin, no password


def test_dms_metrics_refresh_is_readonly_parallel():
    spec = webui.ACTIONS["dms_metrics_refresh"]
    assert spec.get("parallel") is True and spec.get("browser") is True
    assert "--status" in spec["argv"]      # reads, never touches the import
    assert "confirm" not in spec           # read-only, no gate


def test_metrics_payload_handles_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(webui, "DMS_METRICS_FILE", str(tmp_path / "none.json"))
    p = webui._dms_metrics_payload()
    assert p == {"data": None, "ageSeconds": None}


def test_metrics_payload_reads_and_ages(monkeypatch, tmp_path):
    import json as _j, time as _t
    f = tmp_path / "m.json"
    f.write_text(_j.dumps({"status": "In progress",
                           "metrics": {"Emails imported": 20900},
                           "read_at": int(_t.time()) - 30}))
    monkeypatch.setattr(webui, "DMS_METRICS_FILE", str(f))
    p = webui._dms_metrics_payload()
    assert p["data"]["metrics"]["Emails imported"] == 20900
    assert 25 <= p["ageSeconds"] <= 40


def test_read_metrics_parses_labelled_counters():
    import dms_migrate

    class _Page:
        def inner_text(self, _):
            return ("Import data In progress Stop import Discovered tasks "
                    "272,327 Warning 0 Failed 0 Skipped 0 Successful 272,327 "
                    "Users processed 78 Emails imported 20,900 Emails failed 2")

    r = dms_migrate.read_metrics(_Page())
    assert r["status"] == "In progress"
    assert r["metrics"]["Discovered tasks"] == 272327
    assert r["metrics"]["Emails imported"] == 20900


def test_the_button_never_names_a_csv_and_waits_for_approval():
    """It passed the repo-root identities.csv -- the seeder's file, which on the
    live box mapped every user to ANOTHER account's target domain."""
    argv = webui.ACTIONS["dms_import"]["argv"]
    assert not any(a.endswith(".csv") for a in argv)
    assert "--identities" not in argv
    assert "--watch" in argv


def test_the_map_comes_from_this_accounts_ledger(monkeypatch, tmp_path):
    import csv
    import dms_migrate
    from db import MigrationDB, bulk_seed_identities
    db_path = str(tmp_path / "m.db")
    bulk_seed_identities(MigrationDB(db_path), [("a@src.test", "a@tgt2.test")])
    monkeypatch.setenv("MIGRATION_DB", db_path)
    path = dms_migrate.ledger_identities(str(tmp_path / "out"))
    rows = list(csv.reader(open(path)))
    assert rows[1:] == [["a@src.test", "a@tgt2.test"]]


def test_a_map_for_another_domain_is_refused_before_google_sees_it(monkeypatch, tmp_path):
    import dms_migrate
    wrong = tmp_path / "identities.csv"
    wrong.write_text("source_email,target_email\na@src.test,a@target.other.test\n")
    monkeypatch.setenv("TARGET_DOMAIN", "tgt2.test")
    monkeypatch.setattr(dms_migrate, "start",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("reached Google")))
    assert dms_migrate.main(["--apply", "--identities", str(wrong)]) == 2


class _Loc:
    def __init__(self, seen):
        self.first, self._seen = self, seen

    def wait_for(self, **kw):
        if not self._seen:
            raise TimeoutError


class _Console:
    """Just enough of a page for _find_first: which selectors are on it."""
    def __init__(self, *shown):
        self.shown = shown

    def locator(self, sel):
        return _Loc(any(t in sel for t in self.shown))


def test_a_connection_it_just_requested_keeps_the_watch_going():
    """It requested the connection, did Steps 2-3, found Start import disabled
    and exited -- so nobody pressed Start import after the approval came."""
    import dms_migrate
    step, _ = dms_migrate._start_blocked(_Console(dms_migrate.STEP1_PENDING))
    assert step == "step1-pending"
    step, detail = dms_migrate._start_blocked(_Console())
    assert step == "step4-start" and "still disabled" in detail


def test_a_finished_import_is_tallied(monkeypatch):
    import subprocess
    import dms_migrate
    reads = iter(["In progress", "complete"])
    monkeypatch.setattr(dms_migrate, "status", lambda *a: {"status": next(reads)})
    monkeypatch.setattr(dms_migrate.time, "sleep", lambda s: None)
    calls = []
    monkeypatch.setattr(subprocess, "call", lambda argv, **kw: calls.append(argv) or 0)
    import split_mail, mail_completion
    monkeypatch.setattr(mail_completion, "full_mail_check", lambda st: True)
    monkeypatch.setattr(split_mail, "reconcile_dms", lambda *a, **k: {"blocked": []})
    assert dms_migrate.wait_then_tally(60, 10, False) == 0
    assert calls and calls[0][1] == "tally.py"


def test_an_import_still_running_at_the_deadline_is_not_tallied(monkeypatch):
    import subprocess
    import dms_migrate
    now = [1000.0]
    monkeypatch.setattr(dms_migrate.time, "time", lambda: now[0])
    monkeypatch.setattr(dms_migrate.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    monkeypatch.setattr(dms_migrate, "status", lambda *a: {"status": "In progress"})
    monkeypatch.setattr(subprocess, "call", lambda *a, **k: pytest.fail("tallied early"))
    assert dms_migrate.wait_then_tally(30, 10, False) == 1


@pytest.mark.parametrize("started, waited", [(True, True), (False, False)])
def test_it_waits_only_for_an_import_that_started(monkeypatch, started, waited):
    import dms_migrate
    monkeypatch.delenv("TARGET_DOMAIN", raising=False)
    monkeypatch.setattr(dms_migrate, "start", lambda *a, **k: {
        "ok": True, "step": "step4-start", "detail": "", "did": [], "manual": "",
        "started": started})
    seen = []
    monkeypatch.setattr(dms_migrate, "wait_then_tally", lambda *a, **k: seen.append(a) or 0)
    dms_migrate.main(["--apply", "--until-done", "5", "--identities", "none.csv"])
    assert bool(seen) is waited


def test_both_launchers_wait_for_the_finish():
    argv = webui.ACTIONS["dms_import"]["argv"]
    assert "--until-done" in argv
    import api_server
    import inspect
    assert '"--until-done"' in inspect.getsource(api_server._start_dms)


def test_the_link_to_follow_is_the_one_labelled_as_the_request():
    """Live, every link was a c.gle redirect: only the label says which one
    approves (what dms_read_auth_mail.py learned, by hand, on 2026-08-29)."""
    import dms_migrate
    html = ('<a href="https://c.gle/help">Learn more</a> '
            '<a href="https://c.gle/req">View authorization request</a> '
            '<a href="https://c.gle/privacy">Privacy Policy</a>')
    assert dms_migrate._approval_link_in(html) == "https://c.gle/req"


def test_with_no_label_it_falls_back_to_the_last_link():
    import dms_migrate
    assert dms_migrate._approval_link_in("https://c.gle/a x https://c.gle/b y") == "https://c.gle/b"
    assert dms_migrate._approval_link_in("no links here") is None


def test_the_mail_is_found_by_its_subject():
    """The live subject: "Data migration service: Request for authorization"."""
    import dms_migrate
    assert 'subject:"Request for authorization"' in dms_migrate.APPROVAL_QUERY


def test_without_a_source_login_it_leaves_the_click_to_a_person(monkeypatch):
    import dms_migrate
    monkeypatch.delenv("DWD_PASSWORD_SOURCE", raising=False)
    monkeypatch.setattr(dms_migrate, "approval_link",
                        lambda *a: pytest.fail("read the mailbox with no login to use"))
    did = dms_migrate.approve_as_source(10, False)
    assert "waits for the source admin" in did[0]


def test_no_approval_mail_yet_is_said_not_raised(monkeypatch):
    import dms_migrate
    monkeypatch.setenv("DWD_EMAIL_SOURCE", "admin@src.test")
    monkeypatch.setenv("DWD_PASSWORD_SOURCE", "x")
    monkeypatch.setattr(dms_migrate, "approval_link", lambda *a: None)
    assert dms_migrate.approve_as_source(10, False) == ["no approval mail for admin@src.test yet"]


def test_a_pending_request_is_approved_then_looked_at_again(monkeypatch):
    import dms_migrate
    monkeypatch.delenv("TARGET_DOMAIN", raising=False)
    steps = iter(["step1-pending", "step4-start"])
    runs = []
    monkeypatch.setattr(dms_migrate, "start", lambda *a, **k: runs.append(1) or {
        "ok": True, "step": next(steps), "detail": "", "did": [], "manual": ""})
    approved = []
    monkeypatch.setattr(dms_migrate, "approve_as_source",
                        lambda *a: approved.append(1) or ["pressed 'Authorize'"])
    dms_migrate.main(["--apply", "--identities", "none.csv"])
    assert approved == [1] and len(runs) == 2


def test_start_import_still_disabled_means_step1_is_waiting_too(monkeypatch):
    """9 Nov 2026: the console let steps 2-3 proceed before approval; the run stopped at step 4 with
    the connection requested and never approved."""
    import dms_migrate
    monkeypatch.delenv("TARGET_DOMAIN", raising=False)
    steps = iter(["step4-start", "started"])
    runs = []
    monkeypatch.setattr(dms_migrate, "start", lambda *a, **k: runs.append(1) or {
        "ok": True, "step": next(steps), "detail": "", "did": [], "manual": ""})
    approved = []
    monkeypatch.setattr(dms_migrate, "approve_as_source",
                        lambda *a: approved.append(1) or ["pressed 'Authorize'"])
    dms_migrate.main(["--apply", "--identities", "none.csv"])
    assert approved == [1] and len(runs) == 2


def test_an_ab_arm_stops_at_googles_complete_without_a_whole_tenant_tally(monkeypatch):
    import subprocess
    import dms_migrate
    reads = iter(["In progress", "complete"])
    monkeypatch.setattr(dms_migrate, "status", lambda *a: {"status": next(reads)})
    monkeypatch.setattr(dms_migrate.time, "sleep", lambda s: None)
    monkeypatch.setattr(subprocess, "call", lambda *a, **k: pytest.fail("tallied the whole tenant"))
    reconciled = []
    import split_mail, mail_completion
    monkeypatch.setattr(mail_completion, "full_mail_check", lambda st: True)
    monkeypatch.setattr(split_mail, "reconcile_dms",
                        lambda *a, **k: reconciled.append(1) or {"blocked": []})
    assert dms_migrate.wait_then_tally(60, 10, False, tally=False) == 0
    assert reconciled == [1]      # the ledger must learn which messages the import moved


@pytest.mark.parametrize("flag, fresh", [([], False), (["--fresh-import"], True)])
def test_only_a_fresh_import_exits_and_deletes_the_finished_one(monkeypatch, flag, fresh):
    """'Exit import' asks "Exit and delete import?"; nothing confirmed it, so a new user was never
    added. Only a fresh import (new, empty target accounts) may delete; otherwise a delta continues."""
    import dms_migrate
    monkeypatch.delenv("TARGET_DOMAIN", raising=False)
    seen = {}
    monkeypatch.setattr(dms_migrate, "start", lambda *a, **k: seen.update(k) or {
        "ok": True, "step": "step4-start", "detail": "", "did": [], "manual": "", "started": False})
    dms_migrate.main(["--apply", "--identities", "none.csv", *flag])
    assert seen["fresh"] is fresh
    import inspect
    src = inspect.getsource(dms_migrate)      # start itself is replaced above
    assert "Exit and delete import" in src and "if fresh and not dry_run" in src


@pytest.mark.parametrize("text, want", [
    ("Step 4: Import data After you start importing data, Data Import appears Start import "
     "To start data import, ensure you have completed all the previous steps.", "Not started"),
    ("Step 4: Import data Complete Complete Your import is complete. Run delta import Exit import "
     "Emails imported 485", "complete"),
])
def test_a_console_with_no_import_never_reads_as_complete(text, want):
    """Live: "ensure you have completed all the previous steps" read as "complete", so the A/B called
    a mail import done that had never started."""
    import dms_migrate

    class _Page:
        def inner_text(self, _):
            return text

    assert dms_migrate.read_metrics(_Page())["status"] == want


def test_asked_to_see_it_through_an_import_that_never_started_is_not_success(monkeypatch):
    import dms_migrate
    monkeypatch.delenv("TARGET_DOMAIN", raising=False)
    monkeypatch.setattr(dms_migrate, "start", lambda *a, **k: {
        "ok": True, "step": "step4-start", "detail": "'Start import' still disabled", "did": [],
        "manual": "", "started": False})
    monkeypatch.setattr(dms_migrate, "wait_then_tally", lambda *a, **k: pytest.fail("nothing to wait for"))
    assert dms_migrate.main(["--apply", "--until-done", "5", "--identities", "none.csv"]) == 3
    assert dms_migrate.main(["--identities", "none.csv"]) == 0        # a dry run still reports ok


def test_a_second_apply_run_waits_for_the_first_to_let_go_of_the_console(monkeypatch, tmp_path):
    import fcntl
    import dms_migrate
    lock = tmp_path / "dms.lock"
    monkeypatch.setenv("DMS_LOCK_FILE", str(lock))
    monkeypatch.delenv("TARGET_DOMAIN", raising=False)
    monkeypatch.setattr(dms_migrate, "start", lambda *a, **k: {
        "ok": True, "step": "step4-start", "detail": "", "did": [], "manual": "", "started": False})
    held = open(lock, "w")
    fcntl.flock(held, fcntl.LOCK_EX)
    waited = []
    real = fcntl.flock

    def flock(f, op):
        if op & fcntl.LOCK_NB:
            raise BlockingIOError
        waited.append(op)
        return real(f, op & ~fcntl.LOCK_NB) if False else None
    monkeypatch.setattr(fcntl, "flock", flock)
    dms_migrate.main(["--apply", "--identities", "none.csv"])
    assert waited == [fcntl.LOCK_EX]


def test_each_account_has_its_own_dms_counters(monkeypatch, tmp_path):
    """One box-wide dms_metrics.json meant a refresh for account 2 overwrote what account 3's page showed
    (8 Nov 2026). The scraper writes under logs/dms/<account>/ and the API reads that account's file."""
    import dms_migrate
    monkeypatch.delenv("DMS_METRICS_FILE", raising=False)
    monkeypatch.setattr(dms_migrate, "HERE", str(tmp_path))
    monkeypatch.setattr(webui, "DMS_METRICS_FILE", str(tmp_path / "legacy.json"))
    for aid, imported in ((2, 5), (3, 777)):
        path = dms_migrate.metrics_path(aid)
        assert path.endswith(f"logs/dms/{aid}/metrics.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"status": "In progress", "metrics": {"Emails imported": imported}, "read_at": int(time.time())}, fh)
    assert webui._dms_metrics_payload(3)["data"]["metrics"]["Emails imported"] == 777
    assert webui._dms_metrics_payload(2)["data"]["metrics"]["Emails imported"] == 5
    assert webui._dms_metrics_payload(9)["data"] is None            # an account that never ran one


def test_the_scraper_finds_its_account_from_its_ledger_path(monkeypatch):
    import dms_migrate
    monkeypatch.delenv("DMS_METRICS_FILE", raising=False)
    monkeypatch.setenv("MIGRATION_DB", "/opt/bitport/data/accounts/3/migration.db")
    assert dms_migrate.metrics_path().endswith("logs/dms/3/metrics.json")



# -- the pilot (a few users, optionally a fresh import) and the content-named map (8 Nov 2026) --------------
def _ledger_with(db, users):
    for u in users:
        db.conn.execute("INSERT OR REPLACE INTO identity_map(source_email, target_email, entity_type, status) "
                        "VALUES (?,?,?,?)", (f"{u}@s.example", f"{u}@t.example", "user", "DONE"))
    db.conn.commit()


def _pilot(db, settings, monkeypatch, body):
    import config
    monkeypatch.setattr(config, "Settings", lambda account_id=None: settings)
    return webui._dms_pilot_argv(3, body, ["py", "dms_migrate.py", "--apply"])


def test_without_users_the_dms_argv_is_unchanged(db, settings, monkeypatch):
    assert _pilot(db, settings, monkeypatch, {}) == (["py", "dms_migrate.py", "--apply"], "")


def test_a_pilot_names_its_users_and_a_fresh_import_needs_them(db, settings, monkeypatch):
    _ledger_with(db, ["ward7", "ward8", "other"])
    argv, err = _pilot(db, settings, monkeypatch, {"users": ["ward7@s.example"], "fresh": True})
    assert not err and argv[-3] == "--identities" and argv[-1] == "--fresh-import"
    rows = open(argv[-2]).read().splitlines()
    assert rows == ["source_email,target_email", "ward7@s.example,ward7@t.example"]      # only that user
    assert "needs the users" in _pilot(db, settings, monkeypatch, {"fresh": True})[1]    # never a whole tenant


def test_a_pilot_takes_only_this_accounts_users_and_only_a_few(db, settings, monkeypatch):
    _ledger_with(db, [f"u{i}" for i in range(8)])
    assert "not users of this account" in _pilot(db, settings, monkeypatch, {"users": ["x@elsewhere.example"]})[1]
    many = [f"u{i}@s.example" for i in range(8)]
    assert "at most" in _pilot(db, settings, monkeypatch, {"users": many})[1]


def test_the_identity_map_is_named_by_its_content(db, settings, monkeypatch, tmp_path):
    """A fixed name let a 1-user import's map stand in for 45 users (account 3, 8 Nov 2026): the same users give
    the same file name, a different set gives a different one."""
    import config
    import dms_migrate
    monkeypatch.setattr(config, "Settings", lambda account_id=None: settings)
    out = str(tmp_path / "maps")
    _ledger_with(db, ["a"])
    one = dms_migrate.ledger_identities(out)
    assert dms_migrate.ledger_identities(out) == one                 # unchanged users: the same map
    _ledger_with(db, ["b", "c"])
    three = dms_migrate.ledger_identities(out)
    assert three != one and os.path.basename(one).startswith("identities-from-ledger-1u-")
    assert os.path.basename(three).startswith("identities-from-ledger-3u-")


def test_a_console_holding_another_map_is_refused_not_reused():
    import dms_migrate
    src = open(dms_migrate.__file__).read()
    assert 'step="stale-map"' in src and "not ours and not fresh" in src


def test_reconcile_only_records_for_the_named_users_and_needs_them(monkeypatch, tmp_path):
    """A pilot's deliveries are recorded per email, not counted (ward7's 118 kept reading "owed", 8 Nov 2026)."""
    import dms_migrate
    seen = {}
    monkeypatch.setattr(dms_migrate, "record_deliveries", lambda st, db, users: seen.setdefault("users", users) and 0)
    assert dms_migrate.main(["--reconcile-only"]) == 2                       # nobody named: refused
    f = tmp_path / "u.csv"
    f.write_text("source_email,target_email\nward7@s.example,ward7@t.example\n")
    assert dms_migrate.main(["--reconcile-only", "--identities", str(f)]) == 0
    assert seen["users"] == ["ward7@s.example"]


def _finish(monkeypatch, db, settings, users):
    import config
    import dms_migrate
    import mail_completion
    calls = []
    settings.mail_completion_check = "counts"            # the routine mode; "full" always records
    monkeypatch.setattr(config, "Settings", lambda *a, **k: settings)
    monkeypatch.setattr(dms_migrate, "DONE_POLL_SEC", 0)
    monkeypatch.setattr(dms_migrate, "status", lambda *a, **k: {"status": "complete"})
    monkeypatch.setattr(dms_migrate, "record_deliveries", lambda st, db_, u: calls.append(("record", u)) or 0)
    monkeypatch.setattr(mail_completion, "compare_mail_counts", lambda *a, **k: calls.append(("count",)) or {"blocked": 0})
    assert dms_migrate.wait_then_tally(1, 10, False, tally=False, users=users) == 0
    return calls


def test_a_split_pilot_ends_by_recording_deliveries_a_pure_dms_one_by_counting(db, settings, monkeypatch):
    """Recording needs rows the ledger owes. A pure-DMS arm has none: it found 0 emails and called a complete
    import "incomplete" (the speed matrix's DMS arm, 8 Nov 2026)."""
    from config import DEFERRED_TO_DMS
    _ledger_with(db, ["ward7", "ward13"])
    db.log_audit("ward7@s.example", "m1", "message", DEFERRED_TO_DMS, "owed")
    assert _finish(monkeypatch, db, settings, ["ward7@s.example"]) == [("record", ["ward7@s.example"])]
    assert _finish(monkeypatch, db, settings, ["ward13@s.example"]) == [("count",)]      # nothing owed: counts
    assert _finish(monkeypatch, db, settings, None) == [("count",)]                      # whole account, as before


def test_a_complete_for_another_imports_users_is_not_accepted(db, settings, monkeypatch):
    """The console said "complete" for a stale 1-user import while 45 users were expected (8 Nov 2026)."""
    import dms_migrate
    import config
    _ledger_with(db, ["a", "b", "c"])
    monkeypatch.setattr(config, "Settings", lambda *a, **k: settings)
    monkeypatch.setattr(dms_migrate, "DONE_POLL_SEC", 0)
    recorded = []
    monkeypatch.setattr(dms_migrate, "record_deliveries", lambda *a: recorded.append(1) or 0)
    monkeypatch.setattr(dms_migrate, "status", lambda *a, **k: {"status": "complete", "metrics": {"Users processed": 1}})
    assert dms_migrate.wait_then_tally(1, 10, False, tally=False, users=None) == 2
    monkeypatch.setattr(dms_migrate, "status", lambda *a, **k: {"status": "complete", "metrics": {"Users processed": 3}})
    monkeypatch.setattr("mail_completion.compare_mail_counts", lambda *a, **k: {"blocked": 0})
    assert dms_migrate.wait_then_tally(1, 10, False, tally=False, users=None) == 0


def test_the_speed_matrix_takes_exactly_one_user_of_this_account(db, settings, monkeypatch):
    import config
    _ledger_with(db, ["ward13", "ward7"])
    monkeypatch.setattr(config, "Settings", lambda account_id=None: settings)
    base = ["py", "ab_transfer.py"]
    argv, err = webui._ab_matrix_argv(3, {"users": ["ward13@s.example"]}, base)
    assert not err and argv == base + ["--user", "ward13@s.example"]
    assert "exactly one" in webui._ab_matrix_argv(3, {}, base)[1]
    assert "exactly one" in webui._ab_matrix_argv(3, {"users": ["ward13@s.example", "ward7@s.example"]}, base)[1]
    assert "not a user of this account" in webui._ab_matrix_argv(3, {"users": ["x@elsewhere.example"]}, base)[1]
    spec = webui.ACTIONS["ab_matrix"]
    assert spec["argv"].count("--mail-mode") == 3 and "--fresh-targets" in spec["argv"] and spec["browser"]


def test_a_queued_speed_matrix_gets_its_console_logins_back(monkeypatch):
    seen = {}

    class J:
        def start(self, label, argv, env=None, **k):
            seen["env"] = env
            return True, ""
    monkeypatch.setattr(webui, "get_job", lambda _a: J())
    monkeypatch.setattr(webui, "_dms_env", lambda _a: {"DWD_PASSWORD": "from-dwd-env"})
    webui._queue_starter(3, "ab", {"argv": ["python", "ab_transfer.py", "--user", "u@s"], "env": {}})
    assert seen["env"]["DWD_PASSWORD"] == "from-dwd-env"


def _run_handler(monkeypatch, body, db, settings):
    """webui.Handler._post_run, with the launch captured: what really gets started."""
    import config
    sent, launched = [], {}
    monkeypatch.setattr(config, "Settings", lambda account_id=None: settings)
    monkeypatch.setattr(webui, "resolve_target_account", lambda caller, asked: (3, ""))
    monkeypatch.setattr(webui, "_subscription_ok", lambda a: True)
    monkeypatch.setattr(webui, "_dms_env", lambda a: {"X": "1"})
    monkeypatch.setattr(webui, "_account_email", lambda a: "op@x")

    def fake(account_id, label, argv, env=None, **k):
        launched.update(label=label, argv=argv)
        return "started", "ok"
    monkeypatch.setattr(webui, "launch_or_queue", fake)
    h = object.__new__(webui.Handler)
    h._account_id = lambda: 3
    h._json = lambda obj, code=200: sent.append((code, obj))
    h._post_run(body)
    return sent, launched


def test_the_speed_matrix_really_launches_with_its_user_and_needs_the_typed_words(db, settings, monkeypatch):
    """The first live press ran without --user: the queued path rebuilt the argv and dropped it (8 Nov 2026)."""
    _ledger_with(db, ["ward13"])
    body = {"action": "ab_matrix", "users": ["ward13@s.example"], "confirm": "NEW ACCOUNTS"}
    sent, launched = _run_handler(monkeypatch, body, db, settings)
    assert sent[-1][0] == 200 and launched["argv"][-2:] == ["--user", "ward13@s.example"]
    assert launched["argv"].count("--mail-mode") == 3
    sent, launched = _run_handler(monkeypatch, {**body, "confirm": ""}, db, settings)
    assert sent[-1][0] == 400 and not launched
    sent, launched = _run_handler(monkeypatch, {"action": "ab_matrix", "confirm": "NEW ACCOUNTS"}, db, settings)
    assert sent[-1][0] == 400 and "exactly one" in sent[-1][1]["error"] and not launched
