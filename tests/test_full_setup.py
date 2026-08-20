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

import json
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
    def fn(side, project, org, key_dest, dry_run, force, env=None,
           on_step=None, admin_email=""):
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
        monkeypatch.setattr(fs.gcloud_browser_auth, "configure_chat_app",
                            lambda *a, **k: (True, "mocked"))
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
        non-null, is what has to gate the skip. No ambient gcloud identity
        and a failed browser sign-in together are the genuine dead end."""
        missing_path = tmp_path / "not-uploaded-yet.json"
        monkeypatch.setattr(fs.accounts_auth, "get_tenant_config",
                            lambda account_id, side: {"sa_key_path": str(missing_path)})
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready",
                            lambda: (False, "no active gcloud account"))
        monkeypatch.setattr(fs.gcloud_browser_auth, "login",
                            lambda email, password, timeout: (False, "no display available", ""))

        res = fs.run_full_setup("source", "c.example.com", "admin@c.example.com",
                                "pw", account_id=7)
        assert res["ok"] is False
        assert "no display available" in res["phases"][1]["detail"]

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
        # Must not reach a real gcloud/browser call either -- this only
        # matters for what get_tenant_config does, not for the (failed)
        # sign-in attempt that follows.
        monkeypatch.setattr(fs.gcloud_browser_auth, "login",
                            lambda email, password, timeout: (False, "gcloud is not installed", ""))

        fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "pw")
        assert called["get_tenant_config"] is False


class TestSideSelectionIsCorrect:
    """The bug that would have silently used the wrong tenant's project."""

    def test_target_call_provisions_a_target_named_project(self, monkeypatch):
        """gcloud_ready=True (an ambient identity) exercises the real naming
        path -- dry_run now skips provision_side entirely (see the elif
        dry_run branch in full_setup.py), so it can no longer stand in for
        a real run here the way it used to."""
        seen = {}

        def fake_provision_side(side, project, org, key_dest, dry_run, force,
                                env=None, on_step=None, admin_email=""):
            seen["side"] = side
            seen["project"] = project
            seen["key_dest"] = key_dest
            return {"side": side, "project": project, "ok": True, "steps": []}

        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (True, "me"))
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda env=None: "")
        monkeypatch.setattr(fs.provision_gcp, "provision_side", fake_provision_side)
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "42")
        monkeypatch.setattr(fs.dwd_helper, "run", lambda *a, **k: 0)
        monkeypatch.setattr(fs.gcloud_browser_auth, "configure_chat_app",
                            lambda *a, **k: (True, "mocked"))
        monkeypatch.setattr(fs.verify_scopes, "verify",
                            lambda settings, tenant, scopes: [
                                {"scope": s, "ok": True} for s in scopes])
        monkeypatch.setattr(fs.verify_scopes, "required_scopes",
                            lambda settings, tenant: ["scope-a"])

        fs.run_full_setup("target", "a.example.com", "admin@a.example.com", "pw")

        assert seen["side"] == "target"
        assert "tgt" in seen["project"]
        assert "target-sa.json" in seen["key_dest"]

    def test_source_call_never_touches_the_target_project_name(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (True, "me"))
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda env=None: "")
        monkeypatch.setattr(fs.provision_gcp, "provision_side",
                            lambda side, project, *a, **k: (
                                seen.setdefault("project", project),
                                {"side": side, "project": project, "ok": True,
                                 "steps": []})[1])
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "42")
        monkeypatch.setattr(fs.dwd_helper, "run", lambda *a, **k: 0)
        monkeypatch.setattr(fs.gcloud_browser_auth, "configure_chat_app",
                            lambda *a, **k: (True, "mocked"))
        monkeypatch.setattr(fs.verify_scopes, "verify",
                            lambda settings, tenant, scopes: [
                                {"scope": s, "ok": True} for s in scopes])
        monkeypatch.setattr(fs.verify_scopes, "required_scopes",
                            lambda settings, tenant: ["scope-a"])

        fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "pw")
        assert "src" in seen["project"]
        assert "tgt" not in seen["project"]


