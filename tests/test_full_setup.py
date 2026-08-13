"""
tests/test_full_setup.py
=========================
The orchestrator that turns "project, APIs, key, delegation, verified" into
one call. Two properties matter more than the happy path:

  * the password must never survive past the one call that needs it -- not
    in a log line, not left sitting in os.environ for a later step;
  * a side="target" call must provision the TARGET project and read the
    TARGET key. provision_gcp.provision() always builds both sides at once
    and full_setup used to always look up "source" in its result regardless
    of which side was asked for -- a target call would have silently read
    the source project's client ID.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import full_setup as fs  # noqa: E402


class _Phase:
    """Mirrors provision_gcp.Step / full_setup.Phase closely enough for
    monkeypatched provision_side() to return a shape run_full_setup reads."""
    def __init__(self, status="ok", detail=""):
        self.status, self.detail = status, detail


def _fake_provision_side(ok=True, client_id="999", detail=""):
    def fn(side, project, org, key_dest, dry_run, force):
        return {"side": side, "project": project, "ok": ok,
                "steps": [{"name": "x", "status": "ok" if ok else "failed",
                          "detail": detail}],
                "clientId": client_id}
    return fn


class TestSkipsProvisioningWhenKeyAlreadyExists:
    """Cloud project creation needs an identity with org-level rights this
    process never holds (see the comment above phase 1 in full_setup.py) --
    when an account already has a real key file uploaded for a side,
    phase 1 must skip past gcloud entirely rather than trying and failing
    with 'gcloud is not installed' on every single run."""

    def _common(self, monkeypatch):
        monkeypatch.setattr(fs.dwd_helper, "run", lambda *a, **k: 0)
        monkeypatch.setattr(fs.verify_scopes, "required_scopes",
                            lambda settings, tenant: ["scope-a"])
        monkeypatch.setattr(fs.verify_scopes, "verify",
                            lambda settings, tenant, scopes: [
                                {"scope": s, "ok": True} for s in scopes])
        monkeypatch.setattr(fs.accounts_auth, "update_tenant_config", lambda *a, **k: None)

    def test_provision_side_is_never_called_when_a_key_exists(self, monkeypatch, tmp_path):
        key_path = tmp_path / "source-sa.json"
        key_path.write_text('{"client_id": "existing-client-id"}')
        self._common(monkeypatch)

        called = {"gcloud_ready": False, "provision_side": False}
        monkeypatch.setattr(fs.accounts_auth, "get_tenant_config",
                            lambda account_id, side: {"sa_key_path": str(key_path)})
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready",
                            lambda: called.update(gcloud_ready=True) or (True, "me"))
        monkeypatch.setattr(fs.provision_gcp, "provision_side",
                            lambda *a, **k: called.update(provision_side=True) or {})

        res = fs.run_full_setup("source", "c.example.com", "admin@c.example.com",
                                "pw", account_id=7)

        assert called == {"gcloud_ready": False, "provision_side": False}
        assert res["ok"] is True
        assert res["clientId"] == "existing-client-id"
        phase1 = res["phases"][0]
        assert phase1["status"] == "skipped"

    def test_falls_back_to_provisioning_when_no_key_file_exists_yet(self, monkeypatch, tmp_path):
        """tenant_configs.sa_key_path is reserved at account creation time,
        before any key is ever uploaded (accounts_auth.create_account sets
        it immediately) -- the FILE existing, not just the path being
        non-null, is what has to gate the skip."""
        missing_path = tmp_path / "not-uploaded-yet.json"
        monkeypatch.setattr(fs.accounts_auth, "get_tenant_config",
                            lambda account_id, side: {"sa_key_path": str(missing_path)})
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready",
                            lambda: (False, "gcloud is not installed"))

        res = fs.run_full_setup("source", "c.example.com", "admin@c.example.com",
                                "pw", account_id=7)
        assert res["ok"] is False
        assert "gcloud is not installed" in res["phases"][0]["detail"]

    def test_a_key_file_with_no_client_id_fails_clearly(self, monkeypatch, tmp_path):
        key_path = tmp_path / "source-sa.json"
        key_path.write_text("{}")  # valid JSON, but no client_id in it
        monkeypatch.setattr(fs.accounts_auth, "get_tenant_config",
                            lambda account_id, side: {"sa_key_path": str(key_path)})

        res = fs.run_full_setup("source", "c.example.com", "admin@c.example.com",
                                "pw", account_id=7)
        assert res["ok"] is False
        assert "no client_id" in res["phases"][0]["detail"]

    def test_the_legacy_local_gcloud_caller_is_completely_unaffected(self, monkeypatch):
        """account_id=None (the default) must never even look at
        tenant_configs -- confirmed by get_tenant_config never being
        called, not just by behaviour looking the same."""
        called = {"get_tenant_config": False}
        monkeypatch.setattr(fs.accounts_auth, "get_tenant_config",
                            lambda *a, **k: called.update(get_tenant_config=True) or None)
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready",
                            lambda: (False, "gcloud is not installed"))

        fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "pw")
        assert called["get_tenant_config"] is False


class TestSideSelectionIsCorrect:
    """The bug that would have silently used the wrong tenant's project."""

    def test_target_call_provisions_a_target_named_project(self, monkeypatch):
        seen = {}

        def fake_provision_side(side, project, org, key_dest, dry_run, force):
            seen["side"] = side
            seen["project"] = project
            seen["key_dest"] = key_dest
            return {"side": side, "project": project, "ok": True, "steps": []}

        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (True, "me"))
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda: "")
        monkeypatch.setattr(fs.provision_gcp, "provision_side", fake_provision_side)
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "42")

        fs.run_full_setup("target", "a.example.com", "admin@a.example.com",
                          "pw", dry_run=True)

        assert seen["side"] == "target"
        assert "tgt" in seen["project"]
        assert "target-sa.json" in seen["key_dest"]

    def test_source_call_never_touches_the_target_project_name(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (True, "me"))
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda: "")
        monkeypatch.setattr(fs.provision_gcp, "provision_side",
                            lambda side, project, *a, **k: (
                                seen.setdefault("project", project),
                                {"side": side, "project": project, "ok": True,
                                 "steps": []})[1])
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "42")

        fs.run_full_setup("source", "c.example.com", "admin@c.example.com",
                          "pw", dry_run=True)
        assert "src" in seen["project"]
        assert "tgt" not in seen["project"]


