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
        config.CALENDAR_WRITE_SCOPE]


def test_granted_passes_go_on_and_the_rest_say_why(settings):
    import config
    missing = {config.RESOURCE_WRITE_SCOPE}

    def probe(tenant, scopes):
        return (not (set(scopes) & missing), "not delegated")

    env, off = fidelity.plan(settings, probe)
    assert env["MIGRATE_EXTERNAL_SHARES"] == env["MIGRATE_SECONDARY_CALENDARS"] == "true"
    assert env["MIGRATE_GROUPS"] == "true"
    assert "MIGRATE_RESOURCES" not in env
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
                        lambda st, probe: ({"MIGRATE_GROUPS": "true"}, ["MIGRATE_RESOURCES left off"]))
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
