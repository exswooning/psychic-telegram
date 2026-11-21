"""fableplan2 P1.2: the box scripts and their units serve any account, not account 3."""
import os
import re

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = ("dms_delta_loop.py", "dms_user_verify.py", "autopilot.py", "parity_loop.py")


@pytest.mark.parametrize("name", SCRIPTS)
def test_no_account_or_install_path_is_written_into_the_script(name):
    src = open(os.path.join(HERE, name), encoding="utf-8").read()
    assert not re.search(r"accounts/3\b|account_id=3\b|jobs/3\b|dms/3\b|_env\(3\)|verify_dir\(3\)|[\"']/opt/bitport", src)


def test_the_delta_loop_paths_follow_its_account_and_it_needs_one():
    import dms_delta_loop as D
    try:
        D.configure(7)
        assert D.LOG.endswith("logs/jobs/7/dms_delta_loop.log")
        assert D.LOCK.endswith("logs/jobs/7/dms-delta-scheduler.lock")
        assert D.CSV.endswith("logs/dms/7/identities.csv")
        D.configure(7, "/x/ids.csv")
        assert D.CSV == "/x/ids.csv"
        D.configure(0)
        with pytest.raises(SystemExit):
            D.main([])
    finally:
        D.configure(0)


def test_the_user_verify_worker_is_handed_its_account(tmp_path, monkeypatch):
    import dms_user_verify as V
    seen = []
    monkeypatch.setattr(V.subprocess, "run", lambda argv, **kw: seen.append(argv) or type("R", (), {"returncode": 0})())
    try:
        V.configure(7)
        monkeypatch.setattr(V, "LOG", str(tmp_path / "log"))
        assert V.dispatch("a@x.test") == 0
        assert "--account-id=7" in seen[0] and "--verify-user=a@x.test" in seen[0]
        V.configure(0)
        with pytest.raises(SystemExit):
            V.main([])
    finally:
        V.configure(0)


@pytest.mark.parametrize("unit,script", [("bitport-autopilot@.service", "autopilot.py"),
                                         ("bitport-parity@.service", "parity_loop.py")])
def test_a_unit_template_runs_its_script_for_the_instance(unit, script):
    text = open(os.path.join(HERE, "systemd", unit), encoding="utf-8").read()
    exec_line = next(l for l in text.splitlines() if l.startswith("ExecStart="))
    assert script in exec_line and exec_line.endswith("--account-id %i")
    assert "KillMode=process" in text            # never kill the jobs a restart would orphan


# ---- P1.3: the coverage audit takes an account (the API launched it with none, so it read the legacy ledger)

def test_the_coverage_audit_reads_the_named_accounts_ledger(monkeypatch):
    import coverage_audit as cov
    seen = {}

    class S:
        db_path = ":memory:"

    class DB:
        def all_identities(self):
            return [{"entity_type": "user", "source_email": "a@x.test"}]

    monkeypatch.setattr(cov, "Settings", lambda account_id=None: seen.update(account=account_id) or S())
    monkeypatch.setattr(cov, "AuthManager", lambda s: None)
    monkeypatch.setattr(cov, "MigrationDB", lambda path: DB())
    monkeypatch.setattr(cov, "collect", lambda *a: {})
    monkeypatch.setattr(cov, "assess", lambda totals: [])
    monkeypatch.setattr(cov, "render", lambda rows, totals: "ok")
    assert cov.main(["--account-id", "7"]) == 0
    assert seen["account"] == 7


def test_the_coverage_launch_runs_for_the_callers_account(monkeypatch, tmp_path):
    import tempfile

    from fastapi.testclient import TestClient

    import accounts_auth
    import control_plane_db as cpdb
    from db import MigrationDB
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    import api_server
    monkeypatch.setattr(api_server, "HERE", str(tmp_path))
    launched = []
    real = api_server.subprocess.Popen

    def popen(argv, **kw):
        if "coverage_audit.py" in argv:
            launched.append(argv)
            return type("P", (), {"pid": 1})()
        return real(argv, **kw)
    monkeypatch.setattr(api_server.subprocess, "Popen", popen)
    try:
        with TestClient(api_server.app) as client:
            aid = accounts_auth.create_account("cov@one.example", "a-long-password", "Someone")
            client.cookies.set("bp_session", accounts_auth.create_session(aid))
            r = client.post("/api/v2/coverage/start", json={"reason": "check"})
            assert r.json().get("ok") is True, r.text
            assert launched[0][-2:] == ["--account-id", str(aid)]
    finally:
        os.unlink(path)
