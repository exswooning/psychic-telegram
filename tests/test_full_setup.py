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
