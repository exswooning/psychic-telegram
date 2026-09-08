"""
tests/test_chat_repair_is_reachable.py
======================================
configure_chat_app has existed, and been wired into full_setup's own phase,
for a while. It was reachable from nowhere else -- so a tenant whose setup
died before that phase's result was written had no route back to it except
re-running the entire setup against a project that already exists.

Live consequence on this tenant: 46 finished users, 46 `404 Google Chat app
not found`, one per user, and 0 chat spaces across the whole corpus. The
setup's own result file still read {"running": true} with no phases, so
even what the phase had reported was gone.
"""

from __future__ import annotations

import inspect
import re

import pytest

import gcloud_browser_auth
import webui


def _block() -> str:
    # _do_POST, not do_POST: the latter is the crash-guard wrapper, whose
    # source contains none of this.
    src = inspect.getsource(webui.Handler._do_POST)
    i = src.index('if self.path == "/api/configure_chat_app":')
    j = src.index('if self.path == "/api/remove_tenant_setup":', i)
    return src[i:j]


class TestItCanBeRunOnItsOwn:
    def test_the_module_has_an_entry_point(self):
        assert callable(gcloud_browser_auth.main)

    def test_it_refuses_without_a_password_rather_than_hanging(self,
                                                              monkeypatch):
        monkeypatch.delenv("DWD_PASSWORD", raising=False)
        rc = gcloud_browser_auth.main(
            ["--configure-chat", "--project", "p", "--admin", "a@b.test"])
        assert rc == 2

    def test_the_password_is_never_an_argument(self):
        """argv is readable by every process on the box through ps."""
        src = inspect.getsource(gcloud_browser_auth.main)
        assert "DWD_PASSWORD" in src and "getenv" in src
        assert "--password" not in src


class TestTheEndpointGuardsItLikeTheOthers:
    def test_the_side_is_checked(self):
        assert 'side not in ("source", "target")' in _block()

    def test_the_password_goes_through_the_environment(self):
        blk = _block()
        assert 'env["DWD_PASSWORD"]' in blk
        assert "--password" not in blk

    def test_it_refuses_without_one(self):
        assert 'if not env["DWD_PASSWORD"]' in _block()

    def test_the_project_comes_from_the_key_not_the_body(self):
        """A caller-supplied project would let one tenant's request
        reconfigure another tenant's Cloud project."""
        blk = _block()
        assert "ensure_apis.project_of(key)" in blk
        assert 'body.get("project")' not in blk

    def test_the_child_is_not_told_to_resolve_an_account(self):
        """_account_env has already pointed MIGRATION_DB at this account's
        ledger; a child given --account-id follows it there looking for
        tenant_configs, a control-plane-only table."""
        assert '"--account-id"' not in _block()

    def test_the_job_is_named_for_what_it_does(self):
        assert '"configure chat app"' in _block()
