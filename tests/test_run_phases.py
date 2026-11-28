"""One record per migrate run lists its phases; a start after a stop resumes at the first not DONE.

The record says what finished cleanly. Whether a pass is skipped is also decided by the per-user
ledger evidence, so a record that disagrees with the ledger costs a re-walk, never a skipped user.
"""
import contextlib
import os
import tempfile

import pytest

import main
import run_phases as rp

ALL = {"drive", "gmail", "calendar", "contacts", "tasks"}
PASSES = ["drive", "after_drive", "gmail", "calendar+contacts+tasks"]


def states(db):
    return {p["name"]: p["state"] for p in rp.latest(db.conn)["phases"]}


class TestTheRecord:
    def test_phases_are_recorded_in_order_with_their_follow_ons_pending(self, db):
        run = rp.begin(db, PASSES, ["repair", "dms", "tally", "tally_fix"], pid=7)
        rec = rp.latest(db.conn)
        assert rec["run"] == run and rec["pid"] == 7
        assert [p["name"] for p in rec["phases"]] == PASSES + ["repair", "dms", "tally"]
        assert {p["state"] for p in rec["phases"]} == {"PENDING"}
        assert rec["resumeFrom"] == "drive" and rec["state"] == "PENDING"

    def test_states_times_and_attempts_follow_the_phase(self, db):
        run = rp.begin(db, PASSES)
        rp.set_state(db, run, "drive", "RUNNING")
        p = rp.latest(db.conn)["phases"][0]
        assert p["state"] == "RUNNING" and p["startedAt"] and p["endedAt"] is None and p["attempts"] == 1
        rp.set_state(db, run, "drive", "DONE")
        p = rp.latest(db.conn)["phases"][0]
        assert p["state"] == "DONE" and p["endedAt"]
        assert rp.latest(db.conn)["resumeFrom"] == "after_drive"

    def test_a_finished_record_is_not_reopened(self, db):
        run = rp.begin(db, PASSES)
        for n in PASSES:
            rp.set_state(db, run, n, "DONE")
        assert rp.latest(db.conn)["state"] == "DONE"
        assert rp.begin(db, PASSES) != run

    def test_a_different_plan_is_a_new_record(self, db):
        run = rp.begin(db, PASSES)
        assert rp.begin(db, ["gmail"]) != run

    def test_a_restart_turns_a_running_phase_into_stopped_and_names_the_resume_point(self, db):
        run = rp.begin(db, PASSES, ["repair"])
        rp.set_state(db, run, "drive", "DONE")
        rp.set_state(db, run, "after_drive", "DONE")
        rp.set_state(db, run, "gmail", "RUNNING")         # the process was killed here
        assert rp.begin(db, PASSES, ["repair"], pid=9) == run
        rec = rp.latest(db.conn)
        assert states(db)["gmail"] == "STOPPED"
        assert rec["resumes"] == 1 and rec["resumedFrom"] == "gmail" and rec["pid"] == 9
        assert rec["resumeFrom"] == "gmail"

    def test_follow_ons_not_done_follow_the_new_launch_and_done_ones_stay(self, db):
        run = rp.begin(db, ["gmail"], ["repair", "dms"])
        rp.set_state(db, run, "gmail", "STOPPED")
        rp.set_state(db, run, "repair", "DONE")
        rp.begin(db, ["gmail"], ["repair", "tally"])
        assert [p["name"] for p in rp.latest(db.conn)["phases"]] == ["gmail", "repair", "tally"]
        assert states(db)["repair"] == "DONE"

    def test_orphaned_repair_is_closed_by_an_api_restart(self, db):
        run = rp.begin(db, ["gmail"], ["repair"])
        rp.set_state(db, run, "repair", "RUNNING")
        with db.write() as c:
            assert rp.close_orphaned_repair(c) == 1
        assert states(db)["repair"] == "STOPPED"

    def test_follow_on_kinds(self):
        assert rp.follow_on_name("tally@a,b") == "tally" and rp.follow_on_name("repair") == "repair"
        assert rp.follow_on_name("tally_fix") is None and rp.follow_on_name("verify-repair:x") is None


