"""The Tally page reads what the per-user tally stored, and can ask for it again.

Distinct from the one-to-one check (test_one_to_one_api.py): this counts every item on
both sides, not a sample of 25, and never touches run_fidelity (the whole-tenant number
"Run tally" on the reports panel writes). Two rules matter more than the shape of the
JSON: a user nobody has tallied is NOT_TALLIED -- an empty row must never read as a pass;
and a per-user tally is its own job (`user-tally`), never sharing a slot or a log with the
whole-tenant `tally` job.
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


def _row(db, user, count_parity, **payload):
    db.save_user_tally(f"{user}@a.com", f"{user}@b.com", count_parity,
                       {"services": {"drive_files": {"source": 10, "target": 10, "parity": 1.0}}, **payload})


class TestTheView:
    def test_it_needs_a_login(self, cp):
        assert cp.get("/api/v2/tally").status_code in (401, 403)

    def test_a_user_nobody_tallied_is_not_tallied_rather_than_fine(self, cp):
        aid = _signup(cp)
        _users(cp.ledger, "ann", "bob")
        _row(cp.ledger, "ann", 1.0)
        v = cp.get("/api/v2/tally", params={"account_id": aid}).json()
        by = {u["user"]: u for u in v["users"]}
        assert by["ann@a.com"]["verdict"] == "COMPLETE"
        assert by["bob@a.com"]["verdict"] == "NOT_TALLIED" and by["bob@a.com"]["countParity"] is None
        assert v["totals"] == {"COMPLETE": 1, "DIFFERS": 0, "SHORT": 0, "OWED_TO_DMS": 0, "UNKNOWN": 0, "NOT_TALLIED": 1}

    def test_below_the_bar_is_short_not_complete(self, cp):
        aid = _signup(cp)
        _users(cp.ledger, "ann")
        _row(cp.ledger, "ann", 0.5)
        u = cp.get("/api/v2/tally", params={"account_id": aid}).json()["users"][0]
        assert u["verdict"] == "SHORT"

    def test_a_tally_that_measured_nothing_is_unknown_not_complete(self, cp):
        aid = _signup(cp)
        _users(cp.ledger, "ann")
        _row(cp.ledger, "ann", None)
        u = cp.get("/api/v2/tally", params={"account_id": aid}).json()["users"][0]
        assert u["verdict"] == "UNKNOWN"

    def test_the_service_breakdown_reaches_the_page(self, cp):
        aid = _signup(cp)
        _users(cp.ledger, "ann")
        _row(cp.ledger, "ann", 1.0, worst=[{"user": "ann@a.com", "service": "mail", "missing": 3}])
        u = cp.get("/api/v2/tally", params={"account_id": aid}).json()["users"][0]
        assert u["services"]["drive_files"]["parity"] == 1.0
        assert u["worst"] == [{"user": "ann@a.com", "service": "mail", "missing": 3}]

    def test_a_ledger_from_before_the_table_existed_reads_as_nothing_tallied(self, cp):
        aid = _signup(cp)
        _users(cp.ledger, "ann")
        cp.ledger.conn.execute("DROP TABLE user_tally")
        cp.ledger.conn.commit()
        assert cp.get("/api/v2/tally", params={"account_id": aid}).json()["users"][0]["verdict"] == "NOT_TALLIED"

    def test_it_reports_whether_it_runs_on_its_own(self, cp):
        aid = _signup(cp)
        v = cp.get("/api/v2/tally", params={"account_id": aid}).json()
        assert v["onComplete"] is False          # counted after the run, not per user

    def test_another_accounts_page_is_not_readable(self, cp):
        aid = _signup(cp)
        assert cp.get("/api/v2/tally", params={"account_id": aid + 5}).status_code == 403


class TestTallyNow:
    def _capture(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(api_server, "_run_admitted",
                            lambda argv, aid, name, env=None, **k: (seen.update(argv=argv, name=name, aid=aid, then=k.get("then")) or (True, "started")))
        return seen

    def test_it_launches_as_its_own_job_never_the_whole_tenant_ones(self, cp, monkeypatch):
        aid = _signup(cp)
        seen = self._capture(monkeypatch)
        r = cp.post("/api/v2/tally/run", json={"reason": "check them", "account_id": aid})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert seen["name"] == "user-tally", "must never share a slot or a log with the whole-tenant 'tally' job"
        assert seen["argv"][1] == "tally.py"
        assert ["--account-id", str(aid)] == seen["argv"][2:4]

    def test_it_can_be_pointed_at_chosen_users(self, cp, monkeypatch):
        aid = _signup(cp)
        seen = self._capture(monkeypatch)
        cp.post("/api/v2/tally/run", json={"reason": "check them", "account_id": aid,
                                            "users": ["ann@a.com", "bob@a.com"]})
        a = seen["argv"]
        assert [a[i + 1] for i, x in enumerate(a) if x == "--user"] == ["ann@a.com", "bob@a.com"]

    def test_it_needs_a_reason(self, cp, monkeypatch):
        aid = _signup(cp)
        self._capture(monkeypatch)
        assert cp.post("/api/v2/tally/run", json={"account_id": aid}).status_code == 422

    def test_another_accounts_ledger_cannot_be_tallied(self, cp, monkeypatch):
        aid = _signup(cp)
        self._capture(monkeypatch)
        r = cp.post("/api/v2/tally/run", json={"reason": "not mine", "account_id": aid + 5})
        assert r.status_code == 403


class TestTheCliExitsCleanEvenWhenSomethingComesUpShort:
    def test_it_never_reads_as_a_crash(self, monkeypatch):
        """run_watch opens a 'crashed' incident for any non-zero job exit -- a user this
        could not tally is a finding (NOT_TALLIED/UNKNOWN on the page), not a crash. Real
        tally_user_and_save, which itself never raises (see TestNeverRaises below)."""
        import auth as auth_mod
        import db as db_mod
        import config
        import tally as T
        monkeypatch.setattr(auth_mod, "AuthManager", lambda settings: object())

        class _Settings:
            db_path = "unused"
            max_retries = 3
        monkeypatch.setattr(config, "Settings", lambda account_id=None: _Settings())

        class _DB:
            def all_identities(self):
                return [{"source_email": "ann@a.com", "target_email": "ann@b.com", "entity_type": "user"}]

            @property
            def conn(self):
                raise RuntimeError("no ledger to read skips from")
        monkeypatch.setattr(db_mod, "MigrationDB", lambda path: _DB())
        assert T.main(["--account-id", "1"]) == 0


class TestNeverRaises:
    def test_a_failure_counting_either_side_is_recorded_not_raised(self, monkeypatch):
        import tally as T

        class _DB:
            def save_user_tally(self, *a, **k):
                raise AssertionError("must not be called: nothing was measured")

            @property
            def conn(self):
                raise RuntimeError("boom")
        assert T.tally_user_and_save(object(), _DB(), object(), "ann@a.com", "ann@b.com") is None


class TestMailOwedToTheDmsIsNotShort:
    """278 users waiting on the DMS and 22 whose mail never ran all read "Short",
    so the 22 hid in the 300."""

    def _mail(self, ledger, user, target, deferred):
        ledger.save_user_tally(f"{user}@a.com", f"{user}@b.com", target / 100, {"services": {
            "drive_files": {"source": 10, "target": 10, "expected": 10, "parity": 1.0},
            "mail": {"source": 100, "target": target, "expected": 100, "parity": target / 100}}})
        for i in range(deferred):
            ledger.log_audit(f"{user}@a.com", f"m{i}", "message", "SKIPPED_NO_DRIVE_LINK")

    def test_only_owed_mail_missing_is_owed_to_dms(self, cp):
        aid = _signup(cp)
        _users(cp.ledger, "ann", "bob")
        self._mail(cp.ledger, "ann", 20, deferred=80)     # every missing message is owed
        self._mail(cp.ledger, "bob", 20, deferred=0)      # the 22-user shape: mail never ran
        v = cp.get("/api/v2/tally", params={"account_id": aid}).json()
        by = {u["user"]: u["verdict"] for u in v["users"]}
        assert by == {"ann@a.com": "OWED_TO_DMS", "bob@a.com": "SHORT"}
        assert v["totals"]["OWED_TO_DMS"] == 1 and v["totals"]["SHORT"] == 1

    def test_more_missing_than_owed_is_still_short(self, cp):
        aid = _signup(cp)
        _users(cp.ledger, "ann")
        self._mail(cp.ledger, "ann", 20, deferred=50)     # 30 missing beyond what the DMS owes
        assert cp.get("/api/v2/tally", params={"account_id": aid}).json()["users"][0]["verdict"] == "SHORT"
