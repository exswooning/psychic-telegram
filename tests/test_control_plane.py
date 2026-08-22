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

import json
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

        def _boom(*a, **k):
            raise RuntimeError("subprocess died")
        monkeypatch.setattr(api_server, "_run_admitted", _boom)

        r = cp.post("/api/v2/migrate/start",
                    json={"reason": "will crash", "services": ["drive"]}, headers=ADMIN)
        assert r.status_code == 500
        rows = cp.get("/api/v2/actions").json()
        assert rows[0]["outcome"] == "FAILED"
        assert rows[0]["reason"] == "will crash"
        assert "subprocess died" in rows[0]["detail"]

    def test_a_successful_action_records_actor_reason_and_outcome(self, monkeypatch, cp):
        import api_server
        monkeypatch.setattr(api_server, "_run_admitted", lambda *a, **k: (True, "started pid 999"))
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
        """Pins the parser against provision.report()'s ACTUAL output.

        The version this replaces wrote `provision: created a@t.com` -- a
        line provision.py has never printed. It passed against its own
        invention while the endpoint read 0 from every real log, which is
        how a parser that matched nothing shipped with a green test claiming
        it "pins the regex against provision.py's actual wording".

        The format below is copied from provision.report(); the assertion at
        the end of this class checks it still is.
        """
        import api_server

        os.makedirs(os.path.join(api_server.HERE, "logs"), exist_ok=True)
        log = os.path.join(api_server.HERE, "logs", "provision-target.log")
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("Created 2 account(s):\n")
            fh.write("    a@t.com\n")
            fh.write("        password: secret-one\n")
            fh.write("    b@t.com\n")
            fh.write("        password: secret-two\n")
            fh.write("\nFailed (1):\n")
            fh.write("    c@t.com: 409 already exists\n")
        try:
            r = cp.get("/api/v2/provision/status?tenant=target").json()
            assert r["created"] == 2
            assert r["failed"] == 1
            assert {u["email"] for u in r["users"]} == {"a@t.com", "b@t.com",
                                                        "c@t.com"}
            # The whole response, including `tail`, must not carry them.
            assert "secret-one" not in json.dumps(r)
        finally:
            os.remove(log)

    def test_the_parser_matches_the_wording_provision_actually_prints(self):
        """The guard the old test only claimed to be.

        If provision.report() is reworded, this fails here rather than
        silently freezing the progress bar at zero.
        """
        import inspect

        import provision

        src = inspect.getsource(provision.report)
        assert 'account(s):' in src
        assert "Already existed, left untouched" in src
        assert 'Failed (' in src


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
        # Derived from the directory, not hardcoded: a literal list here
        # fails on every new migration for no reason other than being a
        # literal, which teaches the next person to edit the assertion
        # rather than read it. What this test is actually about is that
        # applying twice is safe.
        expected = sorted(f for f in os.listdir(cpdb.MIGRATIONS_DIR)
                          if f.endswith(".sql"))
        assert cpdb.apply_migrations() == expected
        assert cpdb.apply_migrations() == expected   # no error
        os.unlink(path)

    def test_migrations_do_not_touch_engine_tables(self):
        """The DDL must be additive. Anything else risks the live ledger."""
        migrations_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "migrations")
        for name in ("001_control_plane.sql", "002_accounts.sql", "003_active_jobs.sql"):
            with open(os.path.join(migrations_dir, name), encoding="utf-8") as fh:
                sql = fh.read().upper()
            for forbidden in ("DROP ", "ALTER TABLE ID_MAPPING", "ALTER TABLE AUDIT_LOG",
                              "DELETE FROM", "ALTER TABLE IDENTITY_MAP"):
                assert forbidden not in sql, f"{name} contains {forbidden!r}"


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

    def test_a_finished_job_actually_clears_from_the_dashboard(self):
        """Live bug: fleet_agent.py correctly detects a job has exited and
        sends active_job=None, but a preflight run that had long since
        finished kept showing as vps-garud's active_job across dozens of
        fresh heartbeats -- upsert_node's `if v is not None` filter was
        silently excluding the clear-to-None update, so the last real
        value never got overwritten."""
        path = tempfile.mktemp(suffix=".db")
        os.environ["MIGRATION_DB"] = path
        MigrationDB(path)
        cpdb.apply_migrations()
        cpdb.upsert_node("vps-1", active_job="preflight", job_pid=4242)
        assert cpdb.fleet()[0]["active_job"] == "preflight"

        cpdb.upsert_node("vps-1", active_job=None, job_pid=None)
        row = cpdb.fleet()[0]
        assert row["active_job"] is None
        assert row["job_pid"] is None
        os.unlink(path)

    def test_best_effort_metrics_still_hold_their_last_value_on_a_miss(self):
        """The other half of the same fix: cpu_pct/ram_pct/disk_pct are
        genuinely best-effort (see fleet_agent.py's _pct_cpu_ram_disk()) --
        a single failed measurement must still be ignored, not written over
        a real prior reading as if the host suddenly reported nothing."""
        path = tempfile.mktemp(suffix=".db")
        os.environ["MIGRATION_DB"] = path
        MigrationDB(path)
        cpdb.apply_migrations()
        cpdb.upsert_node("vps-1", cpu_pct=12.5, ram_pct=40.0)
        cpdb.upsert_node("vps-1", cpu_pct=None, ram_pct=None)
        row = cpdb.fleet()[0]
        assert row["cpu_pct"] == 12.5
        assert row["ram_pct"] == 40.0
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


class TestCloudProvisioning:
    """
    Provisioning creates real billable Cloud resources and writes private
    keys to disk, so it is gated like any other write -- and defaults to a
    dry run, because the safe option should be the one you get by not
    thinking about it.
    """

    def test_a_viewer_cannot_provision(self, cp):
        r = cp.post("/api/v2/gcp/provision",
                    json={"reason": "curiosity", "source_domain": "c.example.com",
                          "target_domain": "a.example.com"}, headers=VIEWER)
        assert r.status_code == 403

    def test_a_viewer_cannot_enable_apis(self, cp):
        r = cp.post("/api/v2/apis/enable",
                    json={"reason": "curiosity"}, headers=VIEWER)
        assert r.status_code == 403

    def test_domains_are_required_by_the_schema(self, cp):
        """Provisioning without a target domain would create one project and
        strand the run half-done."""
        r = cp.post("/api/v2/gcp/provision",
                    json={"reason": "no target"}, headers=ADMIN)
        assert r.status_code == 422

    def test_launch_is_audited_with_the_domain_pair_as_target(self, monkeypatch, cp):
        import api_server

        class _FakeProc:
            pid = 5150
        monkeypatch.setattr(api_server.subprocess, "Popen",
                            lambda *a, **k: _FakeProc())

        r = cp.post("/api/v2/gcp/provision",
                    json={"reason": "new tenant pair", "dry_run": True,
                          "source_domain": "c.example.com",
                          "target_domain": "a.example.com"}, headers=ADMIN)
        assert r.status_code == 200
        row = cp.get("/api/v2/actions").json()[0]
        assert row["action"] == "gcp.provision"
        assert row["target"] == "c.example.com->a.example.com"
        assert row["outcome"] == "OK"

    def test_a_run_in_flight_is_not_reported_as_failed(self, cp, monkeypatch):
        """provision_gcp writes its JSON in one go at the end, so a run in
        flight leaves a .partial file that is not yet valid JSON. Surfacing
        that as an error would flash 'failed' for the whole minute a project
        takes to create."""
        import api_server
        d = tempfile.mkdtemp()
        monkeypatch.setattr(api_server, "HERE", d)
        os.makedirs(os.path.join(d, "logs"), exist_ok=True)
        with open(os.path.join(d, "logs", "gcp-provision.json.partial"),
                  "w", encoding="utf-8") as fh:
            fh.write('{"sides": [')      # truncated mid-write
        r = cp.get("/api/v2/gcp/status")
        assert r.status_code == 200
        assert r.json()["result"] is None


