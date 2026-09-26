"""The verification page reads what the checker stored, and can ask for it again.

Two rules matter more than the shape of the JSON:
  * a user nobody has checked is NOT_VERIFIED -- an empty row must never read as a pass;
  * a check that found differences must not look like a crashed job to whatever watches
    job exit codes.
"""
import os
import tempfile

import pytest

pytest.importorskip("httpx2", reason="starlette TestClient needs httpx2")
from fastapi.testclient import TestClient  # noqa: E402

import api_server  # noqa: E402
import control_plane_db as cpdb  # noqa: E402
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


def _users(db, *names):
    for n in names:
        db.conn.execute("INSERT OR REPLACE INTO identity_map(source_email, target_email, entity_type, status) "
                        "VALUES (?,?,?,?)", (f"{n}@a.com", f"{n}@b.com", "user", "DONE"))
    db.conn.commit()


def _row(db, user, svc, verdict, **payload):
    db.save_user_verification(f"{user}@a.com", svc, verdict, 5, 5 if verdict == "IDENTICAL" else 3, 40,
                              {"counts": {"differences": 2 if verdict == "DIFFERENCES" else 0},
                               "differences": [{"item": "x"}] if verdict == "DIFFERENCES" else [], **payload})


class TestTheView:
    def test_it_needs_a_login(self, cp):
        assert cp.get("/api/v2/one-to-one").status_code in (401, 403)

    def test_a_user_nobody_checked_is_not_verified_rather_than_fine(self, cp):
        aid = _signup(cp)
        _users(cp.ledger, "ann", "bob")
        _row(cp.ledger, "ann", "drive", "IDENTICAL")
        v = cp.get("/api/v2/one-to-one", params={"account_id": aid}).json()
        by = {u["user"]: u for u in v["users"]}
        assert by["ann@a.com"]["verdict"] == "IDENTICAL"
        assert by["bob@a.com"]["verdict"] == "NOT_VERIFIED" and by["bob@a.com"]["services"] == []
        assert v["totals"] == {"IDENTICAL": 1, "DIFFERENCES": 0, "INCOMPLETE": 0, "NOT_VERIFIED": 1}

    def test_a_users_verdict_is_their_worst_service(self, cp):
        aid = _signup(cp)
        _users(cp.ledger, "ann")
        _row(cp.ledger, "ann", "drive", "IDENTICAL")
        _row(cp.ledger, "ann", "gmail", "DIFFERENCES")
        _row(cp.ledger, "ann", "tasks", "INCOMPLETE")
        u = cp.get("/api/v2/one-to-one", params={"account_id": aid}).json()["users"][0]
        assert u["verdict"] == "DIFFERENCES" and [s["service"] for s in u["services"]] == ["drive", "gmail", "tasks"]

    def test_what_is_wrong_and_how_much_was_sampled_reach_the_page(self, cp):
        aid = _signup(cp)
        _users(cp.ledger, "ann")
        _row(cp.ledger, "ann", "gmail", "DIFFERENCES")
        s = cp.get("/api/v2/one-to-one", params={"account_id": aid}).json()["users"][0]["services"][0]
        assert s["sampledOf"] == 40 and s["checked"] == 5 and s["differences"] == [{"item": "x"}]
        assert s["counts"]["differences"] == 2 and s["verifiedAt"]

    def test_a_ledger_from_before_the_table_existed_reads_as_nothing_verified(self, cp):
        aid = _signup(cp)
        _users(cp.ledger, "ann")
        cp.ledger.conn.execute("DROP TABLE user_verification")
        cp.ledger.conn.commit()
        assert cp.get("/api/v2/one-to-one", params={"account_id": aid}).json()["users"][0]["verdict"] == "NOT_VERIFIED"

    def test_it_reports_whether_it_runs_on_its_own(self, cp):
        aid = _signup(cp)
        v = cp.get("/api/v2/one-to-one", params={"account_id": aid}).json()
        assert v["onComplete"] is True and v["perService"] == 25

    def test_another_accounts_page_is_not_readable(self, cp):
        aid = _signup(cp)
        assert cp.get("/api/v2/one-to-one", params={"account_id": aid + 5}).status_code == 403


class TestVerifyNow:
    def _capture(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(api_server, "_run_admitted",
                            lambda argv, aid, name, env=None: (seen.update(argv=argv, name=name, aid=aid) or (True, "started")))
        return seen

    def test_it_launches_the_checker_as_its_own_job_with_the_usual_sample(self, cp, monkeypatch):
        aid = _signup(cp)
        seen = self._capture(monkeypatch)
        r = cp.post("/api/v2/one-to-one/run", json={"reason": "check them", "account_id": aid})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert seen["name"] == "verify" and seen["argv"][1] == "verify_sample.py"
        assert seen["argv"][seen["argv"].index("--limit") + 1] == "25"
        assert ["--account-id", str(aid)] == seen["argv"][2:4]

    def test_it_can_be_pointed_at_chosen_users_and_a_chosen_sample(self, cp, monkeypatch):
        aid = _signup(cp)
        seen = self._capture(monkeypatch)
        cp.post("/api/v2/one-to-one/run", json={"reason": "check them", "account_id": aid, "limit": 7,
                                                  "users": ["ann@a.com", "bob@a.com"]})
        a = seen["argv"]
        assert a[a.index("--limit") + 1] == "7"
        assert [a[i + 1] for i, x in enumerate(a) if x == "--user"] == ["ann@a.com", "bob@a.com"]

    def test_zero_means_every_item_so_no_limit_is_passed(self, cp, monkeypatch):
        aid = _signup(cp)
        seen = self._capture(monkeypatch)
        cp.post("/api/v2/one-to-one/run", json={"reason": "all of it", "account_id": aid, "limit": 0})
        assert "--limit" not in seen["argv"]

    def test_it_needs_a_reason(self, cp, monkeypatch):
        aid = _signup(cp)
        self._capture(monkeypatch)
        assert cp.post("/api/v2/one-to-one/run", json={"account_id": aid}).status_code == 422

    def test_another_accounts_ledger_cannot_be_checked(self, cp, monkeypatch):
        aid = _signup(cp)
        self._capture(monkeypatch)
        r = cp.post("/api/v2/one-to-one/run", json={"reason": "not mine", "account_id": aid + 5})
        assert r.status_code == 403


class TestADifferenceIsNotACrash:
    def test_the_command_exits_zero_when_it_found_differences(self, monkeypatch):
        import auth as auth_mod
        import db as db_mod
        import verify_sample as V
        monkeypatch.setattr(auth_mod, "AuthManager", lambda settings: object())
        monkeypatch.setattr(db_mod, "MigrationDB", lambda path: object())
        monkeypatch.setattr(V, "run_and_save", lambda *a, **k: {"verdict": "DIFFERENCES"})
        assert V.main([]) == 0, "run_watch opens a 'crashed' incident for any non-zero exit"
