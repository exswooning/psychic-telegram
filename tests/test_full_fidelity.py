"""Full fidelity: every optional pass on whose scopes are granted.

Each off pass is data a whole-tenant run leaves behind, and each is off
because a run mints one token for all its scopes -- one ungranted scope fails
every call in the run. So a pass is only turned on after its own extra scopes
have been minted, and one that could not be is named with what is missing.
"""
from __future__ import annotations

import fidelity


def test_each_pass_names_only_the_scopes_it_adds(settings):
    import config
    assert fidelity.extra_scopes(settings, "migrate_external_shares") == {"source": [],
                                                                         "target": []}
    assert fidelity.extra_scopes(settings, "migrate_resources") == {
        "source": [config.RESOURCE_READONLY_SCOPE], "target": [config.RESOURCE_WRITE_SCOPE]}
    assert fidelity.extra_scopes(settings, "migrate_calendar_acls")["source"] == [
        config.CALENDAR_ACLS_READONLY_SCOPE]


def test_granted_passes_go_on_and_the_rest_say_why(settings):
    import config
    missing = {config.RESOURCE_WRITE_SCOPE}

    def probe(tenant, scopes):
        return (not (set(scopes) & missing), "not delegated")

    env, off = fidelity.plan(settings, probe)
    assert env["MIGRATE_EXTERNAL_SHARES"] == env["MIGRATE_SECONDARY_CALENDARS"] == "true"
    assert env["MIGRATE_GROUPS"] == "true"
    assert env["MIGRATE_RESOURCES"] == "false"          # written off, not left to the default
    assert off == ["MIGRATE_RESOURCES left off: not granted on target "
                   "admin.directory.resource.calendar (not delegated)"]


def test_a_pass_with_no_extra_scope_costs_no_token(settings):
    asked = []
    fidelity.plan(settings, lambda t, s: asked.append((t, tuple(s))) or (True, ""))
    assert all(s for _, s in asked)          # never probed with nothing to check


def test_the_launch_turns_them_on(monkeypatch, tmp_path):
    """The Start Migration launch passes the granted passes to the run as env."""
    import asyncio

    import api_server as A
    import config

    monkeypatch.setattr(fidelity, "plan",
                        lambda st, probe, can_grant=None: ({"MIGRATE_GROUPS": "true"}, ["MIGRATE_RESOURCES left off"]))
    seen = {}

    async def gated(op, action, body, target, launch):
        return launch()

    monkeypatch.setattr(A, "_gated", gated)
    monkeypatch.setattr(A, "_run_admitted", lambda argv, aid, name, env=None, **k:
                        seen.update(env=env) or (True, "started"))
    monkeypatch.setattr(A, "_resolve_account", lambda body, op: 3)

    class _S:
        rewrite_drive_links = True
    monkeypatch.setattr(config, "Settings", lambda account_id=None: _S())
    body = A.StartMigration(reason="measure the full-fidelity switch", services=["drive"])
    ok, detail = asyncio.run(A.migrate_start(body, op=None))
    assert seen["env"]["MIGRATE_GROUPS"] == "true"
    assert "MIGRATE_RESOURCES left off" in detail


def test_a_failed_check_never_blocks_the_launch(monkeypatch):
    import asyncio

    import api_server as A
    import config

    def boom(st, probe):
        raise RuntimeError("no key")
    monkeypatch.setattr(fidelity, "plan", boom)

    async def gated(op, action, body, target, launch):
        return launch()
    monkeypatch.setattr(A, "_gated", gated)
    monkeypatch.setattr(A, "_run_admitted", lambda *a, **k: (True, "started"))
    monkeypatch.setattr(A, "_resolve_account", lambda body, op: 3)

    class _S:
        rewrite_drive_links = True
    monkeypatch.setattr(config, "Settings", lambda account_id=None: _S())
    ok, detail = asyncio.run(A.migrate_start(
        A.StartMigration(reason="check never blocks", services=["drive"]), op=None))
    assert ok and "full fidelity not checked" in detail


