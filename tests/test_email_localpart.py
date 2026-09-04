"""
tests/test_email_localpart.py
=============================
Signup rejects the local-part shapes that can never receive mail.

The accounts table contains ".rohitrokaya08@gmail.com" -- a leading dot,
accepted by the original pattern, unable to receive mail, and sitting in
the admin account list looking like a typo nobody can account for. Found
by reading the Accounts page rather than by any check.

Deliberately narrow: validating email by regex is a losing game, so this
rejects only what RFC 5321 forbids outright in an unquoted local part --
a leading dot, a trailing dot, or two in a row -- and leaves every
argument about the rest alone.
"""

from __future__ import annotations

import pytest

from accounts_auth import AccountError, create_account


class TestTheShapesThatCannotReceiveMail:
    @pytest.mark.parametrize("email", [
        ".rohitrokaya08@gmail.com",     # the one in the live table
        "trailing.@gmail.com",
        "two..dots@gmail.com",
        ".@gmail.com",
    ])
    def test_they_are_refused(self, email, tmp_path, monkeypatch):
        with pytest.raises(AccountError, match="valid email"):
            create_account(email, "a-good-password", "Some Name")


class TestOrdinaryAddressesStillWork:
    @pytest.mark.parametrize("email", [
        "first.last@gmail.com",
        "a@b.co",
        "user+tag@example.com",
        "under_score@sub.domain.org",
        "digits123@example.com",
    ])
    def test_a_dot_inside_the_localpart_is_fine(self, email):
        """The guard must not become an opinion about valid addresses."""
        from accounts_auth import _EMAIL_RE, _BAD_LOCALPART
        assert _EMAIL_RE.match(email)
        assert not _BAD_LOCALPART.search(email.split("@")[0])

    def test_a_dot_in_the_domain_is_untouched(self):
        """The guard applies to the local part only -- every domain has dots."""
        from accounts_auth import _BAD_LOCALPART
        assert not _BAD_LOCALPART.search("user@many.dots.example.com".split("@")[0])
