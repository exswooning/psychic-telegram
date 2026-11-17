"""fableplan2 P1.5: Start is refused, with the reason, before anything launches."""
import pytest

import preflight as P


def ok(rows):
    return {r["name"]: r["ok"] for r in rows}


class TestOneCheckEach:
    def test_no_route_to_google_refuses(self):
        def refuse(*a, **k):
            raise OSError("Network is unreachable")
        r = P.check_network(refuse)
        assert r["ok"] is False and "oauth2.googleapis.com" in r["detail"]

    def test_a_route_to_google_passes(self):
        assert P.check_network(lambda *a, **k: type("S", (), {"close": lambda s: None})())["ok"] is True

    def test_a_full_disk_refuses_and_names_the_free_space(self, tmp_path):
        r = P.check_disk(str(tmp_path), disk=lambda p: {"total": 100e9, "used": 95e9, "free": 5e9, "pct": 95})
        assert r["ok"] is False and "95% full" in r["detail"] and "5 GB free" in r["detail"]
        assert P.check_disk(str(tmp_path), disk=lambda p: {"total": 100e9, "used": 40e9, "free": 60e9, "pct": 40})["ok"] is True

    def test_an_unreadable_disk_is_unknown_not_a_pass_or_a_refusal(self, tmp_path):
        def boom(p):
            raise OSError("no")
        assert P.check_disk(str(tmp_path), disk=boom)["ok"] is None

    def test_a_pending_reboot_or_a_running_upgrade_refuses(self):
        assert P.check_upgrade(exists=lambda p: True)["ok"] is False
        assert P.check_upgrade(exists=lambda p: False, running=lambda: True)["ok"] is False
        assert P.check_upgrade(exists=lambda p: False, running=lambda: False)["ok"] is True


class TestTheDmsLogin:
    def _file(self, tmp_path, text):
        f = tmp_path / "dwd.env"
        f.write_text(text)
        return str(f)

    def test_a_file_that_serves_another_pair_refuses_and_says_which(self, tmp_path):
        f = self._file(tmp_path, "DWD_EMAIL_SOURCE=admin@s.example\nDWD_PASSWORD_SOURCE=x\nDWD_PASSWORD_TARGET=y\n")
        r = P.check_dms_login("a.example", "b.example", f)
        assert r["ok"] is False and "another pair" in r["detail"] and "s.example" in r["detail"]
        assert "x" not in r["detail"].replace("example", "")        # never a value in a row

    def test_no_file_or_no_target_password_refuses(self, tmp_path):
        assert P.check_dms_login("a.example", "b.example", str(tmp_path / "none"))["ok"] is False
        f = self._file(tmp_path, "DWD_EMAIL_SOURCE=admin@a.example\nDWD_PASSWORD_SOURCE=x\n")
        assert "no target admin password" in P.check_dms_login("a.example", "b.example", f)["detail"]

    def test_the_pair_s_own_logins_pass_and_say_whether_the_approval_needs_a_click(self, tmp_path):
        both = self._file(tmp_path, "DWD_EMAIL_SOURCE=admin@a.example\nDWD_PASSWORD_SOURCE=x\nDWD_PASSWORD_TARGET=y\n")
        r = P.check_dms_login("a.example", "b.example", both)
        assert r["ok"] is True and "no click" in r["detail"]
        only_target = self._file(tmp_path, "DWD_PASSWORD_TARGET=y\n")
        assert "waits for a click" in P.check_dms_login("a.example", "b.example", only_target)["detail"]


class TestTheWholeAsk:
    class S:
        scratch_dir, source_domain, target_domain = "", "a.example", "b.example"

    def _env(self, monkeypatch):
        monkeypatch.setenv("BITPORT_PREFLIGHT", "1")
        monkeypatch.setattr(P, "check_network", lambda: P._row("network", True, "up"))
        monkeypatch.setattr(P, "check_disk", lambda path: P._row("disk", True, "40% used"))
        monkeypatch.setattr(P, "check_upgrade", lambda: P._row("upgrade", True, "none"))

    def test_the_tests_switch_it_off(self, monkeypatch):
        monkeypatch.setenv("BITPORT_PREFLIGHT", "0")
        assert P.check(self.S()) == []

    def test_every_refusal_is_named_in_one_sentence(self, monkeypatch):
        self._env(monkeypatch)
        monkeypatch.setattr(P, "check_disk", lambda path: P._row("disk", False, "95% full"))
        rows = P.check(self.S(), ready=lambda s: [P._row("target ready", False, "Drive: 401")])
        why = P.refusal(rows)
        assert "disk: 95% full" in why and "target ready: Drive: 401" in why

    def test_a_check_nobody_could_make_does_not_refuse(self, monkeypatch):
        self._env(monkeypatch)
        rows = P.check(self.S(), ready=lambda s: [P._row("source ready", None, "could not ask Google")])
        assert P.refusal(rows) is None

    def test_the_dms_login_is_asked_only_when_the_dms_is_used(self, monkeypatch):
        self._env(monkeypatch)
        monkeypatch.setattr(P, "check_dms_login", lambda s, t: P._row("dms login", False, "no file"))
        assert "dms login" not in ok(P.check(self.S(), ready=lambda s: []))
        assert ok(P.check(self.S(), needs_dms=True, ready=lambda s: []))["dms login"] is False

    def test_readiness_is_not_asked_without_a_network(self, monkeypatch):
        self._env(monkeypatch)
        monkeypatch.setattr(P, "check_network", lambda: P._row("network", False, "unreachable"))
        asked = []
        P.check(self.S(), ready=lambda s: asked.append(1) or [])
        assert asked == []


def test_the_start_endpoint_answers_409_with_the_reason(monkeypatch):
    import tempfile

    from fastapi.testclient import TestClient

    import accounts_auth
    import api_server
    import control_plane_db as cpdb
    from db import MigrationDB
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    monkeypatch.setattr(P, "check", lambda *a, **k: [P._row("disk", False, "the disk is 95% full")])
    import tenant_capacity
    monkeypatch.setattr(tenant_capacity, "read", lambda *a, **k: {})
    monkeypatch.setattr(tenant_capacity, "refusal", lambda *a, **k: None)
    with TestClient(api_server.app) as client:
        aid = accounts_auth.create_account("pre@one.example", "a-long-password", "Someone")
        client.cookies.set("bp_session", accounts_auth.create_session(aid))
        r = client.post("/api/v2/migrate/start", json={"reason": "go now", "services": ["drive"]})
    assert r.status_code == 409 and "Not started: disk: the disk is 95% full" in r.text


def test_the_upgrade_check_does_not_match_the_shutdown_helper_that_always_runs():
    import inspect
    src = inspect.getsource(P.check_upgrade)
    assert "-x" not in src and "unattended-upgrade( |$)" in src
    import re
    pat = re.compile(r"/usr/bin/unattended-upgrade( |$)")
    assert pat.search("/usr/bin/unattended-upgrade --install-deps")
    assert not pat.search("/usr/bin/python3 /usr/share/unattended-upgrades/unattended-upgrade-shutdown --wait-for-signal")
