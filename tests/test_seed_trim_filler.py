"""Trimming filler is a DELETE launched from the same screen as a fill, so the
properties worth pinning are the guards: it previews unless told otherwise, it
takes the same typed-domain and sandbox gates a seed does, it can only ever be
the narrow --trim-filler command, and it is reachable without restarting the
process that owns a running fill."""
import os
import tempfile

import pytest

import webui

pytest.importorskip("httpx2", reason="starlette TestClient needs httpx2")
from fastapi.testclient import TestClient  # noqa: E402

import control_plane_db as cpdb  # noqa: E402
from db import MigrationDB  # noqa: E402


@pytest.fixture
def _cfg(monkeypatch):
    import config
    real = config.Settings

    def fake(account_id=None, **kw):
        st = real.__new__(real)
        st.source_domain, st.target_domain = "src.example", "tgt.example"
        st.source_admin, st.source_sa_key = "a@src.example", "/k/s.json"
        st.target_admin, st.target_sa_key = "a@tgt.example", "/k/t.json"
        st.db_path, st.account_id = "/tmp/m.db", account_id
        return st

    monkeypatch.setattr(config, "Settings", fake)
    monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)


def _argv(**body):
    return webui.seed_argv({"confirm_domain": "src.example", "trim_filler": True, **body}, 7)


class TestTheCommand:
    def test_it_previews_unless_told_to_apply(self, _cfg):
        argv, _, err = _argv()
        assert not err and "--trim-filler" in argv and "--trim-apply" not in argv

    def test_apply_adds_the_delete_flag(self, _cfg):
        argv, _, _ = _argv(trim_apply=True)
        assert "--trim-apply" in argv

    def test_it_is_only_ever_the_narrow_command(self, _cfg):
        """No scale, no create-users, no reset, no fill: nothing that seeds or
        deletes anything but filler can ride along."""
        argv, _, _ = _argv(trim_apply=True, reset=True, create_users=True, fill_until_full=True, scale="huge")
        for banned in ("--reset", "--create-users", "--fill-until-full", "--top-up-only", "--scale"):
            assert banned not in argv

    def test_it_defaults_to_every_account_and_can_be_narrowed(self, _cfg):
        assert "--all-users" in _argv()[0]
        argv, _, _ = _argv(users="george, ivan")
        assert argv[argv.index("--users") + 1] == "george, ivan" and "--all-users" not in argv

    def test_users_must_be_localparts(self, _cfg):
        _, _, err = _argv(users="george@src.example")
        assert "localparts" in err

    def test_it_runs_with_few_workers_because_it_runs_beside_a_fill(self, _cfg):
        argv, _, _ = _argv()
        assert argv[argv.index("--workers") + 1] == "4"

    def test_the_child_is_told_it_is_a_sandbox_and_pointed_at_the_account(self, _cfg):
        _, env, _ = _argv()
        assert env["SANDBOX_MODE"] == "true" and env["SOURCE_DOMAIN"] == "src.example"


class TestTheGates:
    def test_the_domain_must_be_typed(self, _cfg):
        assert "type the source domain" in _argv(confirm_domain="")[2]

    def test_the_target_domain_is_refused(self, _cfg):
        _, _, err = _argv(confirm_domain="tgt.example")
        assert "TARGET" in err

    def test_a_protected_domain_is_refused_in_words_about_deleting_not_seeding(self, _cfg, monkeypatch):
        import domain_guard
        monkeypatch.setattr(domain_guard, "is_protected", lambda d: True)
        _, _, err = _argv()
        assert "Deleting filler from" in err and "refused" in err


