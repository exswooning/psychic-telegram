"""
tests/test_chat_delete_mismatch.py
==================================
Deleting Chat spaces needs two switches, in two different places.

chat.spaces does not cover delete, so a reset needs the chat.delete scope
granted in the Admin Console AND chat_allow_delete set in config. Neither
half knows about the other, and both mismatches fail quietly:

  granted, flag off      the wipe leaves every space standing. Nothing is
                         broken, so nothing says so -- and the next seed
                         stacks on top of spaces the wipe should have
                         removed. This is the live state that prompted
                         this check: the grant had been made months ago
                         and the flag never followed.

  not granted, flag on   worse, and not visibly about Chat. Requesting an
                         ungranted scope fails the WHOLE token exchange,
                         so Drive, Gmail and Calendar go down with it and
                         the error says unauthorized_client without ever
                         mentioning Chat.

Only the second is a failure. The first is a warning: seeding still works
perfectly, it is the reset that quietly under-deletes.
"""

from __future__ import annotations

import pytest

import check_seed


class _S:
    source_admin = "admin@src.test"
    source_domain = "src.test"

    def __init__(self, allow):
        self.chat_allow_delete = allow


@pytest.fixture
def grant(monkeypatch):
    """Control whether minting chat.delete succeeds."""
    def _set(ok):
        def _build(*a, **k):
            if not ok:
                raise RuntimeError("unauthorized_client")
            return object()
        monkeypatch.setattr(check_seed, "_build", _build)
    return _set


class TestBothHalvesAgree:
    def test_granted_and_enabled_is_fine(self, grant, capsys):
        grant(True)
        assert check_seed._report_chat_delete(_S(True)) is True
        assert "OK" in capsys.readouterr().out

    def test_neither_is_a_note_not_a_problem(self, grant, capsys):
        """Not wanting Chat deleted is a legitimate configuration."""
        grant(False)
        assert check_seed._report_chat_delete(_S(False)) is True
        out = capsys.readouterr().out
        assert "note" in out and "FAIL" not in out


class TestTheQuietMismatch:
    def test_granted_but_disabled_warns_without_failing(self, grant, capsys):
        """Seeding works; only the reset under-deletes. Failing the whole
        check would be wrong, and saying nothing is how this went unnoticed."""
        grant(True)
        assert check_seed._report_chat_delete(_S(False)) is True
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "CHAT_ALLOW_DELETE" in out

    def test_it_says_the_fix_needs_no_admin_console_trip(self, grant, capsys):
        """The whole point: the expensive half is already done. Someone told
        it is 'a scope problem' goes and re-grants a scope they already have."""
        grant(True)
        check_seed._report_chat_delete(_S(False))
        assert "no Admin Console change needed" in capsys.readouterr().out


class TestTheDangerousMismatch:
    def test_enabled_without_the_grant_is_a_failure(self, grant, capsys):
        grant(False)
        assert check_seed._report_chat_delete(_S(True)) is False

    def test_it_names_the_blast_radius(self, grant, capsys):
        """An operator reading 'unauthorized_client' has no reason to suspect
        Chat, so the check has to say Drive and Gmail go down too."""
        grant(False)
        check_seed._report_chat_delete(_S(True))
        out = capsys.readouterr().out
        assert "Drive" in out and "Gmail" in out


class TestItIsWiredIntoTheScopeCheck:
    def test_check_scopes_calls_it(self):
        import inspect
        assert "_report_chat_delete" in inspect.getsource(check_seed.check_scopes)

    def test_a_missing_flag_attribute_defaults_to_off(self, grant):
        """Older configs have no chat_allow_delete at all; that must read as
        'off', not raise."""
        grant(True)

        class Bare:
            source_admin = "a@b.test"
        assert check_seed._report_chat_delete(Bare()) is True
