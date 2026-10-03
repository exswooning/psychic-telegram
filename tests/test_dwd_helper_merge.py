"""dwd_helper overwrites a delegation entry, so the live set must be probed in FULL.

The Admin console's only edit is Overwrite: every live scope missing from the
submitted line is revoked. The merge probed only today's default toggles (and the
legacy account's settings), so it would have revoked `drive` -- which server-side
needs -- and the seeder's write scopes on the same client. And when the probe
failed, it submitted the short list anyway.
"""
import os

import pytest

import dwd_helper


def test_the_probe_knows_every_scope_the_code_names():
    known = dwd_helper._every_known_scope()
    for s in ("https://www.googleapis.com/auth/drive",                 # server-side
              "https://www.googleapis.com/auth/chat.delete",           # seeder reset
              "https://www.googleapis.com/auth/calendar.acls.readonly",
              "https://www.googleapis.com/auth/gmail.insert"):
        assert s in known, s


def test_the_merge_keeps_everything_live_and_adds_only_what_was_asked(settings, monkeypatch):
    import verify_scopes
    live = {"https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/gmail.readonly"}
    probed = {}

    def verify(st, tenant, scopes):
        probed["scopes"] = set(scopes)
        return [{"scope": s, "ok": s in live} for s in scopes]
    monkeypatch.setattr(verify_scopes, "verify", verify)
    want = "https://www.googleapis.com/auth/chat.spaces.readonly"
    merged, added, got_live = dwd_helper._merge_with_live(settings, "source", want)
    assert "https://www.googleapis.com/auth/drive" in probed["scopes"]     # probed although not asked
    assert set(merged) == live | {want} and added == [want] and got_live == live


def test_a_probe_that_fails_refuses_rather_than_overwriting(monkeypatch):
    ran = []
    monkeypatch.setattr(dwd_helper, "_merge_with_live",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("no key")))
    monkeypatch.setattr(dwd_helper, "run", lambda *a, **k: ran.append(a) or 0)
    rc = dwd_helper.main(["--tenant", "source", "--client-id", "123", "--scopes", "x"])
    assert rc == 1 and ran == []


class TestTheSourceConsoleSignsInAsTheSourceAdmin:
    def test_its_own_admin_when_on_its_own_domain(self, settings, monkeypatch):
        settings.source_domain = "source.example"
        monkeypatch.setenv("DWD_EMAIL", "target-admin@target.example")
        monkeypatch.setenv("DWD_PASSWORD", "t")
        monkeypatch.setenv("DWD_EMAIL_SOURCE", "admin@source.example")
        monkeypatch.setenv("DWD_PASSWORD_SOURCE", "s")
        dwd_helper._sign_in_as_source_admin(settings)
        assert os.environ["DWD_EMAIL"] == "admin@source.example"
        assert os.environ["DWD_PASSWORD"] == "s"

    def test_never_another_tenants_admin(self, settings, monkeypatch):
        """A client's source is not our sandbox: our admin's login is never typed
        into it, and neither is the target admin's."""
        settings.source_domain = "client.example"
        monkeypatch.setenv("DWD_EMAIL", "target-admin@target.example")
        monkeypatch.setenv("DWD_PASSWORD", "t")
        monkeypatch.setenv("DWD_EMAIL_SOURCE", "admin@source.example")
        monkeypatch.setenv("DWD_PASSWORD_SOURCE", "s")
        dwd_helper._sign_in_as_source_admin(settings)
        assert "DWD_EMAIL" not in os.environ and "DWD_PASSWORD" not in os.environ


def test_the_after_check_verifies_against_the_accounts_own_key(monkeypatch):
    import inspect
    assert "settings or Settings()" in inspect.getsource(dwd_helper.run)
    assert "settings=st" in inspect.getsource(dwd_helper.main)


class TestItNeverEditsTheWrongEntry:
    """Live: run() clicked our row, then the FIRST Edit button on the page -- Google's
    Data Migration client's -- and gave that client our 33 scopes. Add new, addressed
    by the client ID we type, cannot be pointed at another row."""

    def test_no_row_edit_button_is_ever_used(self):
        import inspect
        code = "\n".join(l for l in inspect.getsource(dwd_helper.run).splitlines()
                         if not l.strip().startswith("#"))
        assert 'name="Edit"' not in code

    def test_a_dialog_for_another_client_is_refused_before_authorize(self):
        import inspect
        src = inspect.getsource(dwd_helper.run)
        assert src.index("REFUSING: the dialog's client ID") < src.index('clicking Authorize')

    def test_the_row_is_read_back_after_authorize(self):
        assert dwd_helper._row_scope_count(
            "sa@x\t115128313431159674171\thttps://mail.google.com/.../auth/admin.directory.group+28 More") == 30
        assert dwd_helper._row_scope_count("sa\t1\thttps://mail.google.com/\tView details") == 1
        assert dwd_helper._row_scope_count("Name Client ID") is None


class TestTheRunsOwnRegrant:
    def test_a_source_regrant_needs_the_source_admin_on_its_own_domain(self, settings, monkeypatch):
        import scope_guard
        settings.source_domain = "source.example"
        monkeypatch.setenv("DWD_EMAIL_SOURCE", "admin@source.example")
        monkeypatch.setenv("DWD_PASSWORD_SOURCE", "s")
        assert scope_guard.can_repair(settings, "source") is True
        settings.source_domain = "client.example"
        assert scope_guard.can_repair(settings, "source") is False

    def test_it_runs_dwd_helper_for_the_account_given(self, settings, monkeypatch):
        import scope_guard
        settings.account_id = 7
        settings.source_domain = "source.example"
        monkeypatch.setenv("DWD_EMAIL_SOURCE", "admin@source.example")
        monkeypatch.setenv("DWD_PASSWORD_SOURCE", "s")
        seen = {}

        class P:
            returncode, stdout, stderr = 0, "", ""
        def run(argv, **k):
            seen["argv"] = argv
            return P()
        monkeypatch.setattr(scope_guard.subprocess, "run", run)
        gap = scope_guard.ScopeGap(tenant="source", subject="admin@source.example", client_id="123",
                                   missing=["https://www.googleapis.com/auth/chat.spaces.readonly"])
        ok, _ = scope_guard.repair(gap, settings=settings)
        assert ok and seen["argv"][-2:] == ["--account-id", "7"]