@pytest.fixture
def runner(monkeypatch, settings, db):
    """The shared runner with everything around run_batch stubbed out (see test_migrate_ordered_passes)."""
    ran, after = [], []
    plan = {"stop_after": None, "fail": set(), "needs": None}

    @contextlib.contextmanager
    def fake_registered(name, account):
        yield

    def fake_batch(auth, d, s, services, **kw):
        ran.append(main._phase_name(services))
        if plan["stop_after"] == ran[-1]:
            main.SHUTDOWN.set()
        return [{"source": "u", "status": "FAILED" if ran[-1] in plan["fail"] else "DONE", "services": {}}]

    monkeypatch.setattr(main, "_registered", fake_registered)
    monkeypatch.setattr(main, "run_batch", fake_batch)
    monkeypatch.setattr(main, "_gate_on_delegation", lambda s: None)
    monkeypatch.setattr(main, "demote_stale_running", lambda d, only=None: 0)
    monkeypatch.setattr(main, "reconcile_service_markers", lambda d, only=None: [])
    monkeypatch.setattr(main.memtrace, "start", lambda: None)
    monkeypatch.setattr(main, "_memory_watchdog", lambda stop, *a, **k: stop.wait(0.01))
    monkeypatch.setattr(main, "_metrics_flusher", lambda stop, *a, **k: stop.wait(0.01))
    monkeypatch.setattr(main, "_migrate_shared_drives", lambda *a: after.append("shared"))
    monkeypatch.setattr(main, "_repair_between_passes", lambda *a: after.append("between"))
    monkeypatch.setattr(main, "_ensure_run_accounts", lambda *a, **k: None)
    monkeypatch.setattr(main, "_before_passes", lambda *a, **k: None)
    monkeypatch.setattr(main, "_run_pass", lambda auth, d, s, svc, *a: fake_batch(auth, d, s, svc))
    # By default the ledger says a user still needs every pass (nothing proven yet).
    monkeypatch.setattr(main, "_pass_user_pairs",
                        lambda d, s, svc, delta, only: [] if plan["needs"] == "nobody" else [("u", "t")])
    monkeypatch.setattr(settings, "dry_run", False, raising=False)
    main.MEMORY_PAUSE.clear()
    main.SHUTDOWN.clear()

    def run(only=None):
        main.SHUTDOWN.clear()
        try:
            return main._run_with_memory_pause(None, db, settings, ALL, delta=False, delta_days=0,
                                               passes=main.ordered_passes(ALL), only=only)
        finally:
            main.SHUTDOWN.clear()
    run.ran, run.after, run.plan = ran, after, plan
    yield run
    main.MEMORY_PAUSE.clear()
    main.SHUTDOWN.clear()


