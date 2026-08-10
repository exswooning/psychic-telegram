"""
tests/test_control_plane.py
===========================
The Migration Command Center's safety properties.

These are not "does the endpoint return 200" tests. Every one of them pins a
property that, if it broke, would let an operator do damage without a trace:

  - a write cannot happen without a Reason Code
  - a viewer cannot trigger a write
  - a REFUSED attempt is still recorded
  - a crashing action still leaves history
  - reads never open a writable handle to the live ledger

The last one matters most: a read/write connection from the dashboard could
block the engine's writer mid-migration.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

import control_plane_db as cpdb
from db import MigrationDB

fastapi = pytest.importorskip("fastapi", reason="control plane deps not installed")
pytest.importorskip("httpx2", reason="starlette TestClient needs httpx2")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def cp(monkeypatch):
    """A control plane over a throwaway ledger, with a known operator table."""
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    monkeypatch.setenv("CP_OPERATORS", "boss:admin,intern:viewer")
    MigrationDB(path)          # base engine schema
    cpdb.apply_migrations()    # control-plane tables

    import api_server
    with TestClient(api_server.app) as client:
        yield client
    try:
        os.unlink(path)
    except OSError:
        pass


ADMIN = {"X-Operator": "boss"}
VIEWER = {"X-Operator": "intern"}


class TestReasonCodeIsMandatory:
    def test_a_write_without_a_reason_is_rejected_before_any_logic(self, cp):
        """422 from the schema, not 400 from a handler. A new write endpoint
        inherits `reason` from WriteAction, so it cannot forget to ask."""
        r = cp.post("/api/v2/migrate/start", json={"services": ["drive"]}, headers=ADMIN)
        assert r.status_code == 422

    def test_a_whitespace_reason_is_not_a_reason(self, cp):
        r = cp.post("/api/v2/migrate/start",
                    json={"reason": "  ", "services": ["drive"]}, headers=ADMIN)
        assert r.status_code == 422

    def test_the_db_layer_refuses_a_blank_reason_independently(self):
        """Defence in depth: the API validates, and so does the writer. A
        script bypassing FastAPI still cannot write unattributed history."""
        with pytest.raises(ValueError, match="Reason Code"):
            cpdb.begin_action("boss", "admin", "migrate.start", "")


class TestRBAC:
    def test_a_viewer_cannot_start_a_migration(self, cp):
        r = cp.post("/api/v2/migrate/start",
                    json={"reason": "curiosity", "services": ["drive"]}, headers=VIEWER)
        assert r.status_code == 403
        assert "viewer" in r.json()["detail"]

    def test_an_unknown_operator_defaults_to_viewer_not_admin(self, cp):
        """Fail closed. An unlisted name is the common case (typo, new hire),
        and defaulting it to admin would make the allowlist decorative."""
        assert cp.get("/api/v2/whoami", headers={"X-Operator": "stranger"}).json()["role"] == "viewer"

    def test_a_viewer_can_still_read(self, cp):
        assert cp.get("/api/v2/users", headers=VIEWER).status_code == 200


class TestEveryAttemptIsAudited:
    def test_a_refused_attempt_is_recorded(self, cp):
        """Who *tried* to wipe the tenant is as interesting as who did."""
        cp.post("/api/v2/migrate/start",
                json={"reason": "should not be allowed", "services": ["drive"]},
                headers=VIEWER)
        rows = cp.get("/api/v2/actions").json()
        assert [(r["actor"], r["action"], r["outcome"]) for r in rows] == \
               [("intern", "migrate.start", "REFUSED")]
        assert rows[0]["reason"] == "should not be allowed"

    def test_intent_is_logged_before_the_action_runs(self, monkeypatch, cp):
        """The ordering property. If the action explodes, the row survives --
        a log that only records successes is a scoreboard, not an audit."""
        import api_server

        def _boom(argv):
            raise RuntimeError("subprocess died")
        monkeypatch.setattr(api_server, "_spawn", _boom)

        r = cp.post("/api/v2/migrate/start",
                    json={"reason": "will crash", "services": ["drive"]}, headers=ADMIN)
        assert r.status_code == 500
        rows = cp.get("/api/v2/actions").json()
        assert rows[0]["outcome"] == "FAILED"
        assert rows[0]["reason"] == "will crash"
        assert "subprocess died" in rows[0]["detail"]

    def test_a_successful_action_records_actor_reason_and_outcome(self, monkeypatch, cp):
        import api_server
        monkeypatch.setattr(api_server, "_spawn", lambda argv: (True, "started pid 999"))
        cp.post("/api/v2/migrate/start",
                json={"reason": "planned cutover", "services": ["drive"],
                      "users": ["a@x.com"]}, headers=ADMIN)
        row = cp.get("/api/v2/actions").json()[0]
        assert (row["actor"], row["actor_role"], row["outcome"]) == ("boss", "admin", "OK")
        assert row["target"] == "a@x.com"
        assert "planned cutover" in row["reason"]


class TestProvisioning:
    """
    The UI front end for `provision-users`. Progress is parsed from the same
    log lines the CLI itself prints (`provision.py`'s `log.info("created
    %s", email)`), so the bar can never disagree with what the command line
    would report -- there is no second source of truth to drift.
    """

    def test_a_viewer_cannot_launch_provisioning(self, cp):
        r = cp.post("/api/v2/provision/start",
                    json={"reason": "curiosity", "tenant": "target"}, headers=VIEWER)
        assert r.status_code == 403

    def test_launch_is_audited_with_the_tenant_as_target(self, monkeypatch, cp):
        import api_server

        class _FakeProc:
            pid = 4242
        monkeypatch.setattr(api_server.subprocess, "Popen", lambda *a, **k: _FakeProc())

        r = cp.post("/api/v2/provision/start",
                    json={"reason": "reprovisioning after cleanup",
                          "tenant": "target"}, headers=ADMIN)
        assert r.status_code == 200
        row = cp.get("/api/v2/actions").json()[0]
        assert row["action"] == "provision.start"
        assert row["target"] == "target"
        assert row["outcome"] == "OK"

    def test_status_reports_total_from_identity_map_not_a_guess(self, cp):
        from db import bulk_seed_identities, MigrationDB
        d = MigrationDB(os.environ["MIGRATION_DB"])
        bulk_seed_identities(d, [("a@s.com", "a@t.com"), ("b@s.com", "b@t.com")])
        d.close()
        r = cp.get("/api/v2/provision/status?tenant=target").json()
        assert r["total"] == 2
        assert r["running"] is False

    def test_progress_is_parsed_from_the_exact_provision_log_format(self, cp, tmp_path):
        """Pins the regex against provision.py's actual wording -- a rename
        of that log line would otherwise silently freeze the progress bar
        at 0 with no test catching it."""
        import api_server

        os.makedirs(os.path.join(api_server.HERE, "logs"), exist_ok=True)
        log = os.path.join(api_server.HERE, "logs", "provision-target.log")
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("2026-01-01 00:00:00 INFO provision: created a@t.com\n")
            fh.write("2026-01-01 00:00:01 INFO provision: created b@t.com\n")
            fh.write("2026-01-01 00:00:02 WARNING provision: could not create c@t.com: 409\n")
        try:
            r = cp.get("/api/v2/provision/status?tenant=target").json()
            assert r["created"] == 2
            assert r["failed"] == 1
        finally:
            os.remove(log)


class TestEmergencyBrake:
    def test_the_kill_switch_needs_a_typed_confirmation_too(self, cp):
        """Reason Code alone is not enough for the one action whose blast
        radius is every file in a tenant."""
        r = cp.post("/api/v2/emergency/revert-public",
                    json={"reason": "incident", "tenant": "target", "confirm": "ok"},
                    headers=ADMIN)
        assert r.status_code == 400
        assert "REVERT" in r.json()["detail"]

    def test_a_missing_revert_script_fails_loudly_rather_than_silently(self, monkeypatch, cp):
        """Reporting success while leaving files public would be the worst
        possible outcome for this particular button."""
        import api_server
        monkeypatch.setattr(os.path, "isfile", lambda p: False)
        r = cp.post("/api/v2/emergency/revert-public",
                    json={"reason": "incident", "tenant": "target", "confirm": "REVERT"},
                    headers=ADMIN)
        assert r.json()["ok"] is False
        assert "cannot revert" in r.json()["detail"]


class TestLedgerSafety:
    def test_reads_use_a_read_only_connection(self):
        """A writable handle from the dashboard could block the engine's
        writer mid-copy. WAL only gives lock-free concurrency to readers."""
        path = tempfile.mktemp(suffix=".db")
        os.environ["MIGRATION_DB"] = path
        MigrationDB(path)
        cpdb.apply_migrations()
        with cpdb.ro() as conn:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("CREATE TABLE nope (x INT)")
        os.unlink(path)

    def test_migrations_are_idempotent(self):
        """They run on every api_server start, including against a database
        with a migration in flight -- which is how they were first applied."""
        path = tempfile.mktemp(suffix=".db")
        os.environ["MIGRATION_DB"] = path
        MigrationDB(path)
        assert cpdb.apply_migrations() == ["001_control_plane.sql"]
        assert cpdb.apply_migrations() == ["001_control_plane.sql"]   # no error
        os.unlink(path)

    def test_migrations_do_not_touch_engine_tables(self):
        """The DDL must be additive. Anything else risks the live ledger."""
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "migrations",
                "001_control_plane.sql"), encoding="utf-8") as fh:
            sql = fh.read().upper()
        for forbidden in ("DROP ", "ALTER TABLE ID_MAPPING", "ALTER TABLE AUDIT_LOG",
                          "DELETE FROM", "ALTER TABLE IDENTITY_MAP"):
            assert forbidden not in sql, f"migration contains {forbidden!r}"


class TestPartialFailureModelling:
    def test_progress_is_per_user_and_never_averaged_into_one_number(self):
        """The real state is 7 DONE / 1 RUNNING / 2 FAILED at once. A single
        batch percentage hides which users need attention."""
        path = tempfile.mktemp(suffix=".db")
        os.environ["MIGRATION_DB"] = path
        d = MigrationDB(path)
        cpdb.apply_migrations()
        from db import bulk_seed_identities
        bulk_seed_identities(d, [("a@s.com", "a@t.com"), ("b@s.com", "b@t.com")])
        with d.write() as conn:
            conn.execute("INSERT INTO audit_log (source_user,item_id,item_type,status)"
                         " VALUES ('a@s.com','f1','file','SUCCESS')")
            conn.execute("INSERT INTO audit_log (source_user,item_id,item_type,status)"
                         " VALUES ('a@s.com','f2','file','FAILED')")
        rows = {r["source_email"]: r for r in cpdb.user_progress()}
        assert rows["a@s.com"]["itemsDone"] == 1
        assert rows["a@s.com"]["itemsFailed"] == 1
        assert rows["a@s.com"]["percent"] == 50.0
        # A user with no activity is 0%, not NaN and not 100%.
        assert rows["b@s.com"]["percent"] == 0.0
        d.close()
        os.unlink(path)

    def test_a_failed_row_with_a_mapping_is_flagged_as_superseded(self):
        """Stops the UI inviting a retry of work a later pass already fixed."""
        path = tempfile.mktemp(suffix=".db")
        os.environ["MIGRATION_DB"] = path
        d = MigrationDB(path)
        cpdb.apply_migrations()
        with d.write() as conn:
            conn.execute("INSERT INTO audit_log (source_user,item_id,item_type,status,"
                         "error_message) VALUES ('a@s.com','f1','file','FAILED','500')")
        d.record_mapping("a@s.com", "f1", "tgt-1", "file")
        assert cpdb.forensic_detail("a@s.com", "f1")["supersededBySuccess"] is True
        d.close()
        os.unlink(path)


class TestFleetLiveness:
    def test_a_silent_node_goes_unhealthy_on_read(self):
        """A crashed node cannot mark itself down -- that is the failure mode.
        So liveness is derived from last_seen at read time, not stored."""
        path = tempfile.mktemp(suffix=".db")
        os.environ["MIGRATION_DB"] = path
        MigrationDB(path)
        cpdb.apply_migrations()
        cpdb.upsert_node("vps-1", hostname="mig1")
        assert cpdb.fleet(stale_after_s=90)[0]["healthy"] is True
        assert cpdb.fleet(stale_after_s=0)[0]["healthy"] is False
        os.unlink(path)


class TestBenchmarkLiveStatus:
    """
    A benchmark takes hours. Before this, the UI showed one chip reading
    "run in flight · pid 12345", which looks identical to a hung process for
    the entire run -- so the two things an operator actually wants (which
    stage, and is the file count still climbing) were only obtainable by
    SSHing in.

    Phase comes from the process table rather than from parsing the run's
    stdout, because stdout goes wherever the launcher redirected it and the
    server has no way to know where that was.
    """

    def test_etime_parses_every_ps_format(self):
        """ps switches format as a run ages: MM:SS, then HH:MM:SS, then
        DD-HH:MM:SS. A benchmark crosses all three boundaries, so getting
        this wrong makes the elapsed time (and the files/sec derived from
        it) silently wrong hours in."""
        from api_server import _etime_seconds
        assert _etime_seconds("05:09") == 309
        assert _etime_seconds("01:05:09") == 3909
        assert _etime_seconds("2-01:05:09") == 2 * 86400 + 3909
        assert _etime_seconds("  05:09 ") == 309

    def test_not_running_reports_nothing_else(self, cp):
        """No phase, no progress, no stale numbers left on screen."""
        r = cp.get("/api/v2/benchmark/running")
        assert r.status_code == 200
        body = r.json()
        if not body["running"]:
            assert "progress" not in body

    def test_phase_order_matches_what_the_benchmark_actually_runs(self):
        """The chips render in this order, so it has to be the real one:
        wipe, reset ledger, migrate, audit. A reordering here would show an
        operator a completed stage as pending."""
        from api_server import _BENCH_PHASES
        assert [p[0] for p in _BENCH_PHASES] == ["wipe", "ledger", "migrate", "audit"]
        # Each phase is identified by the script it shells out to; if
        # benchmark_run.py stops calling one, the phase silently never fires.
        import benchmark_run  # noqa: F401
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "benchmark_run.py")).read()
        for _, needle, _ in _BENCH_PHASES:
            assert needle in src, f"{needle} no longer invoked by benchmark_run.py"

    def test_progress_counts_what_exists_not_what_was_attempted(self):
        """`files` is the number of items with a live target id.

        id_mapping is the right source because a row there means the object
        exists on the target and its id is known. audit_log is keyed UNIQUE
        on (source_user, item_id, item_type), so it holds one row per item
        carrying its latest status -- a file that failed and was later fixed
        reads SUCCESS there with no trace of the failure, which is fine for
        an audit trail and wrong for "how much is on the target right now".
        """
        path = tempfile.mktemp(suffix=".db")
        os.environ["MIGRATION_DB"] = path
        d = MigrationDB(path)
        cpdb.apply_migrations()
        d.record_mapping("a@s.com", "f1", "tgt-1", "file")
        d.record_mapping("a@s.com", "d1", "tgt-d1", "folder")
        with d.write() as conn:
            # Two other items that never landed, plus a lost ACL grant.
            for item in ("f2", "f3"):
                conn.execute("INSERT INTO audit_log (source_user,item_id,item_type,"
                             "status) VALUES ('a@s.com',?,'file','FAILED')", (item,))
            conn.execute("INSERT INTO audit_log (source_user,item_id,item_type,"
                         "status) VALUES ('a@s.com','f1:bob','acl','FAILED')")
        got = cpdb.drive_migrated_counts()
        assert got["files"] == 1 and got["folders"] == 1
        assert got["failed"] == 2 and got["aclFailed"] == 1
        d.close()
        os.unlink(path)

    def test_failures_are_scoped_to_the_run_not_all_time(self):
        """reset_drive_ledger.py clears Drive mappings but not audit_log, so
        ACL rows outlive a wipe. Unscoped, the first live run of this
        reported aclFailed: 20714 -- every one from the previous day's run,
        none from the run being watched. A permanent five-figure failure
        count beside a healthy run teaches the operator to ignore the exact
        field that exists to catch the next silent ACL collapse.
        """
        path = tempfile.mktemp(suffix=".db")
        os.environ["MIGRATION_DB"] = path
        d = MigrationDB(path)
        cpdb.apply_migrations()
        with d.write() as conn:
            conn.execute("INSERT INTO audit_log (source_user,item_id,item_type,"
                         "status,timestamp) VALUES ('a@s.com','old','acl','FAILED',"
                         "'2020-01-01T00:00:00Z')")
            conn.execute("INSERT INTO audit_log (source_user,item_id,item_type,"
                         "status,timestamp) VALUES ('a@s.com','new','acl','FAILED',"
                         "'2099-01-01T00:00:00Z')")
        assert cpdb.drive_migrated_counts()["aclFailed"] == 2
        scoped = cpdb.drive_migrated_counts(since_iso="2098-01-01T00:00:00Z")
        assert scoped["aclFailed"] == 1, "a previous run's failures leaked in"
        assert scoped["scopedSince"] == "2098-01-01T00:00:00Z"
        d.close()
        os.unlink(path)


class TestCoverageAudit:
    """
    Coverage audits make one real API call per user per service and can run
    for minutes, so they launch detached like a benchmark rather than
    blocking a request. The status endpoint has to tell "no audit has run
    yet" apart from "one is running and has not written anything yet" apart
    from "one finished and here is the result" -- three different UI states
    from the same two files (a process table entry and a JSON log).
    """

    def test_a_viewer_cannot_launch_it(self, cp):
        r = cp.post("/api/v2/coverage/start",
                    json={"reason": "curiosity"}, headers=VIEWER)
        assert r.status_code == 403

    def test_no_audit_yet_is_a_clean_not_running_result_none(self, cp, monkeypatch):
        import api_server
        monkeypatch.setattr(api_server, "HERE", tempfile.mkdtemp())
        r = cp.get("/api/v2/coverage/status")
        assert r.status_code == 200
        body = r.json()
        assert body["running"] is False
        assert body["result"] is None

    def test_a_completed_result_is_parsed_and_summarised(self, cp, monkeypatch):
        import json as _json

        import api_server
        d = tempfile.mkdtemp()
        monkeypatch.setattr(api_server, "HERE", d)
        os.makedirs(os.path.join(d, "logs"), exist_ok=True)
        payload = {
            "rows": [
                {"service": "drive", "item": "Folders", "status": "FULL",
                 "verdict": "COVERED", "count": 10, "note": ""},
                {"service": "drive", "item": "Apps Script", "status": "PARTIAL",
                 "verdict": "ABSENT", "count": 0, "note": ""},
                {"service": "other", "item": "Contacts", "status": "PARTIAL",
                 "verdict": "UNPROBED", "count": None, "note": ""},
            ],
            "totals": {"errors": {}, "external_shared_with_me": 0,
                      "migrate_external_shares": True},
        }
        with open(os.path.join(d, "logs", "coverage-20990101T000000Z.json"),
                 "w", encoding="utf-8") as fh:
            _json.dump(payload, fh)
        r = cp.get("/api/v2/coverage/status")
        body = r.json()
        assert body["running"] is False
        assert body["result"]["counts"] == {"covered": 1, "absent": 1, "unprobed": 1}

    def test_a_run_still_being_written_does_not_crash_the_endpoint(self, cp, monkeypatch):
        """The file exists the instant the subprocess opens it for writing,
        long before valid JSON is in it. Partial/invalid JSON must read as
        'no result yet', not as a 500."""
        import api_server
        d = tempfile.mkdtemp()
        monkeypatch.setattr(api_server, "HERE", d)
        os.makedirs(os.path.join(d, "logs"), exist_ok=True)
        with open(os.path.join(d, "logs", "coverage-20990101T000000Z.json"),
                 "w", encoding="utf-8") as fh:
            fh.write('{"rows": [')   # truncated
        r = cp.get("/api/v2/coverage/status")
        assert r.status_code == 200
        assert r.json()["result"] is None


class TestDwdStatus:
    """
    Functional, not documentary: Google exposes no API to read a DWD
    delegation entry, so this mints a token per required scope and reports
    which ones succeed. A key that does not exist, or an admin that is not
    configured, must be reported -- not raise past the caller into a 500.
    """

    def test_an_unknown_tenant_is_rejected(self, cp):
        r = cp.get("/api/v2/dwd/status?tenant=sideways")
        assert r.status_code == 400

    def test_a_missing_key_is_reported_not_a_500(self, cp, monkeypatch, settings):
        """The handler does `from config import Settings` inside its own
        closure so it always sees the live config, not one captured at
        import time -- which means the patch target is config.Settings
        itself, not api_server's or verify_scopes' module namespace."""
        import config

        settings.source_sa_key = "/definitely/does/not/exist.json"
        settings.source_admin = "admin@source.example"
        monkeypatch.setattr(config, "Settings", lambda: settings)
        r = cp.get("/api/v2/dwd/status?tenant=source")
        assert r.status_code == 200
        body = r.json()
        assert body["checked"] is False
        assert "key" in body["error"]


class TestApiServerLoadsEnvSh:
    """
    Every write this server launches is a subprocess started with
    `dict(os.environ)` -- THIS process's own environment. The control plane
    was started via start_control_plane.sh (which never sourced env.sh)
    for this entire project, and the very first live check of the new
    /api/v2/dwd/status endpoint proved it: SOURCE_ADMIN/TARGET_ADMIN came
    back "not set" despite being correctly configured in env.sh the whole
    time. Every subprocess this server ever launched -- migrate, benchmark,
    provision, coverage -- would have failed the same way had it been
    triggered through the API instead of by SSHing in and sourcing env.sh
    by hand, which is how every one of them was actually tested.

    webui.py has always self-loaded env.sh in its own main() for exactly
    this reason; this pins api_server.py to the same behaviour.
    """

    def test_main_loads_env_sh_into_the_process_environment(self, tmp_path, monkeypatch):
        import api_server

        env_path = tmp_path / "env.sh"
        env_path.write_text("export SOURCE_ADMIN=admin@example.com\n"
                            "export SOURCE_DOMAIN=example.com\n")
        monkeypatch.setattr(api_server, "HERE", str(tmp_path))
        monkeypatch.delenv("SOURCE_ADMIN", raising=False)
        monkeypatch.delenv("SOURCE_DOMAIN", raising=False)

        ran = {}
        monkeypatch.setattr(
            "uvicorn.run", lambda *a, **kw: ran.setdefault("called", True))

        api_server.main(["--port", "0"])

        assert ran.get("called") is True
        assert os.environ.get("SOURCE_ADMIN") == "admin@example.com"
        assert os.environ.get("SOURCE_DOMAIN") == "example.com"

    def test_a_missing_env_sh_does_not_crash_startup(self, tmp_path, monkeypatch):
        """No env.sh yet (a fresh checkout, before setup.sh) must still let
        the API start, just without config -- not raise past main()."""
        import api_server

        monkeypatch.setattr(api_server, "HERE", str(tmp_path))
        monkeypatch.setattr(
            "uvicorn.run", lambda *a, **kw: None)
        assert api_server.main(["--port", "0"]) == 0
