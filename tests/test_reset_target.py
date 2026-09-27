"""
tests/test_reset_target.py
==========================
Emptying the target tenant so a reconciliation means something.

phases.py treats target >= source as a pass — right for detecting loss, blind
to a target that was not empty. Observed live: a target holding 2,470 files
from earlier experiments produced "OK drive files 3,813" against a source of
1,342. The migration was in fact correct, but that verdict verified nothing.

This tool deletes from a live tenant, so the tests are about its guards.
"""

from __future__ import annotations

import inspect

import pytest

import reset_target
from config import Settings


def settings(src="c.example.com", tgt="a.example.com"):
    s = Settings()
    s.source_domain, s.target_domain = src, tgt
    return s


class TestGuards:
    def test_sandbox_mode_is_required(self, monkeypatch):
        monkeypatch.delenv("SANDBOX_MODE", raising=False)
        with pytest.raises(SystemExit, match="SANDBOX_MODE"):
            reset_target.assert_sandbox(settings(), "a.example.com")

    def test_the_typed_domain_must_match_the_target(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_MODE", "true")
        with pytest.raises(SystemExit, match="does not match"):
            reset_target.assert_sandbox(settings(), "something-else.com")

    def test_typing_the_source_domain_does_not_authorise_a_target_wipe(self, monkeypatch):
        """The likeliest slip, and the most expensive: emptying the source
        would destroy the corpus the migration is supposed to move."""
        monkeypatch.setenv("SANDBOX_MODE", "true")
        with pytest.raises(SystemExit):
            reset_target.assert_sandbox(settings(), "c.example.com")

    def test_a_protected_domain_is_refused(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_MODE", "true")
        monkeypatch.setenv("PROTECTED_DOMAINS", "a.example.com,corp.com")
        with pytest.raises(SystemExit, match="PROTECTED_DOMAINS"):
            reset_target.assert_sandbox(settings(), "a.example.com")

    def test_identical_source_and_target_is_refused(self, monkeypatch):
        """Nothing good can come of it, and the domain check alone would pass."""
        monkeypatch.setenv("SANDBOX_MODE", "true")
        monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)
        with pytest.raises(SystemExit, match="same"):
            reset_target.assert_sandbox(settings("x.com", "x.com"), "x.com")

    def test_a_correct_invocation_passes(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_MODE", "true")
        monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)
        reset_target.assert_sandbox(settings(), "a.example.com")


class TestItReusesTheSeedersReset:
    def test_it_calls_the_seeder_reset_functions(self):
        """Not a second implementation: the seeder's reset is already guarded,
        tested, and scoped to the MIGRATION-TEST roots."""
        src = inspect.getsource(reset_target.reset_one)
        for fn in ("reset_drive", "reset_gmail", "reset_calendar", "reset_chat"):
            assert f"seed.{fn}" in src

    def test_it_uses_target_credentials_throughout(self):
        """Passing source clients here would empty the wrong tenant."""
        src = inspect.getsource(reset_target.reset_one)
        assert "auth.target_drive" in src
        assert "auth.target_gmail" in src
        assert "auth.target_calendar" in src
        assert "source_drive" not in src and "source_gmail" not in src

    def test_one_failing_service_does_not_abort_the_rest(self):
        src = inspect.getsource(reset_target.reset_one)
        assert src.count("except Exception") >= 2


class TestSelectiveServiceReset:
    """
    Before --services existed, reset_target.py always wiped all four
    services together. That is wrong the moment only one of them needs a
    redo -- e.g. comparing Drive transfer modes after Gmail/Calendar/Chat
    already migrated successfully. Wiping everything to retest Drive would
    have destroyed real, already-correct data for no reason.
    """

    def test_default_resets_every_service(self, monkeypatch):
        calls = []
        monkeypatch.setattr(reset_target, "_load_seeder", lambda: _fake_seeder(calls))
        out = reset_target.reset_one(settings(), _FakeAuth(), "a@a.example.com")
        assert calls == ["drive", "gmail", "calendar", "chat"]
        assert out["drive"] == out["gmail"] == out["calendar"] == out["chat"] == 1

    def test_services_drive_only_touches_nothing_else(self, monkeypatch):
        calls = []
        monkeypatch.setattr(reset_target, "_load_seeder", lambda: _fake_seeder(calls))
        out = reset_target.reset_one(settings(), _FakeAuth(), "a@a.example.com",
                                     services=("drive",))
        assert calls == ["drive"]
        assert out["drive"] == 1
        assert out["gmail"] == out["calendar"] == out["chat"] == 0

    def test_unknown_service_name_is_refused_at_the_cli(self):
        with pytest.raises(SystemExit, match="unknown service"):
            reset_target.main(["--confirm-domain", "a.example.com",
                              "--services", "drive,carrier-pigeon"])

    def test_services_argument_defaults_to_all_four_in_help(self):
        """The flag exists to narrow scope, not to require it on every call
        -- an operator who never heard of --services must get today's
        full-wipe behavior unchanged."""
        assert reset_target.ALL_SERVICES == ("drive", "gmail", "calendar", "chat")


class _FakeGmail:
    """Just enough of the Gmail client: a paged listing that remembers the query, and trash()."""
    def __init__(self, pages):
        self.pages, self.queries, self.trashed = list(pages), [], []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults, pageToken=None):
        self.queries.append(q)
        page = self.pages[0 if pageToken is None else int(pageToken)]
        return _Call({"messages": [{"id": i} for i in page],
                      **({"nextPageToken": str(self.pages.index(page) + 1)} if page is not self.pages[-1] else {})})

    def trash(self, userId, id):
        self.trashed.append(id)
        return _Call({})