class TestPasswordNeverLeaks:
    def test_password_does_not_survive_in_the_environment(self, monkeypatch):
        """The one call that needs the password is dwd_helper.run(); once it
        returns, DWD_PASSWORD must be restored to whatever it was before --
        never left holding this call's secret for a later, unrelated step."""
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (True, "me"))
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda env=None: "")
        monkeypatch.setattr(fs.provision_gcp, "provision_side",
                            _fake_provision_side())
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "42")

        captured = {}

        def fake_dwd_run(client_id, scopes, timeout, headful, tenant=None):
            captured["password_during_call"] = os.environ.get("DWD_PASSWORD")
            return 0

        monkeypatch.setattr(fs.dwd_helper, "run", fake_dwd_run)
        monkeypatch.setattr(fs.gcloud_browser_auth, "configure_chat_app",
                            lambda *a, **k: (True, "mocked"))
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
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda env=None: "")
        monkeypatch.setattr(fs.provision_gcp, "provision_side",
                            _fake_provision_side())
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "42")
        monkeypatch.setattr(fs.dwd_helper, "run", lambda *a, **k: 0)
        monkeypatch.setattr(fs.gcloud_browser_auth, "configure_chat_app",
                            lambda *a, **k: (True, "mocked"))
        monkeypatch.setattr(fs.verify_scopes, "verify",
                            lambda settings, tenant, scopes: [
                                {"scope": s, "ok": True} for s in scopes])
        monkeypatch.setattr(fs.verify_scopes, "required_scopes",
                            lambda settings, tenant: ["scope-a"])

        monkeypatch.setenv("DWD_PASSWORD", "unrelated-preexisting-value")
        fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "pw")
        assert os.environ["DWD_PASSWORD"] == "unrelated-preexisting-value"


class TestDwdCrashIsRecoveredNotFatal:
    """Confirmed live: an uncaught Playwright TimeoutError deep inside
    dwd_helper.run() (a re-click racing a dialog that had already closed)
    propagated all the way out of this function and crashed the whole
    full_setup.py subprocess -- the caller's own status file was left
    reading {"running": true} forever, since the process that would have
    written a clean "failed" result was already dead. dwd_helper.py's own
    selectors are already best-effort against a console DOM that changes
    without notice; this is that same assumption applied to the one call
    site here that used to trust it completely."""

    def _common(self, monkeypatch):
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (True, "me"))
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda env=None: "")
        monkeypatch.setattr(fs.provision_gcp, "provision_side", _fake_provision_side())
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "42")
        monkeypatch.setattr(fs.gcloud_browser_auth, "configure_chat_app",
                            lambda *a, **k: (True, "mocked"))

    def test_a_crash_becomes_a_clean_failed_phase_not_an_exception(self, monkeypatch):
        self._common(monkeypatch)

        def crashing_dwd_run(*a, **k):
            raise TimeoutError("Locator.click: Timeout 30000ms exceeded.")

        monkeypatch.setattr(fs.dwd_helper, "run", crashing_dwd_run)

        res = fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "pw")

        assert res["ok"] is False
        dwd_phase = next(p for p in res["phases"] if "domain-wide delegation" in p["name"])
        assert dwd_phase["status"] == "failed"
        assert "crashed unexpectedly" in dwd_phase["detail"]
        assert "30000ms" in dwd_phase["detail"]

    def test_the_password_is_still_cleaned_up_after_a_crash(self, monkeypatch):
        """The whole point of the try/finally this sits inside -- a crash
        must not be a way to accidentally leave the password sitting in
        this process's own environment."""
        self._common(monkeypatch)
        monkeypatch.setattr(fs.dwd_helper, "run",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.delenv("DWD_PASSWORD", raising=False)

        fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "super-secret-pw")

        assert "DWD_PASSWORD" not in os.environ

    def test_a_normal_success_is_unaffected(self, monkeypatch):
        """The crash-handling path must not change behaviour for the
        overwhelming common case where dwd_helper.run() just returns 0."""
        self._common(monkeypatch)
        monkeypatch.setattr(fs.dwd_helper, "run", lambda *a, **k: 0)
        monkeypatch.setattr(fs.gcloud_browser_auth, "configure_chat_app",
                            lambda *a, **k: (True, "mocked"))
        monkeypatch.setattr(fs.verify_scopes, "required_scopes",
                            lambda settings, tenant: ["scope-a"])
        monkeypatch.setattr(fs.verify_scopes, "verify",
                            lambda settings, tenant, scopes: [
                                {"scope": s, "ok": True} for s in scopes])

        res = fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "pw")
        assert res["ok"] is True