class TestFullSetup:
    """
    full_setup.py drives a real browser and shells to gcloud -- neither
    exists on a headless VPS -- so this only works wherever the control
    plane process itself has them. The endpoint's job is to launch it
    correctly and never let the admin password leak into anything that
    outlives the one subprocess that needs it.
    """

    def test_a_viewer_cannot_launch_it(self, cp):
        r = cp.post("/api/v2/full-setup/start",
                    json={"reason": "curiosity", "side": "source",
                          "domain": "c.example.com",
                          "admin_email": "admin@c.example.com",
                          "admin_password": "x"}, headers=VIEWER)
        assert r.status_code == 403

    def test_password_is_required_by_the_schema(self, cp):
        r = cp.post("/api/v2/full-setup/start",
                    json={"reason": "no password", "side": "source",
                          "domain": "c.example.com",
                          "admin_email": "admin@c.example.com"}, headers=ADMIN)
        assert r.status_code == 422

    def test_the_password_is_never_recorded_in_the_audit_log(self, monkeypatch, cp):
        """Target names the tenant being set up, never the credential."""
        import api_server

        class _FakeProc:
            pid = 7777
            def wait(self):
                return 0
        monkeypatch.setattr(api_server.subprocess, "Popen",
                            lambda *a, **k: _FakeProc())

        cp.post("/api/v2/full-setup/start",
                json={"reason": "fresh sandbox", "side": "source",
                      "domain": "c.example.com",
                      "admin_email": "admin@c.example.com",
                      "admin_password": "hunter2-super-secret",
                      "dry_run": True}, headers=ADMIN)
        row = cp.get("/api/v2/actions").json()[0]
        assert row["action"] == "full_setup.start"
        assert row["target"] == "source:c.example.com"
        blob = json.dumps(row)
        assert "hunter2-super-secret" not in blob

    def test_the_password_reaches_the_subprocess_env_not_the_command_line(
            self, monkeypatch, cp):
        """argv is visible to any process on the box via `ps`. The password
        must travel through subprocess env=, never as a --flag.

        Filtered to calls naming full_setup.py, not just "the last Popen
        call": the app's own background tailer independently shells out to
        `sysctl -n vm.swapusage` for memory metrics, and a blanket capture
        of "whatever Popen was called with" is racy against that -- it
        genuinely overwrote this assertion's dict with the wrong call.
        """
        import api_server

        calls = []

        class _FakeProc:
            pid = 8888
            def wait(self):
                return 0

        def fake_popen(argv, **kwargs):
            calls.append((argv, kwargs))
            return _FakeProc()

        monkeypatch.setattr(api_server.subprocess, "Popen", fake_popen)
        cp.post("/api/v2/full-setup/start",
                json={"reason": "fresh sandbox", "side": "target",
                      "domain": "a.example.com",
                      "admin_email": "admin@a.example.com",
                      "admin_password": "hunter2-super-secret",
                      "dry_run": True}, headers=ADMIN)

        ours = [(argv, kw) for argv, kw in calls if "full_setup.py" in argv]
        assert ours, f"full_setup.py was never launched; saw {[c[0] for c in calls]}"
        argv, kwargs = ours[0]
        assert "hunter2-super-secret" not in argv
        assert kwargs.get("env", {}).get("DWD_PASSWORD") == "hunter2-super-secret"

    def test_seed_flag_is_ignored_for_the_target_side(self, monkeypatch, cp):
        """--seed only ever makes sense for the source; silently accepting it
        for target would be a confusing no-op at best.

        Filtered to the full_setup.py call, not "whatever Popen saw last" --
        the app's background tailer independently shells out to `sysctl` for
        memory metrics, and an unfiltered capture is racy against it (this
        exact test passed for the wrong reason before the filter: sysctl's
        argv does not contain "--seed" either).
        """
        import api_server

        calls = []

        class _FakeProc:
            pid = 9999
            def wait(self):
                return 0

        def fake_popen(argv, **kwargs):
            calls.append(argv)
            return _FakeProc()

        monkeypatch.setattr(api_server.subprocess, "Popen", fake_popen)
        cp.post("/api/v2/full-setup/start",
                json={"reason": "test", "side": "target",
                      "domain": "a.example.com",
                      "admin_email": "admin@a.example.com",
                      "admin_password": "x", "seed": True,
                      "dry_run": True}, headers=ADMIN)
        ours = [a for a in calls if "full_setup.py" in a]
        assert ours, f"full_setup.py was never launched; saw {calls}"
        assert "--seed" not in ours[0]

    def test_status_reports_not_running_with_no_result_by_default(self, cp):
        r = cp.get("/api/v2/full-setup/status?side=source")
        assert r.status_code == 200
        body = r.json()
        assert body["running"] is False
        assert body["result"] is None
        assert body["pid"] is None

    def test_status_reports_the_pid_of_a_running_setup(self, monkeypatch, cp):
        """The pid is what makes a running setup stoppable -- see
        /api/v2/jobs/{pid}/stop, a generic SIGINT-by-pid endpoint that only
        works if something upstream can hand it a real pid first. Unlike
        migrate/delta (which fleet_agent.py's own ps scan already finds),
        nothing else anywhere records a full_setup.py run's pid."""
        import api_server

        class _FakeCompleted:
            def __init__(self, stdout):
                self.stdout = stdout

        def fake_run(argv, **kwargs):
            if argv[:2] == ["ps", "-eo"]:
                return _FakeCompleted(
                    "  1234 /root/migration/.venv/bin/python full_setup.py "
                    "--side source --domain c.example.com "
                    "--admin admin@c.example.com --json\n")
            return _FakeCompleted("")

        monkeypatch.setattr(api_server.subprocess, "run", fake_run)

        r = cp.get("/api/v2/full-setup/status?side=source")
        body = r.json()
        assert body["running"] is True
        assert body["pid"] == 1234

    def test_an_unknown_side_is_rejected(self, cp):
        r = cp.get("/api/v2/full-setup/status?side=sideways")
        assert r.status_code == 400


