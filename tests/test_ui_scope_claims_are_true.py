"""
tests/test_ui_scope_claims_are_true.py
======================================
The Setup Wizard makes a SECURITY claim in prose, and config.py decides
whether it is true.

Written because I shipped this sentence, in the wizard's own scope note:

    "no write scope is requested, so the tool cannot alter the tenant it
     is reading"

It is false. source_scopes() swaps drive.readonly for the Drive WRITE scope
under server_side and link_flip, because files.copy is a create call. The
claim was wrong in exactly the two modes where a reader would most want it
to be right, and nothing failed -- prose has no compiler.

So the prose is pinned to the code here: if a future mode adds a write scope
to the source, the wizard has to say so or this test fails.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from config import Settings, source_scopes, target_scopes

WIZARD = (pathlib.Path(__file__).resolve().parent.parent
          / "migration-webui" / "src" / "pages" / "Wizard.tsx")

# The modes whose whole point is copying server-side, which needs create.
WRITE_MODES = ("server_side", "link_flip")


def writes(scopes) -> set[str]:
    """Scopes that can change the tenant they are used against."""
    return {s for s in scopes if not s.endswith(".readonly")}


@pytest.fixture
def st():
    return Settings()


class TestTheSourceIsReadOnlyWhereTheUiSaysItIs:
    def test_the_default_mode_grants_the_source_no_write_scope(self, st):
        st.transfer_mode = "download_upload"
        assert writes(source_scopes(st)) == set()

    @pytest.mark.parametrize("mode", WRITE_MODES)
    def test_and_these_modes_really_do_grant_one(self, st, mode):
        """The exception is real, not theoretical -- which is the whole
        reason the sentence had to change."""
        st.transfer_mode = mode
        assert writes(source_scopes(st)), (
            f"{mode} no longer needs a source write scope; the wizard's "
            "note now over-warns and should be simplified")

    def test_the_set_of_flags_that_widen_the_source_is_exactly_this(self):
        """Every opt-in extra, one at a time, in the DEFAULT transfer mode.

        Found by writing this test: the source read-only promise has FIVE
        exceptions, not the one the wizard named. Google publishes no
        read-only variant of these scopes, so each is unavoidable given the
        feature -- but "unavoidable" and "unmentioned" are different things,
        and only the first was ever written down.

        This is the record. A new entry means a feature just widened the
        source: either give it a read-only scope, or say so in the UI.
        """
        expected = {
            "migrate_gmail_settings": {"gmail.settings.basic",
                                       "gmail.settings.sharing"},
            "migrate_chat": {"chat.messages", "chat.spaces"},
            "migrate_sso": {"admin.directory.user.security"},
            "migrate_calendar_acls": {"calendar"},
        }
        found = {}
        for flag in [f for f in vars(Settings()) if f.startswith("migrate_")]:
            fresh = Settings()
            fresh.transfer_mode = "download_upload"
            if not isinstance(getattr(fresh, flag, None), bool):
                continue
            setattr(fresh, flag, True)
            w = {s.split("/auth/")[-1] for s in writes(source_scopes(fresh))}
            if w:
                found[flag] = w
        assert found == expected, (
            f"the source's write exceptions changed.\n"
            f"  now: {found}\n  was: {expected}")


class TestTheWizardSaysWhatTheCodeDoes:
    def test_the_note_names_the_modes_that_break_read_only(self):
        """Not a copy check for its own sake: these two words are the only
        warning a reader gets that read-only has an exception."""
        text = WIZARD.read_text()
        assert "server-side" in text and "link-flip" in text, (
            "the wizard's scope note no longer names the modes that grant "
            "the source a write scope")

    def test_it_does_not_claim_no_write_scope_is_ever_requested(self):
        """The exact sentence that was false."""
        assert "no write scope is requested" not in WIZARD.read_text()

    def test_it_still_promises_a_read_and_write_target(self):
        assert re.search(r"read and write", WIZARD.read_text())


class TestTheTargetIsWritable:
    def test_the_target_gets_write_scopes(self, st):
        assert writes(target_scopes(st)), (
            "a migration creates the copy on the target; it cannot be "
            "read-only")

    def test_and_still_reads_the_directory_read_only(self, st):
        assert any(s.endswith("admin.directory.user.readonly")
                   for s in target_scopes(st))