class TestEnvShIsUpdated:
    """After a successful setup, the rest of the tool (webui.py's /api/seed,
    main.py, the Setup Wizard's own status page) must be pointed at the
    tenant that was just built -- otherwise a "Seed now" button that looks
    right ends up acting on whatever tenant env.sh happened to already
    name."""

    def _ok_common(self, monkeypatch):
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (True, "me"))
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda env=None: "")
        monkeypatch.setattr(fs.provision_gcp, "provision_side", _fake_provision_side())
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "42")
        monkeypatch.setattr(fs.dwd_helper, "run", lambda *a, **k: 0)
        monkeypatch.setattr(fs.gcloud_browser_auth, "configure_chat_app",
                            lambda *a, **k: (True, "mocked"))
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
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda env=None: "")
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
        """Neither an ambient identity NOR the browser-driven fallback is
        available -- the genuine dead end, not just a missing key file."""
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready",
                            lambda: (False, "gcloud is not installed"))
        monkeypatch.setattr(fs.gcloud_browser_auth, "login",
                            lambda email, password, timeout: (False, "gcloud is not installed", ""))
        res = fs.run_full_setup("source", "c.example.com",
                                "admin@c.example.com", "pw")
        assert res["ok"] is False
        assert "gcloud" in res["phases"][1]["detail"]

    def test_missing_scopes_after_grant_is_reported_not_assumed_ok(self, monkeypatch):
        """dwd_helper can exit 0 (submitted) while a scope still fails to
        verify -- propagation lag, or a partial grant. That must surface,
        not be swallowed into a plain success, once the retry budget below
        (see TestScopeVerificationRetriesThroughPropagation) is exhausted."""
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (True, "me"))
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda env=None: "")
        monkeypatch.setattr(fs.provision_gcp, "provision_side",
                            _fake_provision_side())
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "42")
        monkeypatch.setattr(fs.dwd_helper, "run", lambda *a, **k: 0)
        monkeypatch.setattr(fs.gcloud_browser_auth, "configure_chat_app",
                            lambda *a, **k: (True, "mocked"))
        monkeypatch.setattr(fs.verify_scopes, "required_scopes",
                            lambda settings, tenant: ["scope-a", "scope-b"])
        monkeypatch.setattr(fs.verify_scopes, "verify",
                            lambda settings, tenant, scopes: [
                                {"scope": "scope-a", "ok": True},
                                {"scope": "scope-b", "ok": False}])
        monkeypatch.setattr(fs.time, "sleep", lambda s: None)

        res = fs.run_full_setup("source", "c.example.com",
                                "admin@c.example.com", "pw")
        assert res["ok"] is False
        assert res["missingScopes"] == ["scope-b"]