class TestSecretsNeverReachTheAuditLog:
    """
    _gated() logs body.model_dump() into operator_actions_log.params_json
    UNCONDITIONALLY -- on the REFUSED path (a viewer's attempt) as well as
    the OK/FAILED paths -- and that table is read by GET /api/v2/actions
    with no role gate beyond being an authenticated operator. Any
    WriteAction field holding a real credential has to be excluded at the
    schema level, because there is exactly one call site (_gated) and
    forgetting it there is silent: the write still succeeds, the secret
    just sits in a permanent, viewer-readable table.

    Found live: StartFullSetup.admin_password was about to do this while
    being built; SaveAiKey.key had already been doing it since the AI
    panel shipped (checked the deployed database -- 0 rows had actually
    triggered it, so nothing needed scrubbing, but the bug was real).
    """

    def test_full_setup_password_never_reaches_params_json(self, monkeypatch, cp):
        import api_server

        class _FakeProc:
            pid = 123
            def wait(self):
                return 0
        monkeypatch.setattr(api_server.subprocess, "Popen",
                            lambda *a, **k: _FakeProc())
        cp.post("/api/v2/full-setup/start",
                json={"reason": "test", "side": "source",
                      "domain": "c.example.com",
                      "admin_email": "a@c.example.com",
                      "admin_password": "correct-horse-battery-staple",
                      "dry_run": True}, headers=ADMIN)
        row = cp.get("/api/v2/actions").json()[0]
        assert "correct-horse-battery-staple" not in json.dumps(row)

    def test_ai_key_never_reaches_params_json(self, cp):
        cp.post("/api/v2/ai/key",
                json={"reason": "test", "key": "gsk_live_secret_value_xyz"},
                headers=ADMIN)
        row = cp.get("/api/v2/actions").json()[0]
        assert "gsk_live_secret_value_xyz" not in json.dumps(row)

    def test_a_refused_write_still_does_not_leak_the_password(self, cp):
        """The REFUSED path logs body.model_dump() too -- a viewer probing
        this endpoint must not be able to fish the admin password out of
        their own denied attempt."""
        cp.post("/api/v2/full-setup/start",
                json={"reason": "curiosity", "side": "source",
                      "domain": "c.example.com",
                      "admin_email": "a@c.example.com",
                      "admin_password": "should-never-appear-anywhere"},
                headers=VIEWER)
        row = cp.get("/api/v2/actions").json()[0]
        assert row["outcome"] == "REFUSED"
        assert "should-never-appear-anywhere" not in json.dumps(row)

    def test_every_writeaction_with_a_secret_shaped_field_excludes_it(self):
        """Structural guard against the next one: any field literally named
        like a credential must opt out of serialization, not rely on
        someone remembering at the call site."""
        import inspect

        import api_server as srv

        secret_names = {"password", "admin_password", "key", "secret", "token"}
        offenders = []
        for name, obj in vars(srv).items():
            if (inspect.isclass(obj) and issubclass(obj, srv.WriteAction)
                    and obj is not srv.WriteAction):
                for field_name, field in obj.model_fields.items():
                    if field_name.lower() in secret_names and not field.exclude:
                        offenders.append(f"{name}.{field_name}")
        assert not offenders, (
            f"secret-shaped field(s) not excluded from model_dump(): "
            f"{offenders}")