class TestPasswordNeverLeaks:
    def test_password_does_not_survive_in_the_environment(self, monkeypatch):
        """The one call that needs the password is dwd_helper.run(); once it
        returns, DWD_PASSWORD must be restored to whatever it was before --
        never left holding this call's secret for a later, unrelated step."""
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (True, "me"))
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda: "")
        monkeypatch.setattr(fs.provision_gcp, "provision_side",
                            _fake_provision_side())
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "42")

        captured = {}

        def fake_dwd_run(client_id, scopes, timeout, headful, tenant=None):
            captured["password_during_call"] = os.environ.get("DWD_PASSWORD")
            return 0

        monkeypatch.setattr(fs.dwd_helper, "run", fake_dwd_run)
        monkeypatch.setattr(fs.verify_scopes, "verify",
                            lambda settings, tenant, scopes: [
                                {"scope": s, "ok": True} for s in scopes])
        monkeypatch.setattr(fs.verify_scopes, "required_scopes",
                            lambda settings, tenant: ["scope-a"])

        monkeypatch.delenv("DWD_PASSWORD", raising=False)
        fs.run_full_setup("source", "c.example.com", "admin@c.example.com",
                          "super-secret-pw")

        assert captured["password_during_call"] == "super-secret-pw"
        assert "DWD_PASSWORD" not in os.environ, \
            "password leaked into the environment past its one call"

    def test_a_pre_existing_dwd_password_env_var_is_restored(self, monkeypatch):
        """If the caller's shell already had DWD_PASSWORD set for some other
        reason, this must not clobber it permanently."""
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (True, "me"))
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda: "")
        monkeypatch.setattr(fs.provision_gcp, "provision_side",
                            _fake_provision_side())
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "42")
        monkeypatch.setattr(fs.dwd_helper, "run", lambda *a, **k: 0)
        monkeypatch.setattr(fs.verify_scopes, "verify",
                            lambda settings, tenant, scopes: [
                                {"scope": s, "ok": True} for s in scopes])
        monkeypatch.setattr(fs.verify_scopes, "required_scopes",
                            lambda settings, tenant: ["scope-a"])

        monkeypatch.setenv("DWD_PASSWORD", "unrelated-preexisting-value")
        fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "pw")
        assert os.environ["DWD_PASSWORD"] == "unrelated-preexisting-value"