class TestScopeVerificationRetriesThroughPropagation:
    """Confirmed live: dwd_helper.run()'s own log already knows a freshly
    accepted grant checks as 0/N scopes live for "a minute or two" and
    says so ("re-run verify_scopes.py ... before assuming it failed") --
    but a real run finishing verification at exactly the wrong moment
    used to report a hard failure on a grant that was only still
    propagating. This automates the very re-run dwd_helper.py's own
    output already tells a human to do by hand."""

    def _common(self, monkeypatch):
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (True, "me"))
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda env=None: "")
        monkeypatch.setattr(fs.provision_gcp, "provision_side", _fake_provision_side())
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "42")
        monkeypatch.setattr(fs.dwd_helper, "run", lambda *a, **k: 0)
        monkeypatch.setattr(fs.gcloud_browser_auth, "configure_chat_app",
                            lambda *a, **k: (True, "mocked"))
        monkeypatch.setattr(fs.verify_scopes, "required_scopes",
                            lambda settings, tenant: ["scope-a", "scope-b"])
        monkeypatch.setattr(fs.time, "sleep", lambda s: None)

    def test_dwd_helper_run_is_not_given_tenant(self, monkeypatch):
        """Regression: passing tenant=side made dwd_helper.run() do its
        OWN functional verification internally, with no retry, and
        return non-zero on any scope not yet live -- confirmed live, this
        short-circuited before phase 3 (the retried version of the exact
        same check) ever ran. Phase 3 must be the only verification gate."""
        self._common(monkeypatch)
        seen = {}

        def fake_dwd_run(client_id, scopes, timeout, headful, tenant=None):
            seen["tenant"] = tenant
            return 0

        monkeypatch.setattr(fs.dwd_helper, "run", fake_dwd_run)
        monkeypatch.setattr(fs.verify_scopes, "verify",
                            lambda settings, tenant, scopes: [
                                {"scope": s, "ok": True} for s in scopes])

        fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "pw")

        assert seen["tenant"] is None

    def test_retries_and_succeeds_once_scopes_finish_propagating(self, monkeypatch):
        self._common(monkeypatch)
        attempts = {"n": 0}

        def fake_verify(settings, tenant, scopes):
            attempts["n"] += 1
            all_live = attempts["n"] >= 3
            return [{"scope": s, "ok": all_live} for s in scopes]

        monkeypatch.setattr(fs.verify_scopes, "verify", fake_verify)

        res = fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "pw")

        assert res["ok"] is True
        assert attempts["n"] == 3

    def test_gives_up_after_exhausting_the_retry_budget(self, monkeypatch):
        self._common(monkeypatch)
        attempts = {"n": 0}

        def fake_verify(settings, tenant, scopes):
            attempts["n"] += 1
            return [{"scope": s, "ok": False} for s in scopes]

        monkeypatch.setattr(fs.verify_scopes, "verify", fake_verify)

        res = fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "pw")

        assert res["ok"] is False
        # 10 backoffs -> 11 verify_scopes.verify() calls (one before each
        # sleep, plus the final check after the last sleep).
        assert attempts["n"] == 11
        assert res["missingScopes"] == ["scope-a", "scope-b"]

    def test_a_clean_first_pass_does_not_retry_at_all(self, monkeypatch):
        """The overwhelming common case (propagation already finished by
        the time this checks) must not pay for a retry budget it never
        needed."""
        self._common(monkeypatch)
        monkeypatch.setattr(fs.time, "sleep",
                            lambda s: (_ for _ in ()).throw(
                                AssertionError("must not sleep on a clean first pass")))
        attempts = {"n": 0}

        def fake_verify(settings, tenant, scopes):
            attempts["n"] += 1
            return [{"scope": s, "ok": True} for s in scopes]

        monkeypatch.setattr(fs.verify_scopes, "verify", fake_verify)

        res = fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "pw")

        assert res["ok"] is True
        assert attempts["n"] == 1


class TestKeySavedEarlyEvenIfVerificationLaterFails:
    """Confirmed live: one real DWD grant took ~23 minutes to fully
    propagate while phase 3's own retry budget only waits ~15 -- a run
    can legitimately finish "failed" here with the underlying project,
    service account, and delegation all genuinely fine, just not yet
    confirmed. Saving the key path the moment phase 1 creates it (not
    only after phase 3 succeeds, the old behaviour -- see phase 3b) is
    what lets a retry reuse that already-granted client ID via the
    "uploaded key" fast path above, instead of creating a brand new
    throwaway GCP project and restarting propagation from zero on a
    brand new client ID."""

    def test_tenant_config_saved_before_verification_even_runs(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (True, "me"))
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda env=None: "")
        monkeypatch.setattr(fs.provision_gcp, "provision_side", _fake_provision_side())
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "42")
        monkeypatch.setattr(fs.dwd_helper, "run", lambda *a, **k: 0)
        monkeypatch.setattr(fs.gcloud_browser_auth, "configure_chat_app",
                            lambda *a, **k: (True, "mocked"))
        monkeypatch.setattr(fs.verify_scopes, "required_scopes",
                            lambda settings, tenant: ["scope-a"])
        monkeypatch.setattr(fs.verify_scopes, "verify",
                            lambda settings, tenant, scopes: [
                                {"scope": s, "ok": False} for s in scopes])
        monkeypatch.setattr(fs.time, "sleep", lambda s: None)
        monkeypatch.setattr(fs.accounts_auth, "get_tenant_config",
                            lambda account_id, side: None)
        saved = {}

        def fake_update(account_id, side, *, domain=None, admin_email=None,
                        sa_key_path=None):
            saved["account_id"] = account_id
            saved["sa_key_path"] = sa_key_path

        monkeypatch.setattr(fs.accounts_auth, "update_tenant_config", fake_update)

        res = fs.run_full_setup("source", "c.example.com",
                                "admin@c.example.com", "pw", account_id=7,
                                keys_dir=str(tmp_path))

        assert res["ok"] is False  # verification never clears in this test
        assert saved["account_id"] == 7
        assert saved["sa_key_path"] == os.path.join(str(tmp_path), "source-sa.json")