class _Call:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class TestGoogleWelcomeMail:
    """A reset-and-rerun cycle left a pair of Google's welcome mail on the target each time: the
    seeder's reset only trashes @seed.test mail, and the source's welcome mail is copied across."""

    def test_it_trashes_every_page_of_googles_own_mail_and_counts_it(self):
        g = _FakeGmail([["a", "b"], ["c"]])
        assert reset_target.trash_google_welcome_mail(g) == 3
        assert g.trashed == ["a", "b", "c"]

    def test_it_only_asks_for_mail_from_google_and_never_lists_the_trash(self):
        g = _FakeGmail([[]])
        assert reset_target.trash_google_welcome_mail(g) == 0
        assert g.queries == ["from:mail-noreply@google.com"]      # no `in:anywhere`: the trash is left as it is

    def test_the_reset_does_it_for_mail_and_only_for_mail(self, monkeypatch):
        calls, swept = [], []
        monkeypatch.setattr(reset_target, "_load_seeder", lambda: _fake_seeder(calls))
        monkeypatch.setattr(reset_target, "trash_google_welcome_mail", lambda g: swept.append(g) or 4)
        out = reset_target.reset_one(settings(), _FakeAuth(), "a@a.example.com")
        assert swept == [("gmail", "a@a.example.com")] and out["gmail"] == 1 + 4
        swept.clear()
        reset_target.reset_one(settings(), _FakeAuth(), "a@a.example.com", services=("drive",))
        assert swept == []

    def test_a_mailbox_the_reset_could_not_reach_is_not_swept_or_reported_twice(self, monkeypatch, capsys):
        class Seed:
            def reset_drive(self, svc, st): return 0
            def reset_gmail(self, svc, st): raise RuntimeError("invalid_grant")
            def reset_calendar(self, svc, st): return 0
            def reset_chat(self, svc, st, local): return 0
        swept = []
        monkeypatch.setattr(reset_target, "_load_seeder", lambda: Seed())
        monkeypatch.setattr(reset_target, "trash_google_welcome_mail", lambda g: swept.append(g) or 1)
        reset_target.reset_one(settings(), _FakeAuth(), "ghost@a.example.com")
        assert swept == [] and capsys.readouterr().out.count("invalid_grant") == 1

    def test_a_failure_tidying_up_does_not_lose_the_rest_of_the_reset(self, monkeypatch):
        calls = []
        monkeypatch.setattr(reset_target, "_load_seeder", lambda: _fake_seeder(calls))
        monkeypatch.setattr(reset_target, "trash_google_welcome_mail", lambda g: 1 / 0)
        out = reset_target.reset_one(settings(), _FakeAuth(), "a@a.example.com")
        assert calls == ["drive", "gmail", "calendar", "chat"] and out["chat"] == 1


class _FakeAuth:
    def target_drive(self, user):
        return ("drive", user)

    def target_gmail(self, user):
        return ("gmail", user)

    def target_calendar(self, user):
        return ("calendar", user)

    def target_chat(self, user):
        return ("chat", user)


def _fake_seeder(calls: list):
    class _Seed:
        def reset_drive(self, svc, settings):
            calls.append("drive")
            return 1

        def reset_gmail(self, svc, settings):
            calls.append("gmail")
            return 1

        def reset_calendar(self, svc, settings):
            calls.append("calendar")
            return 1

        def reset_chat(self, svc, settings, local):
            calls.append("chat")
            return 1
    return _Seed()


class TestItDoesNotShadowTheRealVerify:
    """
    data-generator/ holds its own verify.py. Putting that directory on
    sys.path -- as this module first did, to import the seeder -- shadowed the
    real one for the rest of the process, and the shadowed copy has none of
    the cutover-gate guards.

    Caught by the suite rather than in isolation: these tests passed alone and
    failed together, because merely importing reset_target made
    `verify.UserReport([]).ok` return True again. That is the exact false-pass
    those guards exist to prevent, reintroduced by an import side effect.
    """

    def test_importing_this_module_leaves_verify_intact(self):
        import verify

        assert verify.UserReport(source="a", target="b").ok is False

    def test_the_real_verify_is_the_one_loaded(self):
        import os

        import verify

        assert os.path.basename(os.path.dirname(verify.__file__)) != "data-generator"

    def test_reset_target_itself_adds_nothing_to_the_path(self):
        """pytest puts data-generator on sys.path itself when it collects the
        tests living there, so the suite-wide state is not this module's to
        assert. What is this module's business is not adding it."""
        import sys

        # seed_sandbox inserts the repo root itself at module level, which is
        # a harmless duplicate. The entry that must not survive is
        # data-generator, because that is the one which shadows verify.py.
        gen = [p for p in sys.path if p.endswith("data-generator")]
        reset_target._load_seeder()
        after = [p for p in sys.path if p.endswith("data-generator")]
        assert after == gen, "the seeder loader left data-generator on sys.path"

    def test_the_seeder_is_loaded_by_file_path(self):
        """Appending rather than prepending would still shadow later imports;
        loading by path shadows nothing."""
        src = inspect.getsource(reset_target._load_seeder)
        assert "spec_from_file_location" in src
        assert "sys.path.remove" in src