@pytest.fixture
def cp(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    import api_server
    with TestClient(api_server.app) as client:
        yield client
    try:
        os.unlink(path)
    except OSError:
        pass


def _signup(client, email="a@example.com"):
    r = client.post("/api/v2/auth/signup", json={"email": email, "password": "hunter22222", "name": "Tester"})
    assert r.status_code == 200, r.text
    return client.get("/api/v2/auth/me").json()["id"]


@pytest.fixture
def stub_argv(monkeypatch):
    """The real argv builder is covered above; here only the plumbing is.
    (Patching config.Settings would also redirect the session database.)"""
    def fake(body, account_id=None):
        if body["confirm_domain"] != "src.example":
            return [], {}, "does not match the source domain"
        return (["python", "seed_sandbox.py", "--trim-filler"] + (["--trim-apply"] if body["trim_apply"] else []),
                {}, "")
    monkeypatch.setattr(webui, "seed_argv", fake)


class TestTheEndpoint:
    def test_it_needs_a_login(self, cp):
        r = cp.post("/api/v2/seed/trim-filler", json={"reason": "free the pool", "confirm_domain": "x"})
        assert r.status_code in (401, 403)

    def test_it_launches_the_preview_from_the_seeders_own_directory(self, cp, monkeypatch, stub_argv):
        import api_server
        aid = _signup(cp)
        monkeypatch.setattr(webui, "_seed_ok", lambda a: True)
        seen = {}
        monkeypatch.setattr(api_server, "_run_admitted",
                            lambda argv, account, name, env=None: seen.update(argv=argv, account=account, name=name)
                            or (True, "started"))
        r = cp.post("/api/v2/seed/trim-filler", json={"reason": "free the pool", "confirm_domain": "src.example"})
        assert r.status_code == 200 and r.json()["ok"] is True, r.text
        assert seen["account"] == aid and seen["name"] == "trim-filler"
        assert seen["argv"][1].endswith(os.path.join("data-generator", "seed_sandbox.py"))
        assert os.path.isabs(seen["argv"][1])
        assert "--trim-filler" in seen["argv"] and "--trim-apply" not in seen["argv"]

    def test_apply_is_a_separate_logged_action_from_the_preview(self, cp, monkeypatch, stub_argv):
        import api_server
        _signup(cp)
        monkeypatch.setattr(webui, "_seed_ok", lambda a: True)
        monkeypatch.setattr(api_server, "_run_admitted", lambda *a, **k: (True, "started"))
        cp.post("/api/v2/seed/trim-filler", json={"reason": "free the pool", "confirm_domain": "src.example"})
        cp.post("/api/v2/seed/trim-filler", json={"reason": "free the pool", "confirm_domain": "src.example",
                                                  "apply": True})
        with cpdb.ro() as c:
            acts = [r[0] for r in c.execute("SELECT action FROM operator_actions_log ORDER BY id")]
        assert "seed.trim_filler.preview" in acts and "seed.trim_filler.apply" in acts

    def test_a_wrong_domain_is_refused_and_launches_nothing(self, cp, monkeypatch, stub_argv):
        import api_server
        _signup(cp)
        monkeypatch.setattr(webui, "_seed_ok", lambda a: True)
        launched = []
        monkeypatch.setattr(api_server, "_run_admitted", lambda *a, **k: launched.append(a) or (True, "x"))
        r = cp.post("/api/v2/seed/trim-filler", json={"reason": "free the pool", "confirm_domain": "elsewhere.com"})
        assert r.json()["ok"] is False and launched == []

    def test_an_account_that_may_not_seed_may_not_trim(self, cp, monkeypatch, stub_argv):
        import api_server
        _signup(cp)
        monkeypatch.setattr(webui, "_seed_ok", lambda a: False)
        launched = []
        monkeypatch.setattr(api_server, "_run_admitted", lambda *a, **k: launched.append(a) or (True, "x"))
        r = cp.post("/api/v2/seed/trim-filler", json={"reason": "free the pool", "confirm_domain": "src.example"})
        assert r.json()["ok"] is False and "not enabled" in r.json()["detail"] and launched == []


PREVIEW = """\
Trimming filler back to each account's own storage share for 3 user(s) [PREVIEW -- nothing is deleted] ...
  [george@src.example] 233.1GB vs share 30.0GB: would delete 4,062 filler file(s) (203.1GB)
  [1/3] george@src.example done
  [ivan@src.example] 29.9GB vs share 30.0GB: would delete 0 filler file(s) (0.0GB) -- at or under its share
  [2/3] ivan@src.example done
  [3/3] mia@src.example done

1 account(s) over their share. Would delete 4,062 filler file(s), 203.1 GB.
Preview only -- nothing was deleted.
"""
APPLY = PREVIEW.replace("[PREVIEW -- nothing is deleted]", "[DELETING]").replace("would delete", "deleted") \
    .replace("Would delete", "Deleted").replace("Preview only -- nothing was deleted.\n", "")


class TestStatus:
    def _status(self, monkeypatch, text, live=False):
        import api_server
        monkeypatch.setattr(api_server, "_job_log_lines", lambda *a, **k: text.splitlines())
        monkeypatch.setattr(api_server.job_admission, "list_active",
                            lambda: [{"job_name": "trim-filler", "account_id": 7, "pid": 1}] if live else [])
        monkeypatch.setattr(api_server.job_admission, "is_live", lambda j: True)
        return api_server._trim_status(7)

    def test_a_preview_reads_as_a_preview_with_its_real_progress(self, monkeypatch):
        st = self._status(monkeypatch, PREVIEW)
        assert st["mode"] == "preview" and (st["done"], st["total"]) == (3, 3)
        assert "Would delete 4,062" in st["summary"] and st["running"] is False

    def test_only_accounts_with_something_to_remove_are_listed(self, monkeypatch):
        affected = self._status(monkeypatch, PREVIEW)["affected"]
        assert len(affected) == 1 and "george@src.example" in affected[0]

    def test_the_latest_run_wins_when_the_log_holds_several(self, monkeypatch):
        st = self._status(monkeypatch, PREVIEW + APPLY)
        assert st["mode"] == "apply" and st["summary"].startswith("1 account(s) over their share. Deleted")

    def test_no_run_says_so_instead_of_inventing_one(self, monkeypatch):
        st = self._status(monkeypatch, "")
        assert st["hasRun"] is False and st["mode"] is None and st["summary"] is None

    def test_running_comes_from_the_admission_table(self, monkeypatch):
        assert self._status(monkeypatch, PREVIEW, live=True)["running"] is True