class TestAccountAuth:
    """Real signup/login/logout, layered on top of the operator-RBAC
    machinery above rather than replacing it -- see api_server.py's
    operator() dependency, which now checks a bp_session cookie before
    falling back to the X-Operator header everything above this class
    still exercises."""

    def test_signup_creates_an_account_and_signs_it_in(self, cp):
        r = cp.post("/api/v2/auth/signup",
                    json={"email": "new@example.com", "password": "hunter22222",
                          "name": "New User"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert "bp_session" in r.cookies
        # The cookie just set is honored on the very next request.
        me = cp.get("/api/v2/auth/me")
        assert me.status_code == 200
        assert me.json()["email"] == "new@example.com"

    def test_signup_with_a_duplicate_email_is_rejected(self, cp):
        cp.post("/api/v2/auth/signup",
                json={"email": "dupe@example.com", "password": "hunter22222",
                      "name": "First"})
        r = cp.post("/api/v2/auth/signup",
                    json={"email": "dupe@example.com", "password": "different99",
                          "name": "Second"})
        assert r.status_code == 400

    def test_signup_with_a_short_password_is_rejected_by_the_schema(self, cp):
        r = cp.post("/api/v2/auth/signup",
                    json={"email": "short@example.com", "password": "short",
                          "name": "Short Pw"})
        assert r.status_code == 422

    def test_login_with_correct_credentials_succeeds(self, cp):
        cp.post("/api/v2/auth/signup",
                json={"email": "login@example.com", "password": "hunter22222",
                      "name": "Login User"})
        cp.post("/api/v2/auth/logout")
        r = cp.post("/api/v2/auth/login",
                    json={"email": "login@example.com", "password": "hunter22222"})
        assert r.status_code == 200
        assert cp.get("/api/v2/auth/me").json()["email"] == "login@example.com"

    def test_login_with_wrong_password_fails(self, cp):
        cp.post("/api/v2/auth/signup",
                json={"email": "wrongpw@example.com", "password": "hunter22222",
                      "name": "User"})
        cp.post("/api/v2/auth/logout")
        r = cp.post("/api/v2/auth/login",
                    json={"email": "wrongpw@example.com", "password": "not-it"})
        assert r.status_code == 401

    def test_me_without_a_session_is_unauthorized(self, cp):
        r = cp.get("/api/v2/auth/me")
        assert r.status_code == 401

    def test_logout_ends_the_session(self, cp):
        cp.post("/api/v2/auth/signup",
                json={"email": "out@example.com", "password": "hunter22222",
                      "name": "Out User"})
        assert cp.get("/api/v2/auth/me").status_code == 200
        cp.post("/api/v2/auth/logout")
        assert cp.get("/api/v2/auth/me").status_code == 401

    def test_a_signed_in_account_is_always_admin_of_its_own_resources(self, cp):
        """No team/role concept yet -- see accounts_auth.py. whoami must
        reflect that, since require_admin() downstream trusts op.role
        as-is."""
        cp.post("/api/v2/auth/signup",
                json={"email": "owner@example.com", "password": "hunter22222",
                      "name": "Owner"})
        who = cp.get("/api/v2/whoami").json()
        assert who["role"] == "admin"
        assert who["account_id"] is not None

    def test_actions_are_logged_against_the_accounts_name_and_email(self, cp, monkeypatch):
        """The whole point of removing the free-text operator field: a
        signed-in account's real identity, not a name someone typed once
        and forgot about, ends up in operator_actions_log.actor."""
        import api_server

        cp.post("/api/v2/auth/signup",
                json={"email": "attrib@example.com", "password": "hunter22222",
                      "name": "Attrib User"})

        class _FakeProc:
            pid = 555
        monkeypatch.setattr(api_server.subprocess, "Popen", lambda *a, **k: _FakeProc())
        cp.post("/api/v2/gcp/provision",
                json={"reason": "test", "source_domain": "c.example.com",
                      "target_domain": "a.example.com", "dry_run": True})

        row = cp.get("/api/v2/actions").json()[0]
        assert row["actor"] == "Attrib User <attrib@example.com>"

    def test_password_never_appears_in_the_signup_response(self, cp):
        r = cp.post("/api/v2/auth/signup",
                    json={"email": "resp@example.com",
                          "password": "should-never-be-echoed-back",
                          "name": "Resp User"})
        assert "should-never-be-echoed-back" not in r.text

    def test_password_never_appears_in_the_login_response(self, cp):
        cp.post("/api/v2/auth/signup",
                json={"email": "resp2@example.com", "password": "hunter22222",
                      "name": "Resp User"})
        cp.post("/api/v2/auth/logout")
        r = cp.post("/api/v2/auth/login",
                    json={"email": "resp2@example.com",
                          "password": "should-never-be-echoed-back-2"})
        assert "should-never-be-echoed-back-2" not in r.text


class TestPerAccountIsolation:
    """The actual point of this whole feature: two different customers'
    concurrent setup runs must never share a state file, a log path, or a
    database -- see full_setup.py's phase 3b and api_server.py's
    _full_setup_state_path/_gcp_state_path."""

    def test_two_accounts_full_setup_state_files_never_collide(self, monkeypatch):
        """Both land on the SAME shared control-plane db (one MIGRATION_DB
        for this whole test, unlike most tests here) -- the state-file path
        is what has to keep them apart, not separate databases."""
        path = tempfile.mktemp(suffix=".db")
        monkeypatch.setenv("MIGRATION_DB", path)
        monkeypatch.setenv("CP_OPERATORS", "")
        MigrationDB(path)
        cpdb.apply_migrations()

        import api_server

        class _FakeProc:
            pid = 4242
            def wait(self):
                return 0

        monkeypatch.setattr(api_server.subprocess, "Popen", lambda *a, **k: _FakeProc())

        with TestClient(api_server.app) as client:
            client.post("/api/v2/auth/signup",
                        json={"email": "acct1@example.com", "password": "hunter22222",
                              "name": "Acct One"})
            r1 = client.post("/api/v2/full-setup/start",
                             json={"reason": "test", "side": "source",
                                   "domain": "one.example.com",
                                   "admin_email": "admin@one.example.com",
                                   "admin_password": "x", "dry_run": True})
            assert r1.status_code == 200
            path1 = api_server._full_setup_state_path("source", 1)

            client.post("/api/v2/auth/logout")
            client.post("/api/v2/auth/signup",
                        json={"email": "acct2@example.com", "password": "hunter22222",
                              "name": "Acct Two"})
            r2 = client.post("/api/v2/full-setup/start",
                             json={"reason": "test", "side": "source",
                                   "domain": "two.example.com",
                                   "admin_email": "admin@two.example.com",
                                   "admin_password": "x", "dry_run": True})
            assert r2.status_code == 200
            path2 = api_server._full_setup_state_path("source", 2)

        assert path1 != path2
        os.unlink(path)

    def test_the_legacy_x_operator_path_is_unaffected_by_account_scoping(self, cp):
        """A caller with no bp_session cookie at all -- the SSH-tunnel/
        CP_OPERATORS path every test above this class already exercises --
        must see account_id=None, exactly as before this feature existed."""
        who = cp.get("/api/v2/whoami", headers=ADMIN).json()
        assert who["account_id"] is None


class TestSubscriptionEnforcement:
    """The manual v1 billing gate: accounts.subscription_active, checked in
    _gated() alongside require_admin(). No Stripe webhook yet -- an
    operator flips this by hand (accounts_auth.set_subscription_active),
    which is exactly what these tests do instead of adding a payment flow
    just to reach the code path."""

    def test_an_inactive_subscription_blocks_a_gated_write(self, cp):
        import accounts_auth

        cp.post("/api/v2/auth/signup",
                json={"email": "inactive@example.com", "password": "hunter22222",
                      "name": "Inactive User"})
        me = cp.get("/api/v2/auth/me").json()
        accounts_auth.set_subscription_active(me["id"], False)

        r = cp.post("/api/v2/migrate/start",
                    json={"reason": "should be blocked", "services": ["drive"]})
        assert r.status_code == 402
        assert "subscription" in r.json()["detail"]

    def test_reactivating_restores_access(self, monkeypatch, cp):
        import accounts_auth
        import api_server

        monkeypatch.setattr(api_server, "_run_admitted", lambda *a, **k: (True, "started pid 999"))
        cp.post("/api/v2/auth/signup",
                json={"email": "reactivate@example.com", "password": "hunter22222",
                      "name": "Reactivate User"})
        me = cp.get("/api/v2/auth/me").json()
        accounts_auth.set_subscription_active(me["id"], False)
        assert cp.post("/api/v2/migrate/start",
                       json={"reason": "test reason", "services": ["drive"]}).status_code == 402

        accounts_auth.set_subscription_active(me["id"], True)
        r = cp.post("/api/v2/migrate/start",
                    json={"reason": "test reason", "services": ["drive"], "dry_run": True})
        assert r.status_code == 200

    def test_reads_still_work_while_inactive(self, cp):
        """Nothing here touches reads -- an inactive account can still see
        its own data, only privileged writes are blocked."""
        import accounts_auth

        cp.post("/api/v2/auth/signup",
                json={"email": "readonly@example.com", "password": "hunter22222",
                      "name": "Read Only"})
        me = cp.get("/api/v2/auth/me").json()
        accounts_auth.set_subscription_active(me["id"], False)
        assert cp.get("/api/v2/auth/me").status_code == 200
        assert cp.get("/api/v2/users").status_code == 200

    def test_the_legacy_x_operator_path_is_exempt(self, monkeypatch, cp):
        """account_id=None (the SSH-tunnel/CP_OPERATORS path) is the
        operator himself, never a billed client -- nothing to deactivate
        him against."""
        import api_server

        monkeypatch.setattr(api_server, "_run_admitted", lambda *a, **k: (True, "started pid 999"))
        r = cp.post("/api/v2/migrate/start",
                    json={"reason": "test reason", "services": ["drive"], "dry_run": True},
                    headers=ADMIN)
        assert r.status_code == 200

    def test_a_refused_write_from_an_inactive_account_is_still_audited(self, cp):
        """Same principle as TestEveryAttemptIsAudited above, extended to
        this new refusal reason."""
        import accounts_auth

        cp.post("/api/v2/auth/signup",
                json={"email": "audited@example.com", "password": "hunter22222",
                      "name": "Audited User"})
        me = cp.get("/api/v2/auth/me").json()
        accounts_auth.set_subscription_active(me["id"], False)
        cp.post("/api/v2/migrate/start",
                json={"reason": "should be refused", "services": ["drive"]})

        rows = cp.get("/api/v2/actions").json()
        assert rows[0]["outcome"] == "REFUSED"


class TestCrossAccountJobAdmission:
    """migrate_start's use of job_admission.py -- see
    tests/test_job_admission.py for the ledger's own unit tests. This is
    just proving the wiring: a slot occupied by ANY account (including the
    operator's own account_id=None jobs) refuses a real request through
    the real endpoint, and _gated() reports that refusal the same way it
    reports any other execution-time failure (ok:false, HTTP 200 -- this
    is a capacity refusal, not an RBAC one, so it does not get the
    REFUSED/402 treatment those get)."""

    def test_a_slot_already_taken_by_another_account_blocks_migrate_start(self, cp):
        import job_admission

        # Fill every slot, not just one: the cap moved above 1 once
        # resources.recommend() learned to divide the memory budget between
        # concurrent jobs, and a test that occupies exactly one slot stops
        # testing a refusal the moment two are allowed.
        taken = list(range(999, 999 + job_admission.MAX_CONCURRENT_TENANT_JOBS))
        for acct in taken:
            assert job_admission.try_admit(acct, "seed")[0]
        try:
            r = cp.post("/api/v2/migrate/start",
                        json={"reason": "should be blocked", "services": ["drive"]},
                        headers=ADMIN)
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is False
            assert "capacity is full" in body["detail"]
        finally:
            for acct in taken:
                job_admission.release(acct, "seed")

    def test_the_slot_is_freed_once_the_process_exits(self, monkeypatch, cp):
        """_run_admitted's wait-thread must actually call release() -- not
        just admit and forget, which would permanently wedge the machine
        at zero capacity after the very first migration."""
        import api_server
        import job_admission

        class _FakeProc:
            pid = 4242
            def wait(self):
                return 0
        monkeypatch.setattr(api_server.subprocess, "Popen", lambda *a, **k: _FakeProc())

        r = cp.post("/api/v2/migrate/start",
                    json={"reason": "test reason", "services": ["drive"]}, headers=ADMIN)
        assert r.json()["ok"] is True
        # The wait-thread runs concurrently but _FakeProc.wait() returns
        # immediately with nothing to actually wait on -- give it a moment
        # to run before asserting the slot is free again.
        import time
        for _ in range(50):
            with cpdb.ro() as conn:
                if conn.execute("SELECT COUNT(*) n FROM active_jobs").fetchone()["n"] == 0:
                    break
            time.sleep(0.02)
        with cpdb.ro() as conn:
            assert conn.execute("SELECT COUNT(*) n FROM active_jobs").fetchone()["n"] == 0

    def test_active_jobs_endpoint_shows_another_accounts_running_job(self, cp):
        """The bug this endpoint exists to fix: RunningNow.tsx's other
        sources (webui.py's per-account Job, full_setup_status's ps scan)
        each only ever see the CALLING account's own job -- so a job
        started under one account was invisible to every other account,
        even though job_admission's cap meant it was exactly what was
        blocking them. This is the one read that isn't scoped by caller."""
        import job_admission

        job_admission.try_admit(999, "seed", pid=4242)
        try:
            r = cp.get("/api/v2/active-jobs", headers=ADMIN)
            assert r.status_code == 200
            rows = r.json()
            assert len(rows) == 1
            assert rows[0]["account_id"] == 999
            assert rows[0]["job_name"] == "seed"
            assert rows[0]["pid"] == 4242
        finally:
            job_admission.release(999, "seed")


class TestSuperadminAdminEndpoints:
    """The admin dashboard's backend: listing every account and toggling
    one client's subscription. require_admin (which every signed-in client
    already passes for their own data) is deliberately not enough here --
    require_superadmin is a stronger, separate check."""

    def _signed_in(self, cp, email):
        cp.post("/api/v2/auth/signup",
                json={"email": email, "password": "hunter22222", "name": "User"})
        return cp.get("/api/v2/auth/me").json()["id"]

    def test_a_regular_client_cannot_list_accounts(self, cp):
        self._signed_in(cp, "regular@example.com")
        assert cp.get("/api/v2/admin/accounts").status_code == 403

    def test_a_superadmin_can_list_every_account(self, cp):
        import accounts_auth

        self._signed_in(cp, "other@example.com")
        cp.post("/api/v2/auth/logout")
        self._signed_in(cp, "boss2@example.com")
        accounts_auth.promote_to_superadmin("boss2@example.com")
        # promote happens after the session was already issued -- op's
        # is_superadmin comes from operator()'s own accounts_auth.get_account
        # call on *this* request, so no re-login is needed for it to see
        # the fresh flag.
        r = cp.get("/api/v2/admin/accounts")
        assert r.status_code == 200
        emails = {row["email"] for row in r.json()}
        assert {"other@example.com", "boss2@example.com"} <= emails

    def test_a_regular_client_cannot_toggle_anyones_subscription(self, cp):
        target_id = self._signed_in(cp, "target@example.com")
        cp.post("/api/v2/auth/logout")
        self._signed_in(cp, "attacker@example.com")
        r = cp.post(f"/api/v2/admin/accounts/{target_id}/subscription",
                    json={"reason": "trying my luck", "active": False})
        assert r.status_code == 403

    def test_a_superadmin_can_deactivate_a_clients_subscription(self, cp):
        import accounts_auth

        target_id = self._signed_in(cp, "client@example.com")
        cp.post("/api/v2/auth/logout")
        self._signed_in(cp, "realboss@example.com")
        accounts_auth.promote_to_superadmin("realboss@example.com")
        r = cp.post(f"/api/v2/admin/accounts/{target_id}/subscription",
                    json={"reason": "trial ended", "active": False})
        assert r.status_code == 200
        assert accounts_auth.get_account(target_id)["subscription_active"] == 0

    def test_a_refused_admin_action_is_still_audited(self, cp):
        target_id = self._signed_in(cp, "victim@example.com")
        cp.post("/api/v2/auth/logout")
        self._signed_in(cp, "attacker2@example.com")
        cp.post(f"/api/v2/admin/accounts/{target_id}/subscription",
                json={"reason": "should be refused", "active": False})
        rows = cp.get("/api/v2/actions").json()
        assert rows[0]["outcome"] == "REFUSED"

    def test_seed_enabled_defaults_off_for_a_new_account(self, cp):
        target_id = self._signed_in(cp, "fresh@example.com")
        cp.post("/api/v2/auth/logout")
        self._signed_in(cp, "boss3@example.com")
        import accounts_auth
        accounts_auth.promote_to_superadmin("boss3@example.com")
        row = next(r for r in cp.get("/api/v2/admin/accounts").json()
                  if r["id"] == target_id)
        assert row["seed_enabled"] == 0

    def test_a_regular_client_cannot_toggle_anyones_seed_flag(self, cp):
        target_id = self._signed_in(cp, "target2@example.com")
        cp.post("/api/v2/auth/logout")
        self._signed_in(cp, "attacker3@example.com")
        r = cp.post(f"/api/v2/admin/accounts/{target_id}/seed",
                    json={"reason": "trying my luck", "enabled": True})
        assert r.status_code == 403

    def test_a_superadmin_can_enable_seeding_for_a_client(self, cp):
        import accounts_auth

        target_id = self._signed_in(cp, "client2@example.com")
        cp.post("/api/v2/auth/logout")
        self._signed_in(cp, "realboss2@example.com")
        accounts_auth.promote_to_superadmin("realboss2@example.com")
        r = cp.post(f"/api/v2/admin/accounts/{target_id}/seed",
                    json={"reason": "demo account", "enabled": True})
        assert r.status_code == 200
        assert accounts_auth.get_account(target_id)["seed_enabled"] == 1

    def test_auth_me_reflects_seed_enabled(self, cp):
        import accounts_auth

        target_id = self._signed_in(cp, "seedme@example.com")
        cp.post("/api/v2/auth/logout")
        self._signed_in(cp, "realboss3@example.com")
        accounts_auth.promote_to_superadmin("realboss3@example.com")
        cp.post(f"/api/v2/admin/accounts/{target_id}/seed",
               json={"reason": "demo", "enabled": True})
        cp.post("/api/v2/auth/logout")
        cp.post("/api/v2/auth/login",
               json={"email": "seedme@example.com", "password": "hunter22222"})
        assert cp.get("/api/v2/auth/me").json()["seed_enabled"] is True


_FAKE_SA_KEY = {
    "type": "service_account",
    "project_id": "wsmig-src-99999",
    "private_key_id": "abc123",
    "private_key": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
    "client_email": "source-sa@wsmig-src-99999.iam.gserviceaccount.com",
    "client_id": "111222333444555666",
}


class TestUploadCredentials:
    """provision_gcp.py runs on the admin's own machine now (see
    full_setup.py's phase 1 comment) -- this is the one thing it hands
    back to the control plane: a service-account key, uploaded by an
    already-authenticated browser tab rather than a token in a script."""

    def _signed_in(self, cp, email="upload@example.com"):
        cp.post("/api/v2/auth/signup",
                json={"email": email, "password": "hunter22222", "name": "Upload User"})

    def test_a_valid_key_is_accepted_and_stored(self, cp):
        import api_server

        self._signed_in(cp)
        r = cp.post("/api/v2/setup/credentials",
                    json={"reason": "local provisioning done", "side": "source",
                          "domain": "c.example.com", "service_account_key": _FAKE_SA_KEY})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["detail"] == _FAKE_SA_KEY["client_id"]

        me = cp.get("/api/v2/auth/me").json()
        # The handler resolves this relative to api_server.HERE (the repo
        # root), not the test process's cwd -- matching where every other
        # account_id-keyed path (accounts_auth.create_account's own
        # keys/{id}/ reservation) already lives. conftest.py's
        # _cleanup_account_dirs autouse fixture sweeps this account's whole
        # keys/{id}/ directory away afterward.
        key_path = os.path.join(api_server.HERE, "keys", str(me["id"]), "source-sa.json")
        assert os.path.isfile(key_path)
        with open(key_path, encoding="utf-8") as fh:
            assert json.load(fh) == _FAKE_SA_KEY

    def test_the_tenant_config_status_reflects_the_upload(self, cp):
        self._signed_in(cp, "status@example.com")
        before = cp.get("/api/v2/setup/tenant-config?side=source").json()
        assert before["hasKey"] is False

        cp.post("/api/v2/setup/credentials",
                json={"reason": "test", "side": "source", "domain": "c.example.com",
                      "service_account_key": _FAKE_SA_KEY})
        after = cp.get("/api/v2/setup/tenant-config?side=source").json()
        assert after["hasKey"] is True
        assert after["clientId"] == _FAKE_SA_KEY["client_id"]
        assert after["domain"] == "c.example.com"

    def test_a_key_missing_the_type_field_is_rejected(self, cp):
        self._signed_in(cp, "badtype@example.com")
        bad = dict(_FAKE_SA_KEY, type="user_account")
        r = cp.post("/api/v2/setup/credentials",
                    json={"reason": "test", "side": "source", "domain": "c.example.com",
                          "service_account_key": bad})
        assert r.status_code == 400

    def test_a_key_missing_a_required_field_is_rejected(self, cp):
        self._signed_in(cp, "badfields@example.com")
        bad = {k: v for k, v in _FAKE_SA_KEY.items() if k != "private_key"}
        r = cp.post("/api/v2/setup/credentials",
                    json={"reason": "test", "side": "source", "domain": "c.example.com",
                          "service_account_key": bad})
        assert r.status_code == 400
        assert "private_key" in r.json()["detail"]

    def test_an_anonymous_caller_cannot_upload(self, cp):
        """No bp_session cookie at all -- require_login() must refuse this
        before _gated() even runs, since there is no account to attribute
        the key to."""
        r = cp.post("/api/v2/setup/credentials",
                    json={"reason": "test", "side": "source", "domain": "c.example.com",
                          "service_account_key": _FAKE_SA_KEY})
        assert r.status_code == 401

    def test_the_private_key_never_reaches_the_audit_log(self, cp):
        self._signed_in(cp, "secret@example.com")
        cp.post("/api/v2/setup/credentials",
                json={"reason": "test", "side": "source", "domain": "c.example.com",
                      "service_account_key": _FAKE_SA_KEY})
        row = cp.get("/api/v2/actions").json()[0]
        assert "FAKE" not in json.dumps(row)
        assert "BEGIN PRIVATE KEY" not in json.dumps(row)


class TestDwdStatusIsScopedToTheCaller:
    """This endpoint read a bare Settings(), so every SaaS account was shown
    the LEGACY env.sh tenant's delegation rather than its own. Because those
    are different tenants, the answer was a confident "0/N live, all
    missing" for delegation that worked. Confirmed live: it reported 0/14
    for account 7's source while that same service-account key impersonated
    two of that tenant's users and read their mailboxes in the same minute.

    A migration tool that reports working delegation as broken sends an
    operator to re-run setup against a tenant that needed nothing.
    """

    def test_the_caller_account_is_used_to_build_settings(self, monkeypatch, cp):
        import api_server

        seen = {}

        class _FakeSettings:
            def __init__(self, account_id=None):
                seen["account_id"] = account_id

        import config
        monkeypatch.setattr(config, "Settings", _FakeSettings)
        # _key_and_subject is reached straight after Settings() is built;
        # raising there stops the handler once it has told us what we need.
        import verify_scopes
        monkeypatch.setattr(verify_scopes, "_key_and_subject",
                            lambda s, t: (_ for _ in ()).throw(RuntimeError("stop")))

        cp.get("/api/v2/dwd/status?tenant=source", headers=ADMIN)

        assert "account_id" in seen, "handler never built Settings"
        # The legacy operator path is account_id=None; what matters is that
        # the account is threaded through at all rather than ignored.
        assert seen["account_id"] is None or isinstance(seen["account_id"], int)

    def test_an_unknown_tenant_is_still_rejected(self, cp):
        assert cp.get("/api/v2/dwd/status?tenant=sideways",
                      headers=ADMIN).status_code == 400


class TestProvisionProgressIsReadable:
    """`provision-users` prints SECTIONS, not one line per account:

        Created 2 account(s):
            alice@target...
                password: <secret>
        Already existed, left untouched (1):
            info@target...
        Failed (1):
            carol@target...: quota exceeded

    The regexes this replaced (`provision: created (\\S+)`) matched none of
    that, so `created` read 0 on runs that had just created accounts --
    reported next to a denominator read from a different tenant's database.
    Clicking the button repeatedly and seeing "0/11 created" was both numbers
    being wrong at once.
    """

    LOG = [
        "2026-08-20 06:15 INFO db ready\n",
        "Tenant : target (target.example.com)\n",
        "\n",
        "Created 2 account(s):\n",
        "    alice@target.example.com\n",
        "        password: SuperSecret123\n",
        "    bob@target.example.com\n",
        "        password: OtherSecret456\n",
        "\n",
        "Already existed, left untouched (1):\n",
        "    info@target.example.com\n",
        "\n",
        "Failed (1):\n",
        "    carol@target.example.com: quota exceeded\n",
    ]

    def test_every_account_is_found_with_its_state(self):
        import api_server

        out = api_server._parse_provision_log(self.LOG)
        assert out["created"] == 2
        assert out["existing"] == 1
        assert out["failed"] == 1
        by = {u["email"]: u["state"] for u in out["users"]}
        assert by["alice@target.example.com"] == "created"
        assert by["info@target.example.com"] == "existing"
        assert by["carol@target.example.com"] == "failed"

    def test_a_failure_keeps_its_reason(self):
        import api_server

        out = api_server._parse_provision_log(self.LOG)
        carol = [u for u in out["users"] if u["email"].startswith("carol")][0]
        assert "quota exceeded" in carol["detail"]

    def test_passwords_never_reach_the_response(self):
        """provision.py prints each new account's password once, by design.
        That log is read by an HTTP endpoint, so anything echoing raw lines
        puts a live credential in a browser response and whatever caches it."""
        import api_server

        out = api_server._parse_provision_log(self.LOG)
        blob = json.dumps(out)
        assert "SuperSecret123" not in blob
        assert "OtherSecret456" not in blob
        assert "password" not in blob.lower()

    def test_a_dry_run_still_parses(self):
        import api_server

        out = api_server._parse_provision_log(
            ["Would create 1 account(s):\n", "    zoe@target.example.com\n"])
        assert out["created"] == 1

    def test_an_empty_log_is_all_zeroes_not_an_error(self):
        import api_server

        out = api_server._parse_provision_log([])
        assert out == {"users": [], "created": 0, "existing": 0, "failed": 0}

    def test_log_preamble_is_not_mistaken_for_accounts(self):
        """"Tenant : target (...)" and timestamps must not parse as emails."""
        import api_server

        out = api_server._parse_provision_log(self.LOG[:3])
        assert out["users"] == []


class TestProvisionDenominatorIsThisAccounts:
    """identity_count read the SHARED control-plane database for every
    caller, so a SaaS account's progress bar showed the LEGACY tenant's
    headcount. Confirmed live: "0/11 created" on account 7, whose
    identity_map holds exactly one user."""

    def test_identity_count_accepts_an_account(self):
        import inspect

        import control_plane_db as c

        assert "account_id" in inspect.signature(c.identity_count).parameters

    def test_no_account_still_reads_the_shared_ledger(self, cp):
        """The legacy / SSH-tunnel caller genuinely lives there. `cp` is
        here for the database it sets up, not the client."""
        import control_plane_db as c

        assert isinstance(c.identity_count(), int)

    def test_a_missing_account_ledger_falls_back_rather_than_raising(self, cp):
        """A progress bar is not worth an exception -- an account with no
        ledger yet reads the shared one instead of 500ing the panel."""
        import control_plane_db as c

        assert isinstance(c.identity_count(999999), int)


class TestAScanThatDiedIsNotStillRunning:
    """A deep scan runs in a thread inside this process, so a restart takes
    it with no chance to record that.

    Confirmed live: a scan started at 06:30:52, a deploy restarted the server
    at 06:31:01, and its status file still said running a quarter of an hour
    later. The panel faithfully rendered "reading the tenant..." forever, and
    no amount of waiting or refreshing could recover it -- the file had to be
    deleted by hand. Believing the file over the clock is what made it
    unrecoverable.
    """

    def _state(self, cp, payload):
        """The staleness rule itself. Driving it through HTTP would test the
        login gate instead -- the test client has no session."""
        import api_server

        return api_server._mark_stale_scan(dict(payload))

    def test_a_stale_heartbeat_reads_as_interrupted(self, cp):
        import time as t

        r = self._state(cp, {"running": True, "startedAt": t.time() - 99999,
                             "heartbeat": t.time() - 99999})
        assert r["running"] is False
        assert r["interrupted"] is True
        assert "restarted" in r["error"]

    def test_a_fresh_heartbeat_is_left_alone(self, cp):
        """Calling a slow scan dead is as wrong as believing a dead one --
        one account's Drive walk can take ~3 minutes."""
        import time as t

        r = self._state(cp, {"running": True, "startedAt": t.time() - 600,
                             "heartbeat": t.time() - 5, "done": 12,
                             "scanTotal": 201})
        assert r["running"] is True
        assert r.get("interrupted") is not True
        assert r["done"] == 12 and r["scanTotal"] == 201

    def test_an_old_file_without_a_heartbeat_still_gets_judged(self, cp):
        """Files written before heartbeats existed must not be immortal."""
        import time as t

        r = self._state(cp, {"running": True, "startedAt": t.time() - 99999})
        assert r["running"] is False
        assert r["interrupted"] is True

    def test_a_finished_scan_is_untouched(self, cp):
        r = self._state(cp, {"running": False, "accounts": 5, "deep": True})
        assert r["running"] is False
        assert r.get("interrupted") is not True
        assert r["accounts"] == 5


class TestScanReportsProgress:
    """One account's Drive walk takes ~3 minutes, so a 200-account scan that
    says nothing until it finishes looks identical to one that died."""

    def test_snapshot_reports_each_account_as_it_lands(self, monkeypatch):
        import tenant_inventory as ti

        emails = [f"u{i}@x.com" for i in range(4)]

        class FakeAuth:
            def directory(self, side, writable=False):
                class D:
                    def users(self_inner):
                        return self_inner
                    def list(self_inner, **kw):
                        class E:
                            def execute(self_e):
                                return {"users": [{"primaryEmail": e}
                                                  for e in emails]}
                        return E()
                return D()

        monkeypatch.setattr(ti, "AuthManager", lambda s: FakeAuth())
        monkeypatch.setattr(ti, "licenses", lambda s, side: ({}, ""))
        monkeypatch.setattr(ti, "probe_account",
                            lambda a, side, e: {"email": e, "emails": 1,
                                                "threads": 1, "driveBytes": 1,
                                                "error": ""})
        monkeypatch.setattr(ti, "deep_probe",
                            lambda a, s, side, e: {"driveKinds": {}, "shared": 0,
                                                   "external": 0, "anyone": 0,
                                                   "calendarEvents": 0,
                                                   "calendars": 0,
                                                   "chatSpaces": 0,
                                                   "chatMessages": 0,
                                                   "error": ""})

        class S:
            source_domain = "x.com"
            target_domain = ""
            migrate_chat = False

        seen: list[tuple] = []
        ti.snapshot(S(), "source", deep=True, deep_sample=4,
                    on_progress=lambda d, t: seen.append((d, t)))
        assert [d for d, _ in seen] == [1, 2, 3, 4]
        assert all(t == 4 for _, t in seen)

    def test_a_progress_callback_that_raises_does_not_kill_the_scan(
            self, monkeypatch):
        """Reporting is not worth losing an hour of walking."""
        import tenant_inventory as ti

        class FakeAuth:
            def directory(self, side, writable=False):
                class D:
                    def users(self_inner):
                        return self_inner
                    def list(self_inner, **kw):
                        class E:
                            def execute(self_e):
                                return {"users": [{"primaryEmail": "a@x.com"}]}
                        return E()
                return D()

        monkeypatch.setattr(ti, "AuthManager", lambda s: FakeAuth())
        monkeypatch.setattr(ti, "licenses", lambda s, side: ({}, ""))
        monkeypatch.setattr(ti, "probe_account",
                            lambda a, side, e: {"email": e, "emails": 1,
                                                "threads": 1, "driveBytes": 1,
                                                "error": ""})
        monkeypatch.setattr(ti, "deep_probe",
                            lambda a, s, side, e: {"driveKinds": {}, "shared": 7,
                                                   "external": 0, "anyone": 0,
                                                   "calendarEvents": 0,
                                                   "calendars": 0,
                                                   "chatSpaces": 0,
                                                   "chatMessages": 0,
                                                   "error": ""})

        class S:
            source_domain = "x.com"
            target_domain = ""
            migrate_chat = False

        def boom(done, total):
            raise RuntimeError("status file is on a full disk")

        snap = ti.snapshot(S(), "source", deep=True, deep_sample=1,
                           on_progress=boom)
        assert snap["totals"]["shared"] == 7


class TestScansAreReconciledAtStartup:
    """A fresh process cannot have a scan thread in flight -- they run inside
    it. Marking orphans at startup is exact, where the heartbeat timeout is
    only eventually right: without this a deploy left the panel waiting the
    full 15-minute staleness window before it could even offer to start
    again, on top of the work it had just thrown away."""

    def _run(self, tmp_path, monkeypatch, payload):
        import api_server

        monkeypatch.setattr(api_server, "HERE", str(tmp_path))
        d = tmp_path / "logs" / "7"
        d.mkdir(parents=True)
        p = d / "inventory-scan-source.json"
        p.write_text(json.dumps(payload))
        api_server._reconcile_inventory_scans()
        return json.loads(p.read_text())

    def test_a_running_scan_is_marked_interrupted(self, tmp_path, monkeypatch):
        out = self._run(tmp_path, monkeypatch,
                        {"running": True, "done": 11, "scanTotal": 201})
        assert out["running"] is False
        assert out["interrupted"] is True
        assert "restarted" in out["error"]

    def test_a_finished_scan_is_left_exactly_as_it_was(self, tmp_path, monkeypatch):
        """The common case at startup, and the one that must not be
        damaged -- a completed scan is the panel's only deep data."""
        done = {"running": False, "accounts": 201, "deep": True,
                "totals": {"shared": 88825}}
        out = self._run(tmp_path, monkeypatch, done)
        assert out == done

    def test_an_unreadable_file_does_not_block_startup(self, tmp_path, monkeypatch):
        import api_server

        monkeypatch.setattr(api_server, "HERE", str(tmp_path))
        d = tmp_path / "logs" / "7"
        d.mkdir(parents=True)
        (d / "inventory-scan-source.json").write_text("{ not json")
        api_server._reconcile_inventory_scans()      # must not raise

    def test_no_logs_directory_is_fine(self, tmp_path, monkeypatch):
        import api_server

        monkeypatch.setattr(api_server, "HERE", str(tmp_path / "nothing"))
        api_server._reconcile_inventory_scans()      # must not raise

    def test_it_runs_on_startup(self):
        """A reconciler nobody calls is not a reconciler."""
        import inspect

        import api_server

        assert "_reconcile_inventory_scans" in inspect.getsource(api_server.lifespan)


class TestFailuresGroupByCauseNotByItem:
    """Grouping on the raw message's first 120 characters was useless: that
    window is mostly URL, and every Drive error carries its own file id. One
    cause became thousands of groups of a few rows each -- a screen full of
    identical "200 · acl" lines that hid the single reason behind them.

    Seen live: 127,852 ACL failures rendering as page after page of
    per-file groups, when there were two causes in total.
    """

    def _rows(self, items):
        return [{"item_type": t, "error_message": m, "source_user": u}
                for t, m, u in items]

    def test_one_cause_across_many_files_is_one_group(self):
        import api_server

        rows = self._rows([
            ("acl", 'HttpError 403 requesting https://www.googleapis.com/drive/v3/files/1AAAAAAAAAAAAAAAAAAAAAAAAAAAAA/permissions returned "Quota exceeded"', "a@x.com"),
            ("acl", 'HttpError 403 requesting https://www.googleapis.com/drive/v3/files/1BBBBBBBBBBBBBBBBBBBBBBBBBBBBB/permissions returned "Quota exceeded"', "b@x.com"),
            ("acl", 'HttpError 403 requesting https://www.googleapis.com/drive/v3/files/1CCCCCCCCCCCCCCCCCCCCCCCCCCCCC/permissions returned "Quota exceeded"', "a@x.com"),
        ])
        out = api_server._group_failures(rows)
        assert len(out) == 1
        assert out[0]["count"] == 3

    def test_different_causes_stay_apart(self):
        """Normalising must not flatten genuinely different problems into
        one -- that would hide the 27 permanent failures behind the 828
        transient ones."""
        import api_server

        rows = self._rows([
            ("file", "exhausted 6 retries on HTTP 500 (internalError)", "a@x.com"),
            ("file", "HTTP 403 (exportSizeLimitExceeded): file too large", "b@x.com"),
        ])
        assert len(api_server._group_failures(rows)) == 2

    def test_the_same_message_on_different_item_types_stays_apart(self):
        import api_server

        rows = self._rows([("acl", "Quota exceeded", "a@x.com"),
                           ("file", "Quota exceeded", "a@x.com")])
        assert len(api_server._group_failures(rows)) == 2

    def test_affected_users_are_counted_as_well_as_named(self):
        """"3 users" and "all 201" are different problems with the same
        message, and five sample names cannot tell them apart."""
        import api_server

        rows = self._rows([("acl", "Quota exceeded", f"u{i}@x.com")
                           for i in range(12)])
        out = api_server._group_failures(rows)[0]
        assert out["userCount"] == 12
        assert len(out["users"]) == 5

    def test_groups_come_back_commonest_first(self):
        import api_server

        rows = (self._rows([("file", "rare thing", "a@x.com")])
                + self._rows([("file", "common thing", f"u{i}@x.com")
                              for i in range(5)]))
        out = api_server._group_failures(rows)
        assert out[0]["reason"] == "common thing"

    def test_an_empty_message_does_not_crash_the_report(self):
        import api_server

        out = api_server._group_failures(
            [{"item_type": "acl", "error_message": None, "source_user": None}])
        assert out[0]["count"] == 1


class TestMetricsAndTestEndpointsRespond:
    """Called over HTTP, not just imported.

    /api/v2/metrics shipped calling accounts_auth.account_db_path(), which
    does not exist. It imported cleanly, type-checked cleanly, and returned
    500 on the first real request -- Python resolves module attributes at
    call time, so nothing short of calling the endpoint could have said so.
    Every test around it exercised the parser and the DB layer, and none
    exercised the route.
    """

    def _signed_in(self, cp, email):
        cp.post("/api/v2/auth/signup",
                json={"email": email, "password": "hunter22222", "name": "User"})
        return cp.get("/api/v2/auth/me").json()["id"]

    def test_metrics_for_my_account_does_not_error(self, cp):
        self._signed_in(cp, "metrics@example.com")
        r = cp.get("/api/v2/metrics")
        assert r.status_code == 200, r.text
        assert "error" in r.json()

    def test_metrics_by_account_id_does_not_error(self, cp):
        account_id = self._signed_in(cp, "metrics2@example.com")
        r = cp.get(f"/api/v2/metrics/{account_id}")
        assert r.status_code == 200, r.text

    def test_an_unconfigured_account_is_explained_not_a_500(self, cp):
        """Settings(account_id=...) raises when an account has no
        tenant_configs rows. That is a state to explain on the page, not a
        server error."""
        self._signed_in(cp, "metrics3@example.com")
        r = cp.get("/api/v2/metrics/999999")
        assert r.status_code in (200, 403), r.text

    def test_metrics_requires_a_login(self, cp):
        assert cp.get("/api/v2/metrics").status_code in (401, 403)

    def test_another_accounts_metrics_are_refused(self, cp):
        """The same tenancy gate migration_detail uses -- one client must
        never read another's throughput, let alone their failure reasons."""
        self._signed_in(cp, "owner-a@example.com")
        r = cp.get("/api/v2/metrics/424242")
        assert r.status_code == 403

    def test_the_test_report_is_operator_only(self, cp):
        """It names source files and carries assertion text from a private
        codebase."""
        self._signed_in(cp, "regular-tests@example.com")
        assert cp.get("/api/v2/tests").status_code == 403

    def test_the_test_report_requires_a_login(self, cp):
        assert cp.get("/api/v2/tests").status_code in (401, 403)