class TestEverythingOnByDefault:
    """Full scope, always: every optional pass defaults on, and a pass whose
    scope a tenant has not granted is switched off by name -- never allowed to
    fail every call in the run, which is what one ungranted scope does."""

    def test_every_optional_pass_defaults_on(self, monkeypatch):
        from config import Settings
        for var in fidelity.OPTIONAL.values():
            monkeypatch.delenv(var, raising=False)
        s = Settings()
        assert all(getattr(s, flag) for flag in fidelity.OPTIONAL), [
            f for f in fidelity.OPTIONAL if not getattr(s, f)]

    def test_a_healthy_tenant_costs_one_token_per_side_and_drops_nothing(self, settings):
        asked = []
        assert fidelity.drop_ungranted(settings, lambda t, s: asked.append(t) or (True, "")) == []
        assert asked == ["source", "target"]

    def test_only_the_pass_with_the_missing_scope_is_switched_off(self, settings, monkeypatch):
        import config
        for flag in fidelity.OPTIONAL:
            setattr(settings, flag, True)
        missing = {config.RESOURCE_WRITE_SCOPE}
        monkeypatch.delenv("MIGRATE_RESOURCES", raising=False)
        monkeypatch.delenv(fidelity.DROPPED_ENV, raising=False)
        notes = fidelity.drop_ungranted(
            settings, lambda t, s: (not (set(s) & missing), "not delegated"))
        assert settings.migrate_resources is False and settings.migrate_groups is True
        assert notes == ["MIGRATE_RESOURCES off: target has not granted "
                         "admin.directory.resource.calendar (not delegated)"]
        import os
        assert "MIGRATE_RESOURCES" not in os.environ      # not exported unless asked

    def test_a_run_hands_the_switch_off_to_the_processes_it_starts(self, settings, monkeypatch):
        import os

        import config
        import main
        monkeypatch.delenv(fidelity.DROPPED_ENV, raising=False)
        monkeypatch.setenv("MIGRATE_CHAT", "true")
        settings.migrate_chat = True
        chat = set(config.CHAT_SCOPES)
        fidelity.drop_ungranted(settings, lambda t, s: (not (set(s) & chat), "x"),
                                export_env=True)
        assert os.environ["MIGRATE_CHAT"] == "false" and "MIGRATE_CHAT" in fidelity.dropped()
        # A child selecting chat must not switch it back on.
        child = type("S", (), {"migrate_chat": False, "migrate_contacts": False,
                               "migrate_tasks": False})()
        main._enable_selected_services(child, {"chat", "contacts"})
        assert child.migrate_chat is False and child.migrate_contacts is True

    def test_the_launch_check_writes_off_as_well_as_on(self, settings):
        import config
        env, off = fidelity.plan(settings, lambda t, s: (config.RESOURCE_WRITE_SCOPE not in s, "no"))
        assert env["MIGRATE_RESOURCES"] == "false" and env["MIGRATE_GROUPS"] == "true"


class TestEveryProcessChecksBeforeItsFirstCredential:
    def test_no_key_files_means_no_probe_and_no_change(self, settings, monkeypatch):
        import auth
        called = []
        monkeypatch.setattr(fidelity, "drop_ungranted", lambda *a, **k: called.append(1) or [])
        settings.source_sa_key = settings.target_sa_key = "/nonexistent.json"
        a = auth.AuthManager(settings)
        a._scopes("source")
        assert called == []

    def test_with_keys_it_checks_once(self, settings, monkeypatch, tmp_path):
        import auth
        called = []
        monkeypatch.setattr(fidelity, "drop_ungranted",
                            lambda s, probe, **k: called.append(1) or ["MIGRATE_SSO off: x"])
        for side in ("source", "target"):
            p = tmp_path / f"{side}.json"
            p.write_text("{}")
            setattr(settings, f"{side}_sa_key", str(p))
        settings.auth_mode = "key"        # other tests leave AUTH_MODE behind
        a = auth.AuthManager(settings)
        a._scopes("source")
        a._scopes("target")
        assert called == [1]


def test_a_run_creates_sso_profiles_unassigned(monkeypatch, db, settings):
    import main
    import sso
    seen = []
    monkeypatch.setattr(sso.SSOMigrator, "__init__", lambda self, a, d, s: None)
    monkeypatch.setattr(sso.SSOMigrator, "migrate_profiles", lambda self: seen.append("sso"))
    settings.migrate_groups = settings.migrate_resources = False
    settings.migrate_sso = True
    main._before_passes(db, object(), settings, {"drive"})
    assert seen == ["sso"]


def test_a_side_without_a_key_is_skipped_not_probed(settings, tmp_path):
    """reset_target carries only the target's key: requiring both skipped the
    check, every scope was requested, and the token was refused."""
    settings.source_sa_key = str(tmp_path / "missing.json")
    ok, why = fidelity.probe_for(settings)("source", ["x"])
    assert ok and "no key" in why


def test_one_key_is_enough_for_the_check(settings, monkeypatch, tmp_path):
    import auth
    called = []
    monkeypatch.setattr(fidelity, "drop_ungranted", lambda *a, **k: called.append(1) or [])
    p = tmp_path / "t.json"
    p.write_text("{}")
    settings.source_sa_key, settings.target_sa_key = str(tmp_path / "none.json"), str(p)
    settings.auth_mode = "key"
    auth.AuthManager(settings)._scopes("target")
    assert called == [1]


class TestARegrantableGapStaysOnForTheRunToGrant:
    """Live: the launch switched off every pass whose scopes were missing, so the run's
    start-up re-grant (scope_guard.ensure) never even saw them."""

    def test_on_a_tenant_the_run_can_grant_the_pass_stays_on_and_says_so(self, settings):
        import config
        missing = {config.CALENDAR_ACLS_READONLY_SCOPE}
        probe = lambda tenant, scopes: (not (set(scopes) & missing), "not delegated")
        env, notes = fidelity.plan(settings, probe, can_grant=lambda t: t == "source")
        assert env["MIGRATE_CALENDAR_ACLS"] == "true"
        assert any("start-up check grants it" in n for n in notes)

    def test_where_it_cannot_the_pass_is_left_off_as_before(self, settings):
        import config
        missing = {config.CALENDAR_ACLS_READONLY_SCOPE}
        probe = lambda tenant, scopes: (not (set(scopes) & missing), "not delegated")
        env, notes = fidelity.plan(settings, probe, can_grant=lambda t: False)
        assert env["MIGRATE_CALENDAR_ACLS"] == "false"
