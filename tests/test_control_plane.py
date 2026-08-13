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
        expected = ["001_control_plane.sql", "002_accounts.sql"]
        assert cpdb.apply_migrations() == expected
        assert cpdb.apply_migrations() == expected   # no error
        os.unlink(path)

    def test_migrations_do_not_touch_engine_tables(self):
        """The DDL must be additive. Anything else risks the live ledger."""
        migrations_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "migrations")
        for name in ("001_control_plane.sql", "002_accounts.sql"):
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

        monkeypatch.setattr(api_server, "_spawn", lambda argv: (True, "started pid 999"))
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

        monkeypatch.setattr(api_server, "_spawn", lambda argv: (True, "started pid 999"))
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