class TestTheRunWritesIt:
    def test_every_phase_is_done_in_order_after_a_clean_run(self, runner, db, monkeypatch):
        monkeypatch.setenv("RUN_FOLLOW_ONS", "repair,dms")
        runner()
        assert runner.ran == ["drive", "gmail", "calendar+contacts+tasks"]
        assert states(db) == {"drive": "DONE", "after_drive": "DONE", "gmail": "DONE",
                              "calendar+contacts+tasks": "DONE", "repair": "PENDING", "dms": "PENDING"}
        assert runner.after == ["shared", "between"]

    def test_a_stop_leaves_the_pass_stopped_and_the_rest_pending(self, runner, db):
        runner.plan["stop_after"] = "gmail"
        runner()
        assert states(db) == {"drive": "DONE", "after_drive": "DONE", "gmail": "STOPPED",
                              "calendar+contacts+tasks": "PENDING"}

    def test_a_pass_with_a_failed_user_is_failed_not_done(self, runner, db):
        runner.plan["fail"] = {"drive"}
        runner()
        assert states(db)["drive"] == "FAILED"

    def test_a_crash_inside_a_pass_is_recorded_and_still_raised(self, runner, db, monkeypatch):
        monkeypatch.setattr(main, "_run_pass", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            runner()
        assert states(db)["drive"] == "FAILED"
        assert "boom" in rp.latest(db.conn)["phases"][0]["note"]

    def test_a_chosen_few_keeps_no_record(self, runner, db, settings):
        main._run_with_memory_pause(None, db, settings, ALL, delta=False, delta_days=0,
                                    passes=main.ordered_passes(ALL), only=["a@x"])
        assert rp.latest(db.conn) is None

    def test_a_selected_recovery_can_opt_into_a_recorded_phase_cursor(self, runner, db, monkeypatch):
        monkeypatch.setenv("RUN_RECORD_SCOPED", "true")
        runner.plan["stop_after"] = "drive"
        runner(only=["a@x"])
        first = rp.latest(db.conn)
        assert first["resumeFrom"] == "drive" and states(db)["drive"] == "STOPPED"

        runner.ran.clear()
        runner.plan["stop_after"] = None
        runner.plan["needs"] = "nobody"
        runner(only=["a@x"])
        resumed = rp.latest(db.conn)
        assert resumed["run"] == first["run"] and resumed["resumes"] == 1
        assert resumed["resumedFrom"] == "drive"
        assert runner.ran == ["drive", "gmail", "calendar+contacts+tasks"]


class TestARestartResumes:
    def test_it_starts_at_the_first_phase_not_done_when_the_ledger_agrees(self, runner, db):
        runner.plan["stop_after"] = "gmail"
        runner()
        runner.ran.clear()
        runner.after.clear()
        runner.plan["stop_after"] = None
        runner.plan["needs"] = "nobody"          # nobody still needs Drive in the ledger
        runner()
        # Drive and its after-steps were not walked again; gmail (STOPPED) and the rest ran.
        assert runner.ran == ["gmail", "calendar+contacts+tasks"]
        assert runner.after == []
        rec = rp.latest(db.conn)
        assert rec["state"] == "DONE" and rec["resumes"] == 1 and rec["resumedFrom"] == "gmail"

    def test_a_done_phase_is_never_skipped_when_the_ledger_says_a_user_still_needs_it(self, runner, db):
        runner.plan["stop_after"] = "gmail"
        runner()
        runner.ran.clear()
        runner.after.clear()
        runner.plan["stop_after"] = None
        runner.plan["needs"] = None              # a user is still owed Drive and everything else
        runner()
        assert runner.ran == ["drive", "gmail", "calendar+contacts+tasks"]
        assert runner.after == ["shared", "between"]

    def test_a_killed_run_resumes_the_same_way(self, runner, db):
        """SIGKILL leaves the phase RUNNING and nothing after it; the next start closes it."""
        run = rp.begin(db, PASSES, ["repair"])
        for n in ("drive", "after_drive"):
            rp.set_state(db, run, n, "DONE")
        rp.set_state(db, run, "gmail", "RUNNING")
        runner.plan["needs"] = "nobody"
        runner()
        assert runner.ran == ["gmail", "calendar+contacts+tasks"]
        assert states(db)["repair"] == "PENDING"        # the follow-on the dead parent never ran, still shown

    def test_a_done_record_with_pending_follow_ons_makes_the_next_start_skip_every_pass(
            self, runner, db, capsys):
        run = rp.begin(db, PASSES, ["repair"])
        for n in PASSES:
            rp.set_state(db, run, n, "DONE")
        runner.plan["needs"] = "nobody"
        runner()
        assert runner.ran == [] and runner.after == []
        assert capsys.readouterr().out.count("already done") == 3
        assert rp.latest(db.conn)["run"] == run and rp.latest(db.conn)["resumeFrom"] == "repair"


class TestTheEndpoint:
    def test_it_returns_the_latest_record_and_needs_access(self, monkeypatch, tmp_path):
        pytest.importorskip("httpx2", reason="starlette TestClient needs httpx2")
        from fastapi.testclient import TestClient

        import api_server
        import control_plane_db as cpdb
        from db import MigrationDB

        path = tempfile.mktemp(suffix=".db")
        monkeypatch.setenv("MIGRATION_DB", path)
        monkeypatch.setenv("BITPORT_SIGNUP_OPEN", "1")
        MigrationDB(path)
        cpdb.apply_migrations()
        ledger = str(tmp_path / "ledger.db")
        monkeypatch.setattr(api_server, "_ledger_path", lambda aid: ledger)
        try:
            with TestClient(api_server.app) as client:
                assert client.get("/api/v2/runs/1/phases").status_code in (401, 403)
                r = client.post("/api/v2/auth/signup", json={"email": "a@example.com",
                                                             "password": "hunter22222", "name": "Tester"})
                assert r.status_code == 200, r.text
                aid = client.get("/api/v2/auth/me").json()["id"]
                assert client.get(f"/api/v2/runs/{aid}/phases").json() == {"accountId": aid, "record": None}
                led = MigrationDB(ledger)
                run = rp.begin(led, ["drive", "gmail"], ["repair"])
                rp.set_state(led, run, "drive", "DONE")
                rec = client.get(f"/api/v2/runs/{aid}/phases").json()["record"]
                assert [(p["name"], p["state"]) for p in rec["phases"]] == [
                    ("drive", "DONE"), ("gmail", "PENDING"), ("repair", "PENDING")]
                assert rec["resumeFrom"] == "gmail" and rec["state"] == "PENDING"
                assert client.get(f"/api/v2/runs/{aid + 99}/phases").status_code in (401, 403, 404)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
