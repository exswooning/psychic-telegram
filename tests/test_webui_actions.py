"""
tests/test_webui_actions.py
===========================
Server-side action wiring: the ACTIONS registry, the launch toggles, and the
env-derivation that lets phases.py see them.

This session added shared_drives.py, contacts_engine.py, tasks_engine.py,
sso.py and acl_audit.py to the engine without adding them to the one place an
operator actually clicks things -- the web UI. These tests cover the wiring
added to close that gap, not the features themselves (those have their own
suites).
"""

from __future__ import annotations

import os

import pytest

import webui

NEW_ACTIONS = ("phased_migrate", "phased_count_only", "shared_drives_inventory",
              "shared_drives_migrate", "acl_audit", "sso_inventory",
              "sso_migrate", "backfill_drive", "resolve")


class TestNewActionsAreWellFormed:
    """Same shape every other ACTIONS entry has to have -- checked here so a
    new entry can't silently violate the security boundary the module
    docstring describes."""

    @pytest.mark.parametrize("name", NEW_ACTIONS)
    def test_the_action_exists(self, name):
        assert name in webui.ACTIONS

    @pytest.mark.parametrize("name", NEW_ACTIONS)
    def test_argv_starts_with_the_interpreter_and_a_real_script(self, name):
        argv = webui.ACTIONS[name]["argv"]
        assert argv[0] == webui.PY
        script = argv[1]
        assert not script.startswith("-"), (
            f"{name}: {script!r} looks like a flag, not a script -- argv is "
            f"probably missing the script name")
        path = os.path.join(os.path.dirname(os.path.abspath(webui.__file__)), script)
        assert os.path.isfile(path), f"{name} points at {script!r}, which does not exist"

    @pytest.mark.parametrize("name", NEW_ACTIONS)
    def test_nothing_here_can_become_a_shell_string(self, name):
        """Matches the existing test_actions_never_build_a_shell_string
        invariant for the whole registry, restated for the new entries so a
        future refactor of ACTIONS can't quietly reintroduce shell=True."""
        argv = webui.ACTIONS[name]["argv"]
        assert all(isinstance(a, str) for a in argv)

    def test_every_destructive_new_action_has_a_confirm_phrase(self):
        for name in NEW_ACTIONS:
            spec = webui.ACTIONS[name]
            if spec.get("destructive"):
                assert spec.get("confirm"), f"{name} is destructive with no phrase"

    def test_read_only_verification_actions_are_not_destructive(self):
        """acl_audit and the inventory/count-only actions touch no tenant
        data -- if one of these ever gets marked destructive, that is a sign
        something about it changed and needs a second look, not that the
        flag should just follow."""
        for name in ("phased_count_only", "shared_drives_inventory",
                    "acl_audit", "sso_inventory", "backfill_drive", "resolve"):
            assert not webui.ACTIONS[name].get("destructive"), name

    def test_sso_migrate_does_not_pass_assign(self):
        """--assign recreates who signs in through the IdP; sso.py's own
        docstring calls that the dangerous half and keeps it a separate,
        deliberate step. The button must not skip that."""
        assert "--assign" not in webui.ACTIONS["sso_migrate"]["argv"]

    def test_shared_drives_actions_pass_all_drives(self):
        """Without --all-drives, shared_drives.py only sees drives the admin
        happens to belong to, which under-reports and under-migrates with no
        error -- exactly the silent-gap shape this codebase keeps finding."""
        for name in ("shared_drives_inventory", "shared_drives_migrate"):
            assert "--all-drives" in webui.ACTIONS[name]["argv"]

    def test_backfill_drive_names_a_service_explicitly(self):
        """backfill-services --services is required with no default; an
        empty or missing value would be an argparse error at run time
        instead of at review time."""
        argv = webui.ACTIONS["backfill_drive"]["argv"]
        assert "--services" in argv
        i = argv.index("--services")
        assert argv[i + 1], "no service named"


