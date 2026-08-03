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

    def test_data_generator_is_not_on_the_path(self):
        import sys

        assert not any(p.endswith("data-generator") for p in sys.path), \
            "data-generator on sys.path shadows verify.py for the whole process"

    def test_the_seeder_is_loaded_by_file_path(self):
        """Appending rather than prepending would still shadow later imports;
        loading by path shadows nothing."""
        src = inspect.getsource(reset_target._load_seeder)
        assert "spec_from_file_location" in src
        assert "sys.path.remove" in src
