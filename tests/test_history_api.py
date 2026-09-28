"""The History page: every run this account has ever had, newest first.

Almost all of it is run_watch's own run_events -- every job that ever passed
through admission, whatever launched it. repair is the one job kind that does
not (it runs as a plain background thread, see api_server._start_repair), so
it is merged in here from its own small ledger table instead.
"""
import os
import tempfile

import pytest

pytest.importorskip("httpx2", reason="starlette TestClient needs httpx2")
from fastapi.testclient import TestClient  # noqa: E402

import api_server  # noqa: E402
import control_plane_db as cpdb  # noqa: E402
import run_watch  # noqa: E402
from db import MigrationDB  # noqa: E402


@pytest.fixture
def cp(monkeypatch, tmp_path):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    monkeypatch.setenv("BITPORT_SIGNUP_OPEN", "1")
    MigrationDB(path)
    cpdb.apply_migrations()
    ledger = str(tmp_path / "ledger.db")
    monkeypatch.setattr(api_server, "_ledger_path", lambda aid: ledger)
    with TestClient(api_server.app) as client:
        client.ledger = MigrationDB(ledger)
        yield client
    try:
        os.unlink(path)
    except OSError:
        pass


def _signup(client, email="a@example.com"):
    r = client.post("/api/v2/auth/signup", json={"email": email, "password": "hunter22222", "name": "Tester"})
    assert r.status_code == 200, r.text
    return client.get("/api/v2/auth/me").json()["id"]


class TestTheView:
    def test_it_needs_a_login(self, cp):
        assert cp.get("/api/v2/history").status_code in (401, 403)

    def test_an_account_with_nothing_recorded_gets_an_empty_list_not_an_error(self, cp):
        aid = _signup(cp)
        v = cp.get("/api/v2/history", params={"account_id": aid}).json()
        assert v == {"accountId": aid, "runs": []}

    def test_a_finished_migrate_shows_up(self, cp):
        aid = _signup(cp)
        run_watch.record_started(aid, "migrate", 100, "2026-09-26T01:00:00Z")
        run_watch.record_finished(aid, "migrate", 0, 100, detail="clean")
        v = cp.get("/api/v2/history", params={"account_id": aid}).json()
        assert len(v["runs"]) == 1
        r = v["runs"][0]
        assert r["jobName"] == "migrate" and r["rc"] == 0 and r["running"] is False
        assert r["detail"] == "clean"

    def test_every_job_kind_that_ever_registered_shows_up(self, cp):
        """Not a hardcoded allowlist -- whatever job_admission ever saw."""
        aid = _signup(cp)
        for name in ("migrate", "delta", "seed", "reset target", "wipe target",
                    "full-setup", "verify", "user-tally", "tally", "dms", "trim-filler"):
            run_watch.record_started(aid, name, 1)
            run_watch.record_finished(aid, name, 0, 1)
        names = {r["jobName"] for r in cp.get("/api/v2/history", params={"account_id": aid}).json()["runs"]}
        assert names == {"migrate", "delta", "seed", "reset target", "wipe target",
                         "full-setup", "verify", "user-tally", "tally", "dms", "trim-filler"}

    def test_a_run_still_going_has_no_finish_time_or_exit_code(self, cp):
        aid = _signup(cp)
        run_watch.record_started(aid, "seed", 200)
        r = cp.get("/api/v2/history", params={"account_id": aid}).json()["runs"][0]
        assert r["running"] is True and r["finishedAt"] is None and r["rc"] is None

    def test_another_accounts_runs_never_appear(self, cp):
        aid = _signup(cp, "a@example.com")
        run_watch.record_started(aid, "migrate", 1)
        run_watch.record_finished(aid, "migrate", 0, 1)
        other = _signup(cp, "b@example.com")   # now logged in as this second account
        run_watch.record_started(other, "seed", 2)
        run_watch.record_finished(other, "seed", 0, 2)
        # Logged in as "other", asking for nothing in particular: gets ITS OWN
        # history, never the first account's -- op.account_id, not a guess.
        names = [r["jobName"] for r in cp.get("/api/v2/history").json()["runs"]]
        assert names == ["seed"]

    def test_a_non_superadmin_cannot_read_another_accounts_history_by_asking_for_it(self, cp):
        aid = _signup(cp, "a@example.com")
        _signup(cp, "b@example.com")   # now logged in as this second, non-superadmin account
        assert cp.get("/api/v2/history", params={"account_id": aid}).status_code == 403

    def test_a_crash_carries_its_negative_exit_code_through(self, cp):
        aid = _signup(cp)
        run_watch.record_started(aid, "migrate", 1)
        run_watch.record_finished(aid, "migrate", -6, 1)
        assert cp.get("/api/v2/history", params={"account_id": aid}).json()["runs"][0]["rc"] == -6

    def test_repair_runs_are_merged_in_from_their_own_table(self, cp):
        """repair is launched as a plain thread, not an admitted subprocess --
        it never appears in run_events at all, so it has to come from somewhere else."""
        aid = _signup(cp)
        run_id = cp.ledger.repair_started()
        cp.ledger.repair_finished(run_id, "3 grants reapplied")
        r = cp.get("/api/v2/history", params={"account_id": aid}).json()["runs"][0]
        assert r["jobName"] == "repair" and r["rc"] == 0 and r["detail"] == "3 grants reapplied"

    def test_a_repair_that_errored_is_a_nonzero_exit_not_a_clean_one(self, cp):
        aid = _signup(cp)
        run_id = cp.ledger.repair_started()
        cp.ledger.repair_finished(run_id, "", "directory unreachable")
        r = cp.get("/api/v2/history", params={"account_id": aid}).json()["runs"][0]
        assert r["rc"] == 1 and "directory unreachable" in r["detail"]

    def test_a_repair_still_running_is_shown_as_running_too(self, cp):
        aid = _signup(cp)
        cp.ledger.repair_started()
        r = cp.get("/api/v2/history", params={"account_id": aid}).json()["runs"][0]
        assert r["running"] is True

    def test_migrate_and_repair_history_interleave_by_time_not_by_source(self, cp):
        aid = _signup(cp)
        run_watch.record_started(aid, "migrate", 1, "2026-09-26T01:00:00Z")
        run_watch.record_finished(aid, "migrate", 0, 1)
        run_id = cp.ledger.repair_started()
        cp.ledger.conn.execute("UPDATE repair_runs SET started_at=? WHERE id=?",
                              ("2026-09-27T01:00:00Z", run_id))
        cp.ledger.conn.commit()
        cp.ledger.repair_finished(run_id, "ok")
        names = [r["jobName"] for r in cp.get("/api/v2/history", params={"account_id": aid}).json()["runs"]]
        assert names == ["repair", "migrate"]      # newest (repair) first

    def test_an_account_with_no_ledger_file_yet_still_returns_its_run_events(self, cp, monkeypatch, tmp_path):
        """A brand-new account has registered a job (real) but has never had a ledger
        created (also real, e.g. before the first tenant is configured) -- repair_runs
        living in a file that does not exist yet must not make the whole request fail."""
        aid = _signup(cp)
        run_watch.record_started(aid, "migrate", 1)
        run_watch.record_finished(aid, "migrate", 0, 1)
        monkeypatch.setattr(api_server, "_ledger_path", lambda a: str(tmp_path / "no-such-ledger.db"))
        v = cp.get("/api/v2/history", params={"account_id": aid}).json()
        assert v["runs"][0]["jobName"] == "migrate"