class TestServiceToggleDefaults:
    def test_contacts_and_tasks_are_off_like_chat(self):
        """Each widens the OAuth grant, and an unauthorised scope fails every
        call outright -- so enabling one must be a deliberate click, the same
        reasoning that already keeps chat off by default."""
        svcs = webui._RUN_STATE["services"]
        assert svcs["chat"] is False
        assert svcs["contacts"] is False
        assert svcs["tasks"] is False

    def test_the_original_three_are_still_on(self):
        svcs = webui._RUN_STATE["services"]
        assert svcs["drive"] is True
        assert svcs["gmail"] is True
        assert svcs["calendar"] is True

    def test_set_toggles_accepts_the_new_keys(self):
        prior = dict(webui._RUN_STATE["services"])
        try:
            out = webui.set_toggles({"services": {"contacts": True, "tasks": True}})
            assert out["toggles"]["services"]["contacts"] is True
            assert out["toggles"]["services"]["tasks"] is True
        finally:
            webui._RUN_STATE["services"] = prior


class TestPhaseGatedEnv:
    """
    phases.py's per-phase gate reads MIGRATE_CHAT/CONTACTS/TASKS from the
    environment regardless of which --phase was asked for, unlike main.py
    migrate/delta which infer them from --services. A checkbox only reaches
    phases.py if the launch environment says so explicitly -- otherwise a
    MIGRATE_CHAT=true left over in env.sh from an earlier session runs Chat
    with no checkbox visibly responsible for it.
    """

    def test_an_enabled_toggle_sets_true(self, monkeypatch):
        monkeypatch.setitem(webui._RUN_STATE["services"], "chat", True)
        env = webui._service_env()
        assert env["MIGRATE_CHAT"] == "true"

    def test_a_disabled_toggle_sets_false_explicitly(self, monkeypatch):
        """Not merely absent -- absent would inherit whatever env.sh already
        has, which is exactly the stale-setting failure this exists to
        prevent."""
        monkeypatch.setitem(webui._RUN_STATE["services"], "chat", False)
        monkeypatch.setenv("MIGRATE_CHAT", "true")   # stale from an old session
        env = webui._service_env()
        assert env["MIGRATE_CHAT"] == "false"

    def test_contacts_and_tasks_are_covered_too(self, monkeypatch):
        monkeypatch.setitem(webui._RUN_STATE["services"], "contacts", True)
        monkeypatch.setitem(webui._RUN_STATE["services"], "tasks", False)
        env = webui._service_env()
        assert env["MIGRATE_CONTACTS"] == "true"
        assert env["MIGRATE_TASKS"] == "false"

    def test_only_the_phase_gated_actions_use_it(self):
        """migrate/delta get the toggle through --services already, by way of
        main.py's own cmd_migrate; forcing the env there too would be
        harmless but is not the mechanism, and this pins which one is."""
        assert "migrate" not in webui._PHASE_GATED_ACTIONS
        assert "delta" not in webui._PHASE_GATED_ACTIONS
        assert "phased_migrate" in webui._PHASE_GATED_ACTIONS
        assert "phased_count_only" in webui._PHASE_GATED_ACTIONS


class TestGuidedPathIncludesTheNewVerification:
    def test_acl_audit_follows_verify_in_the_real_migration_paths(self):
        import wizard

        for mode in ("migrate_only", "seed_and_migrate"):
            runs = wizard.RUN_MODES[mode]["runs"]
            assert "acl_audit" in runs
            assert runs.index("acl_audit") > runs.index("verify")

    def test_seed_only_is_untouched(self):
        """Nothing to audit when nothing was migrated."""
        import wizard

        assert wizard.RUN_MODES["seed_only"]["runs"] == ["seed"]

    def test_every_run_mode_key_resolves_to_a_real_action_or_seed(self):
        import wizard

        for mode, spec in wizard.RUN_MODES.items():
            for key in spec["runs"]:
                assert key == "seed" or key in webui.ACTIONS, (
                    f"{mode} references {key!r}, which is not an action")

    def test_step_9_offers_the_new_checks(self):
        assert "acl_audit" in webui.STEP_ACTIONS[9]
        assert "resolve" in webui.STEP_ACTIONS[9]