class TestBrowserAuthFallback:
    """When no ambient gcloud identity exists (the normal state on a shared
    VPS) and no key is on file yet, phase 1 must fall back to driving a
    real browser through gcloud's own OAuth consent with the SAME admin
    credentials already given for delegation -- not just fail with 'no
    active gcloud account' the way it used to."""

    def _common(self, monkeypatch):
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (False, "no active gcloud account"))
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda env=None: "")
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "42")
        monkeypatch.setattr(fs.dwd_helper, "run", lambda *a, **k: 0)
        monkeypatch.setattr(fs.gcloud_browser_auth, "configure_chat_app",
                            lambda *a, **k: (True, "mocked"))
        monkeypatch.setattr(fs.verify_scopes, "required_scopes",
                            lambda settings, tenant: ["scope-a"])
        monkeypatch.setattr(fs.verify_scopes, "verify",
                            lambda settings, tenant, scopes: [
                                {"scope": s, "ok": True} for s in scopes])
        # account_id=7 with no key uploaded yet -- the exact state that
        # should fall through to the browser-auth path.
        monkeypatch.setattr(fs.accounts_auth, "get_tenant_config", lambda account_id, side: None)
        monkeypatch.setattr(fs.accounts_auth, "update_tenant_config", lambda *a, **k: None)

    def test_falls_back_to_browser_auth_using_the_same_admin_credentials(self, monkeypatch):
        seen = {}

        def fake_login(email, password, timeout):
            seen["email"] = email
            seen["password"] = password
            return True, "You are now logged in as x", "/tmp/fake-cloudsdk-config"

        monkeypatch.setattr(fs.gcloud_browser_auth, "login", fake_login)
        monkeypatch.setattr(fs.gcloud_browser_auth, "cleanup", lambda d: None)
        monkeypatch.setattr(fs.provision_gcp, "provision_side", _fake_provision_side())
        self._common(monkeypatch)

        res = fs.run_full_setup("source", "c.example.com", "admin@c.example.com",
                                "super-secret-pw", account_id=7)

        assert res["ok"] is True
        assert seen["email"] == "admin@c.example.com"
        assert seen["password"] == "super-secret-pw"

    def test_provision_side_runs_with_the_ephemeral_cloudsdk_config(self, monkeypatch):
        """The whole point of the isolated login is that every gcloud call
        this makes on the tenant's behalf actually uses it -- not the
        process's own ambient (and, on this box, empty) config."""
        seen = {}
        monkeypatch.setattr(fs.gcloud_browser_auth, "login",
                            lambda email, password, timeout: (True, "ok", "/tmp/fake-cloudsdk-config"))
        monkeypatch.setattr(fs.gcloud_browser_auth, "cleanup", lambda d: None)

        def fake_provision_side(side, project, org, key_dest, dry_run, force,
                                env=None, on_step=None, admin_email=""):
            seen["env"] = env
            return {"side": side, "project": project, "ok": True, "steps": [], "clientId": "42"}

        monkeypatch.setattr(fs.provision_gcp, "provision_side", fake_provision_side)
        self._common(monkeypatch)

        fs.run_full_setup("source", "c.example.com", "admin@c.example.com",
                          "pw", account_id=7)

        assert seen["env"]["CLOUDSDK_CONFIG"] == "/tmp/fake-cloudsdk-config"

    def test_cleanup_runs_after_a_successful_provision(self, monkeypatch):
        cleaned = []
        monkeypatch.setattr(fs.gcloud_browser_auth, "login",
                            lambda email, password, timeout: (True, "ok", "/tmp/fake-cloudsdk-config"))
        monkeypatch.setattr(fs.gcloud_browser_auth, "cleanup", cleaned.append)
        monkeypatch.setattr(fs.provision_gcp, "provision_side", _fake_provision_side())
        self._common(monkeypatch)

        fs.run_full_setup("source", "c.example.com", "admin@c.example.com",
                          "pw", account_id=7)

        assert cleaned == ["/tmp/fake-cloudsdk-config"]

    def test_cleanup_runs_even_when_provisioning_itself_fails(self, monkeypatch):
        """The ephemeral credential must not outlive this call just because
        something downstream (a quota, an org policy) went wrong."""
        cleaned = []
        monkeypatch.setattr(fs.gcloud_browser_auth, "login",
                            lambda email, password, timeout: (True, "ok", "/tmp/fake-cloudsdk-config"))
        monkeypatch.setattr(fs.gcloud_browser_auth, "cleanup", cleaned.append)
        monkeypatch.setattr(fs.provision_gcp, "provision_side",
                            _fake_provision_side(ok=False, detail="quota exceeded"))
        self._common(monkeypatch)

        res = fs.run_full_setup("source", "c.example.com", "admin@c.example.com",
                                "pw", account_id=7)

        assert res["ok"] is False
        assert cleaned == ["/tmp/fake-cloudsdk-config"]

    def test_a_failed_browser_sign_in_never_calls_provision_side(self, monkeypatch):
        called = {"provision_side": False}
        monkeypatch.setattr(fs.gcloud_browser_auth, "login",
                            lambda email, password, timeout: (False, "stalled on 2FA", ""))
        monkeypatch.setattr(fs.provision_gcp, "provision_side",
                            lambda *a, **k: called.update(provision_side=True) or {})
        self._common(monkeypatch)

        res = fs.run_full_setup("source", "c.example.com", "admin@c.example.com",
                                "pw", account_id=7)

        assert res["ok"] is False
        assert called["provision_side"] is False
        assert "stalled on 2FA" in res["phases"][1]["detail"]

    def test_dry_run_never_drives_a_browser_sign_in(self, monkeypatch):
        """Preview needs no live credential at all -- see the elif dry_run
        branch in full_setup.py."""
        called = {"login": False}
        monkeypatch.setattr(fs.gcloud_browser_auth, "login",
                            lambda *a, **k: called.update(login=True) or (True, "ok", "/tmp/x"))
        self._common(monkeypatch)

        res = fs.run_full_setup("source", "c.example.com", "admin@c.example.com",
                                "pw", account_id=7, dry_run=True)

        assert called["login"] is False
        assert res["ok"] is True

    def test_an_ambient_ready_identity_never_drives_a_browser_sign_in(self, monkeypatch):
        """The legacy/local-gcloud caller (or any box that already has an
        authenticated gcloud) must keep working exactly as before -- the
        browser fallback only ever kicks in when gcloud_ready() says no."""
        called = {"login": False}
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (True, "me"))
        monkeypatch.setattr(fs.gcloud_browser_auth, "login",
                            lambda *a, **k: called.update(login=True) or (True, "ok", "/tmp/x"))
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda env=None: "")
        monkeypatch.setattr(fs.provision_gcp, "provision_side", _fake_provision_side())
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "42")
        monkeypatch.setattr(fs.dwd_helper, "run", lambda *a, **k: 0)
        monkeypatch.setattr(fs.gcloud_browser_auth, "configure_chat_app",
                            lambda *a, **k: (True, "mocked"))
        monkeypatch.setattr(fs.verify_scopes, "required_scopes",
                            lambda settings, tenant: ["scope-a"])
        monkeypatch.setattr(fs.verify_scopes, "verify",
                            lambda settings, tenant, scopes: [
                                {"scope": s, "ok": True} for s in scopes])

        fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "pw")

        assert called["login"] is False


