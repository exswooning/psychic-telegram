"""
tests/test_admin_secrets.py
===========================
The credential file held one DWD_EMAIL/DWD_PASSWORD pair, and it was the
TARGET admin's. A source-side console step therefore signed in with the
target admin's password and sat on Google's sign-in form until it timed
out, reporting "likely 2FA/captcha" -- a guess, about a password that was
simply for a different account. Two tenants are two Google organisations
with two administrators; one pair cannot serve both.
"""

from __future__ import annotations

import pytest

import admin_secrets as sec


@pytest.fixture
def envfile(tmp_path):
    def write(text):
        p = tmp_path / "dwd.env"
        p.write_text(text)
        return str(p)
    return write


class TestPerSideWins:
    def test_the_source_password_is_used_for_source(self, envfile):
        p = envfile("DWD_PASSWORD_SOURCE=src-secret\nDWD_PASSWORD_TARGET=tgt-secret\n")
        assert sec.password_for("source", p) == "src-secret"

    def test_and_the_target_for_target(self, envfile):
        p = envfile("DWD_PASSWORD_SOURCE=src-secret\nDWD_PASSWORD_TARGET=tgt-secret\n")
        assert sec.password_for("target", p) == "tgt-secret"

    def test_a_side_specific_value_beats_the_shared_one(self, envfile):
        """Otherwise adding the source password changes nothing and the
        source step keeps signing in as the target."""
        p = envfile("DWD_PASSWORD=shared\nDWD_PASSWORD_SOURCE=src-secret\n")
        assert sec.password_for("source", p) == "src-secret"


class TestTheOldShapeKeepsWorking:
    def test_a_lone_dwd_password_still_serves(self, envfile):
        p = envfile("DWD_PASSWORD=only-one\n")
        assert sec.password_for("target", p) == "only-one"
        assert sec.password_for("source", p) == "only-one"

    def test_a_missing_file_is_empty_not_an_error(self, tmp_path):
        assert sec.password_for("source", str(tmp_path / "nope.env")) == ""


class TestItSaysWhichCredentialIsMissing:
    def test_no_password_names_the_admin_it_needed_one_for(self, envfile):
        p = envfile("")
        s = type("S", (), {"source_admin": "info@src.example"})()
        why = sec.missing("source", s, p)
        assert "info@src.example" in why and "DWD_PASSWORD_SOURCE" in why

    def test_no_admin_configured_is_a_different_problem(self, envfile):
        p = envfile("DWD_PASSWORD_SOURCE=x\n")
        s = type("S", (), {"source_admin": ""})()
        assert "admin address" in sec.missing("source", s, p)

    def test_nothing_at_all_says_so(self, envfile):
        p = envfile("")
        s = type("S", (), {"source_admin": ""})()
        assert "address or password" in sec.missing("source", s, p)

    def test_a_complete_pair_is_no_complaint(self, envfile):
        p = envfile("DWD_PASSWORD_SOURCE=x\n")
        s = type("S", (), {"source_admin": "info@src.example"})()
        assert sec.missing("source", s, p) == ""


class TestTheTenantConfigIsTheAuthorityForWho:
    def test_the_account_s_own_admin_wins_over_the_file(self, envfile):
        """The file is deployment-wide; the admin is per account. Letting
        the file win is how one account's step signs in as another's."""
        p = envfile("DWD_EMAIL=stale@old.example\n")
        s = type("S", (), {"source_admin": "info@src.example"})()
        assert sec.email_for("source", s, p) == "info@src.example"

    def test_the_file_fills_in_when_no_account_is_configured(self, envfile):
        p = envfile("DWD_EMAIL=fallback@x.example\n")
        assert sec.email_for("source", None, p) == "fallback@x.example"
