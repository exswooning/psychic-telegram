"""
tests/test_rewrite_toggle.py
============================
REWRITE_DRIVE_LINKS is a launch toggle, not an env-only setting.

A feature that cannot be switched on from the product cannot be checked
from it either -- and checking a change through the UI is the workflow the
UI exists for. It travels as environment rather than argv because main.py
has no flag for it: the engine reads it off Settings(), which reads the
environment.

Default off, deliberately. The engine refuses to start a mail pass with
this on before Drive has migrated (an inserted message is skipped by dedup
forever, so links rewritten too late stay wrong permanently), which means
a toggle left on by accident turns a mail-only migration into a hard
failure rather than a slow one.
"""

from __future__ import annotations

import pytest

import webui


@pytest.fixture(autouse=True)
def _restore():
    before = webui._RUN_STATE.get("rewrite_drive_links")
    yield
    webui._RUN_STATE["rewrite_drive_links"] = before


class TestTheToggle:
    def test_it_is_off_by_default(self):
        assert webui._RUN_STATE["rewrite_drive_links"] is False

    def test_it_can_be_switched_on(self):
        out = webui.set_toggles({"rewrite_drive_links": True})
        assert out["toggles"]["rewrite_drive_links"] is True

    def test_it_can_be_switched_off_again(self):
        webui.set_toggles({"rewrite_drive_links": True})
        assert webui.set_toggles(
            {"rewrite_drive_links": False})["toggles"]["rewrite_drive_links"] is False

    def test_an_unrelated_toggle_does_not_disturb_it(self):
        """set_toggles is sent partial bodies by the toolbar; a missing key
        must mean 'unchanged', not 'off'."""
        webui.set_toggles({"rewrite_drive_links": True})
        webui.set_toggles({"dry_run": True})
        assert webui._RUN_STATE["rewrite_drive_links"] is True

    def test_it_is_readable_not_only_settable(self):
        """A UI that can only POST has to mutate something to discover the
        state, so the switch renders from whatever the page assumed."""
        webui.set_toggles({"rewrite_drive_links": True})
        assert dict(webui._RUN_STATE)["rewrite_drive_links"] is True


class TestItReachesTheEngine:
    def test_on_becomes_a_true_environment_value(self):
        webui.set_toggles({"rewrite_drive_links": True})
        assert webui._launch_env({})["REWRITE_DRIVE_LINKS"] == "true"

    def test_off_is_written_explicitly_not_omitted(self):
        """Omitting it would let a value left in env.sh win over the toggle,
        so the switch would appear off while the run had it on."""
        webui.set_toggles({"rewrite_drive_links": False})
        assert webui._launch_env(
            {"REWRITE_DRIVE_LINKS": "true"})["REWRITE_DRIVE_LINKS"] == "false"

    def test_it_does_not_clobber_the_rest_of_the_environment(self):
        env = webui._launch_env({"SOURCE_DOMAIN": "a.test", "MIGRATION_DB": "/x"})
        assert env["SOURCE_DOMAIN"] == "a.test" and env["MIGRATION_DB"] == "/x"

    def test_the_base_environment_is_not_mutated(self):
        base = {"SOURCE_DOMAIN": "a.test"}
        webui._launch_env(base)
        assert "REWRITE_DRIVE_LINKS" not in base

    def test_only_the_launch_actions_get_it(self):
        """Every other action has a fixed argv and no business inheriting a
        per-run choice."""
        src = open("webui.py", encoding="utf-8").read()
        seg = src.split("if name in _LAUNCH_KEYS:", 1)[1][:120]
        assert "_launch_env(env)" in seg


class TestScopingARunToNamedUsers:
    """Without this the UI could only ever launch a whole-tenant migration --
    200 users and roughly six days on the live account -- which means the one
    thing the UI exists for, checking a change, could not be done from it.
    Same gap the seed form had, same fix."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        before = webui._RUN_STATE.get("users")
        webui._RUN_STATE["users"] = ""
        yield
        webui._RUN_STATE["users"] = before

    def test_empty_still_means_every_mapped_user(self):
        assert "--user" not in webui._action_argv("migrate")

    def test_named_users_each_get_their_own_flag(self):
        """main.py takes --user repeatedly, not a comma list."""
        webui.set_toggles({"users": "a@x.test, b@x.test"})
        argv = webui._action_argv("migrate")
        assert argv.count("--user") == 2
        assert "a@x.test" in argv and "b@x.test" in argv

    def test_surrounding_whitespace_is_not_part_of_the_address(self):
        webui.set_toggles({"users": "  a@x.test ,  b@x.test  "})
        argv = webui._action_argv("migrate")
        assert "a@x.test" in argv and " a@x.test " not in argv

    def test_a_trailing_comma_does_not_add_an_empty_user(self):
        """--user '' would match nothing and migrate nobody, silently."""
        webui.set_toggles({"users": "a@x.test,"})
        argv = webui._action_argv("migrate")
        assert argv.count("--user") == 1

    def test_delta_is_scoped_too(self):
        webui.set_toggles({"users": "a@x.test"})
        assert "--user" in webui._action_argv("delta")

    def test_a_fixed_argv_action_is_untouched(self):
        """Only migrate/delta follow the toggles; verify, report and the rest
        have fixed argv and must not inherit a per-run choice."""
        webui.set_toggles({"users": "a@x.test"})
        assert "--user" not in webui._action_argv("verify")

    def test_clearing_it_restores_the_whole_tenant(self):
        webui.set_toggles({"users": "a@x.test"})
        webui.set_toggles({"users": ""})
        assert "--user" not in webui._action_argv("migrate")