class TestProgressFile:
    """A UI polling full_setup.py's own status file sees nothing but
    "still running" for however long a real run takes -- several minutes,
    for a fresh tenant -- unless something in here writes intermediate
    checkpoints somewhere pollable. progress_file is that somewhere;
    these pin what actually lands in it, not just that SOMETHING does."""

    def _ok_common(self, monkeypatch):
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (True, "me"))
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda env=None: "")
        monkeypatch.setattr(fs.provision_gcp, "provision_side", _fake_provision_side())
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "42")
        monkeypatch.setattr(fs.dwd_helper, "run", lambda *a, **k: 0)
        monkeypatch.setattr(fs.gcloud_browser_auth, "configure_chat_app",
                            lambda *a, **k: (True, "mocked"))
        monkeypatch.setattr(fs.verify_scopes, "required_scopes",
                            lambda settings, tenant: ["scope-a"])
        monkeypatch.setattr(fs.verify_scopes, "verify",
                            lambda settings, tenant, scopes: [
                                {"scope": s, "ok": True} for s in scopes])

    def test_a_full_run_ends_at_100_percent_done(self, monkeypatch, tmp_path):
        self._ok_common(monkeypatch)
        progress_file = str(tmp_path / "p.json")

        fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "pw",
                          progress_file=progress_file)

        with open(progress_file, encoding="utf-8") as fh:
            final = json.load(fh)
        assert final == {"pct": 100, "label": "done"}

    def test_pct_never_goes_backwards_across_checkpoints(self, monkeypatch, tmp_path):
        """_progress() overwrites the same file on every call -- this
        catches an out-of-order checkpoint that would make the bar visibly
        jump backwards mid-run, which a single final-value assertion
        can't."""
        self._ok_common(monkeypatch)
        progress_file = str(tmp_path / "p.json")
        seen = []
        orig_replace = fs.os.replace

        def spy_replace(src, dst):
            orig_replace(src, dst)
            with open(dst, encoding="utf-8") as fh:
                seen.append(json.load(fh)["pct"])
        monkeypatch.setattr(fs.os, "replace", spy_replace)

        fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "pw",
                          progress_file=progress_file)

        assert seen == sorted(seen), f"progress went backwards: {seen}"
        assert seen[0] < seen[-1]

    def test_dry_run_still_reaches_a_terminal_checkpoint(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "")
        progress_file = str(tmp_path / "p.json")

        fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "pw",
                          dry_run=True, progress_file=progress_file)

        with open(progress_file, encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["pct"] == 2, "dry run should only ever reach the initial checkpoint"

    def test_a_failed_browser_sign_in_leaves_progress_at_its_last_real_checkpoint(
            self, monkeypatch, tmp_path):
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (False, "no active gcloud account"))
        monkeypatch.setattr(fs.gcloud_browser_auth, "login",
                            lambda email, password, timeout: (False, "stalled on 2FA", ""))
        progress_file = str(tmp_path / "p.json")

        fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "pw",
                          progress_file=progress_file)

        with open(progress_file, encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["pct"] == 5
        assert "admin@c.example.com" in data["label"]

    def test_no_progress_file_given_is_a_silent_no_op(self, monkeypatch):
        """The overwhelming majority of callers (every test above this
        class, the whole CLI before this feature existed) never pass one
        -- this must not raise or change behaviour for them."""
        self._ok_common(monkeypatch)
        res = fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "pw")
        assert res["ok"] is True

    def test_a_write_failure_does_not_take_down_the_run(self, monkeypatch, tmp_path):
        """Best-effort by design -- see _progress()'s own docstring line."""
        self._ok_common(monkeypatch)
        # A directory where progress_file expects a writable file path.
        bad_path = str(tmp_path)

        res = fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "pw",
                                progress_file=bad_path)
        assert res["ok"] is True

    def test_on_step_from_provisioning_maps_into_the_18_to_75_range(self, monkeypatch, tmp_path):
        """The slowest stretch (project create + a dozen sequential API
        enables) is exactly where a caller most needs the bar to keep
        moving -- pin that provision_side's own on_step callback actually
        reaches _progress() with sane, in-range values."""
        monkeypatch.setattr(fs.provision_gcp, "gcloud_ready", lambda: (True, "me"))
        monkeypatch.setattr(fs.provision_gcp, "detect_org", lambda env=None: "")
        monkeypatch.setattr(fs.provision_gcp, "client_id_of", lambda p: "42")
        monkeypatch.setattr(fs.dwd_helper, "run", lambda *a, **k: 0)
        monkeypatch.setattr(fs.gcloud_browser_auth, "configure_chat_app",
                            lambda *a, **k: (True, "mocked"))
        monkeypatch.setattr(fs.verify_scopes, "required_scopes",
                            lambda settings, tenant: ["scope-a"])
        monkeypatch.setattr(fs.verify_scopes, "verify",
                            lambda settings, tenant, scopes: [
                                {"scope": s, "ok": True} for s in scopes])

        captured = []

        def fake_provision_side(side, project, org, key_dest, dry_run, force,
                                env=None, on_step=None, admin_email=""):
            for i in range(1, 6):
                if on_step:
                    on_step(i, 5, f"step {i}")
                    captured.append(fs.json.loads(open(  # noqa: SIM115
                        progress_file, encoding="utf-8").read())["pct"])
            return {"side": side, "project": project, "ok": True, "steps": [],
                    "clientId": "42"}
        monkeypatch.setattr(fs.provision_gcp, "provision_side", fake_provision_side)

        progress_file = str(tmp_path / "p.json")
        fs.run_full_setup("source", "c.example.com", "admin@c.example.com", "pw",
                          progress_file=progress_file)

        assert captured == [18 + int(57 * i / 5) for i in range(1, 6)]
        assert all(18 <= p <= 75 for p in captured)