class TestEnvShIsUpdated:
    """After a successful setup, the rest of the tool (webui.py's /api/seed,
    main.py, the Setup Wizard's own status page) must be pointed at the
    tenant that was just built -- otherwise a "Seed now" button that looks
    right ends up acting on whatever tenant env.sh happened to already
    name."""

    def _ok_common(self, monkeypatch):
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (True, "me"))
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda: "")
        monkeypatch.setattr(fs.provision_gcp, "provision_side", _fake_provision_side())
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "42")
        monkeypatch.setattr(fs.dwd_helper, "run", lambda *a, **k: 0)
        monkeypatch.setattr(fs.verify_scopes, "required_scopes",
                            lambda settings, tenant: ["scope-a"])
        monkeypatch.setattr(fs.verify_scopes, "verify",
                            lambda settings, tenant, scopes: [
                                {"scope": s, "ok": True} for s in scopes])

    def test_source_success_writes_source_keys(self, monkeypatch):
        self._ok_common(monkeypatch)
        written = {}
        monkeypatch.setattr("webui.write_config_raw", lambda pairs: written.update(pairs))

        res = fs.run_full_setup("source", "c.example.com",
                                "admin@c.example.com", "pw")
        assert res["ok"] is True
        assert written["SOURCE_DOMAIN"] == "c.example.com"
        assert written["SOURCE_ADMIN"] == "admin@c.example.com"
        assert "source-sa.json" in written["SOURCE_SA_KEY"]
        assert "TARGET_DOMAIN" not in written

    def test_target_success_writes_target_keys(self, monkeypatch):
        self._ok_common(monkeypatch)
        written = {}
        monkeypatch.setattr("webui.write_config_raw", lambda pairs: written.update(pairs))

        fs.run_full_setup("target", "a.example.com", "admin@a.example.com", "pw")
        assert written["TARGET_DOMAIN"] == "a.example.com"
        assert written["TARGET_ADMIN"] == "admin@a.example.com"
        assert "SOURCE_DOMAIN" not in written

    def test_dry_run_never_writes_env_sh(self, monkeypatch):
        self._ok_common(monkeypatch)
        called = {"n": 0}
        monkeypatch.setattr("webui.write_config_raw", lambda pairs: called.__setitem__("n", called["n"] + 1))

        fs.run_full_setup("source", "c.example.com", "admin@c.example.com",
                          "pw", dry_run=True)
        assert called["n"] == 0

    def test_write_failure_is_reported_but_does_not_flip_overall_ok(self, monkeypatch):
        """The tenant itself is already fully set up by this point -- a
        broken env.sh write is a real problem worth surfacing, but it
        shouldn't be reported as though provisioning or delegation failed."""
        self._ok_common(monkeypatch)

        def boom(pairs):
            raise OSError("disk full")

        monkeypatch.setattr("webui.write_config_raw", boom)
        res = fs.run_full_setup("source", "c.example.com",
                                "admin@c.example.com", "pw")
        env_phase = next(p for p in res["phases"] if "env.sh" in p["name"])
        assert env_phase["status"] == "failed"
        assert "SOURCE_DOMAIN=c.example.com" in env_phase["detail"]


class TestFailureModes:
    def test_provisioning_failure_stops_before_dwd(self, monkeypatch):
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (True, "me"))
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda: "")
        monkeypatch.setattr(fs.provision_gcp, "provision_side",
                            _fake_provision_side(ok=False, detail="quota exceeded"))

        called = {"dwd": False}
        monkeypatch.setattr(fs.dwd_helper, "run",
                            lambda *a, **k: called.update(dwd=True) or 0)

        res = fs.run_full_setup("source", "c.example.com",
                                "admin@c.example.com", "pw")
        assert res["ok"] is False
        assert called["dwd"] is False, "DWD ran despite a failed provisioning step"

    def test_no_gcloud_fails_before_any_write(self, monkeypatch):
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready",
                            lambda: (False, "gcloud is not installed"))
        res = fs.run_full_setup("source", "c.example.com",
                                "admin@c.example.com", "pw")
        assert res["ok"] is False
        assert "gcloud" in res["phases"][0]["detail"]

    def test_missing_scopes_after_grant_is_reported_not_assumed_ok(self, monkeypatch):
        """dwd_helper can exit 0 (submitted) while a scope still fails to
        verify -- propagation lag, or a partial grant. That must surface,
        not be swallowed into a plain success."""
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (True, "me"))
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda: "")
        monkeypatch.setattr(fs.provision_gcp, "provision_side",
                            _fake_provision_side())
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "42")
        monkeypatch.setattr(fs.dwd_helper, "run", lambda *a, **k: 0)
        monkeypatch.setattr(fs.verify_scopes, "required_scopes",
                            lambda settings, tenant: ["scope-a", "scope-b"])
        monkeypatch.setattr(fs.verify_scopes, "verify",
                            lambda settings, tenant, scopes: [
                                {"scope": "scope-a", "ok": True},
                                {"scope": "scope-b", "ok": False}])

        res = fs.run_full_setup("source", "c.example.com",
                                "admin@c.example.com", "pw")
        assert res["ok"] is False
        assert res["missingScopes"] == ["scope-b"]
