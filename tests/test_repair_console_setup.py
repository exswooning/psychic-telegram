"""
tests/test_repair_console_setup.py
==================================
The DWD grant and the Chat app are the two things a tenant needs that have
no API and no gcloud command. full_setup does both, once. Neither was
reachable afterwards -- so a tenant whose setup died before those phases, or
one set up before a scope was added, had no route back except re-running the
entire setup against a project that already exists.

Live: 46 finished users, 46 chat 404s, no chat data, a setup result file
still reading {"running": true}, and a delegation predating the purge scope
so its resets could only trash.
"""

from __future__ import annotations

import ast
import inspect
import os

import pytest

import repair_console_setup as R
import webui


def _argv_flags(script: str) -> list[str]:
    """The flags this module actually passes to `script`.

    Read from the syntax tree, not from the text: an earlier version of this
    check matched the word --admin inside a COMMENT explaining that
    dwd_helper has no such flag, and passed while the code was still wrong.
    """
    tree = ast.parse(open(R.__file__, encoding="utf-8").read())
    for n in ast.walk(tree):
        if isinstance(n, ast.List) and n.elts:
            vals = [e.value for e in n.elts if isinstance(e, ast.Constant)]
            if script in vals:
                return [v for v in vals
                        if isinstance(v, str) and v.startswith("--")]
    return []


class TestItCallsEachToolTheWayThatToolWorks:
    def test_dwd_helper_is_not_given_a_flag_it_does_not_have(self):
        """It takes the sign-in identity from DWD_EMAIL. A --admin would be
        an argparse error: exit 2, before the browser even opens."""
        assert "--admin" not in _argv_flags("dwd_helper.py")

    def test_dwd_helper_gets_the_client_and_the_scopes(self):
        flags = _argv_flags("dwd_helper.py")
        assert "--client-id" in flags and "--scopes" in flags

    def test_the_chat_step_gets_the_project_and_an_admin(self):
        flags = _argv_flags("gcloud_browser_auth.py")
        assert {"--configure-chat", "--project", "--admin"} <= set(flags)

    def test_every_flag_it_passes_is_one_the_target_accepts(self):
        """The check that would have caught --admin without knowing to look
        for it: read each script's own argparse and compare."""
        here = os.path.dirname(os.path.abspath(R.__file__))
        for script in ("dwd_helper.py", "gcloud_browser_auth.py"):
            tree = ast.parse(open(os.path.join(here, script),
                                  encoding="utf-8").read())
            accepted = {a.value for n in ast.walk(tree)
                        if isinstance(n, ast.Call)
                        and getattr(n.func, "attr", "") == "add_argument"
                        for a in n.args if isinstance(a, ast.Constant)}
            for flag in _argv_flags(script):
                assert flag in accepted, f"{script} does not accept {flag}"


class TestItRefusesRatherThanHanging:
    def test_no_password_is_an_error_not_a_prompt(self, monkeypatch):
        """Both steps sign in to a Google console, and a child with no tty
        cannot prompt -- it would block until the job was killed."""
        monkeypatch.delenv("DWD_PASSWORD", raising=False)
        out = R.repair("source", True, True)
        assert out["ok"] is False
        assert "DWD_PASSWORD" in out["error"]

    def test_it_runs_nothing_before_that_check(self, monkeypatch):
        monkeypatch.delenv("DWD_PASSWORD", raising=False)
        ran = []
        monkeypatch.setattr(R, "_run", lambda *a, **k: ran.append(a) or (True, ""))
        R.repair("source", True, True)
        assert ran == []


class TestTheEndpointGuardsIt:
    def _block(self) -> str:
        src = inspect.getsource(webui.Handler._do_POST)
        i = src.index('if self.path == "/api/repair_console_setup":')
        j = src.index('if self.path == "/api/configure_chat_app":', i)
        return src[i:j]

    def test_the_password_goes_through_the_environment(self):
        blk = self._block()
        assert 'env["DWD_PASSWORD"]' in blk and "--password" not in blk

    def test_it_refuses_without_one(self):
        assert 'if not env["DWD_PASSWORD"]' in self._block()

    def test_the_side_is_checked(self):
        assert 'side not in ("source", "target")' in self._block()

    def test_the_child_is_not_told_to_resolve_an_account(self):
        assert '"--account-id"' not in self._block()


class TestTheChatStepCanActuallyReachTheConsole:
    """The Chat app page is configured AS the Workspace admin, and a
    Workspace super admin holds nothing on a Cloud project. full_setup
    grants roles/editor for exactly this reason; repair did not, so a
    tenant whose provision skipped the grant failed here every time -- and
    reported it as a missing form field rather than a missing permission."""

    def test_repair_grants_project_access_before_configuring_chat(self):
        import inspect

        import repair_console_setup as rcs

        src = inspect.getsource(rcs.repair)
        grant = src.index("grant_admin_console_access")
        chat = src.index("--configure-chat")
        assert grant < chat, "the grant must happen before the console step"

    def test_it_uses_the_same_grant_provisioning_does(self):
        """Not a second copy of the gcloud invocation -- one of them would
        drift, and the one that drifts is the one nobody runs daily."""
        import inspect

        import provision_gcp
        import repair_console_setup as rcs

        assert "provision_gcp.grant_admin_console_access" in \
            inspect.getsource(rcs.repair)
        assert callable(provision_gcp.grant_admin_console_access)
