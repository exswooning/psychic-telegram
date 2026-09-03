"""
tests/test_mail_transport.py
============================
Who carries the mail is one choice, not two independent toggles.

Mail is the slice that matters -- 349,560 of 593,816 items on the last
full run -- and it sits behind a 3/sec/account write ceiling Google states
is not adjustable, so this engine is the slow path by construction. DMS
hands the whole job to Google, which is not rate-limited by us.

Before this, "engine does mail" and "DMS does mail" were separate
switches with two wrong states and no way to see you were in one:

  both on    every message copied twice -- a duplicate the user sees in
             their own inbox
  both off   no mail migrated at all, and a clean-looking run

Live evidence of the first: DMS ran after the engine had already inserted
the mail, discovered 288,219 messages and skipped 270,264 as already
present. Correct behaviour, entirely wasted pass.
"""

from __future__ import annotations

import pytest

import webui

SERVICES = {"drive": True, "gmail": True, "calendar": True,
            "chat": False, "contacts": False, "tasks": False}


@pytest.fixture(autouse=True)
def _clean():
    before = (dict(webui._RUN_STATE["services"]),
              webui._RUN_STATE.get("mail_transport"),
              webui._RUN_STATE.get("users"),
              webui._RUN_STATE.get("rewrite_drive_links"))
    webui._RUN_STATE["services"] = dict(SERVICES)
    webui._RUN_STATE["mail_transport"] = "engine"
    webui._RUN_STATE["users"] = ""
    yield
    (webui._RUN_STATE["services"], webui._RUN_STATE["mail_transport"],
     webui._RUN_STATE["users"], webui._RUN_STATE["rewrite_drive_links"]) = before
    webui._RUN_STATE.pop("last_note", None)


def _services(action="migrate"):
    argv = webui._action_argv(action)
    return argv[argv.index("--services") + 1].split(",") if "--services" in argv else []


class TestTheChoiceIsHonoured:
    def test_engine_carries_mail_by_default(self):
        assert "gmail" in _services()

    def test_dms_takes_gmail_off_the_engine(self):
        webui.set_toggles({"mail_transport": "dms"})
        assert "gmail" not in _services()

    def test_the_other_services_are_untouched(self):
        """Only mail moves. Turning off Drive too would be a silent, large
        change to what a run does."""
        webui.set_toggles({"mail_transport": "dms"})
        assert "drive" in _services() and "calendar" in _services()

    def test_switching_back_restores_mail(self):
        webui.set_toggles({"mail_transport": "dms"})
        webui.set_toggles({"mail_transport": "engine"})
        assert "gmail" in _services()

    def test_delta_follows_the_same_choice(self):
        """A delta that re-copied mail DMS already imported would duplicate
        exactly what the bulk pass was careful not to."""
        webui.set_toggles({"mail_transport": "dms"})
        assert "gmail" not in _services("delta")


class TestItCannotBeSetToNonsense:
    @pytest.mark.parametrize("bad", ["", "google", "DMS", None, 1, "both"])
    def test_an_unknown_transport_is_ignored(self, bad):
        """Anything but the two known values keeps the current setting.
        Falling back to a default would silently move the mail."""
        webui.set_toggles({"mail_transport": "dms"})
        webui.set_toggles({"mail_transport": bad})
        assert webui._RUN_STATE["mail_transport"] == "dms"

    def test_it_is_readable_so_the_ui_can_render_the_real_state(self):
        webui.set_toggles({"mail_transport": "dms"})
        assert webui.set_toggles({})["toggles"]["mail_transport"] == "dms"


class TestGmailOffWithoutDmsIsStillPossible:
    def test_turning_gmail_off_by_hand_still_works(self):
        """Choosing DMS is not the only reason to skip mail -- a Drive-only
        rehearsal is a normal thing to want."""
        webui.set_toggles({"services": {**SERVICES, "gmail": False}})
        assert "gmail" not in _services()


class TestDmsAndLinkRewritingCannotBothBeOn:
    """DMS never passes a message through gmail_engine -- Google copies the
    bytes and nothing here ever holds one -- so REWRITE_DRIVE_LINKS has no
    code path to run in under it.

    Leaving the switch on would be a flag that silently does nothing, which
    is precisely the failure the mail-before-Drive guard exists to prevent.
    Adding the transport choice created a second instance of it; this closes
    that one the same way, by making the impossible state unreachable and
    saying why rather than failing quietly.
    """

    def test_choosing_dms_turns_rewriting_off(self):
        webui.set_toggles({"mail_transport": "engine",
                           "rewrite_drive_links": True})
        t = webui.set_toggles({"mail_transport": "dms"})["toggles"]
        assert t["rewrite_drive_links"] is False

    def test_it_says_why_rather_than_just_doing_it(self):
        webui.set_toggles({"mail_transport": "engine",
                           "rewrite_drive_links": True})
        webui.set_toggles({"mail_transport": "dms"})
        note = webui._RUN_STATE.get("last_note", "")
        assert "DMS" in note and "source" in note

    def test_turning_rewriting_on_under_dms_is_refused(self):
        webui.set_toggles({"mail_transport": "dms"})
        t = webui.set_toggles({"rewrite_drive_links": True})["toggles"]
        assert t["rewrite_drive_links"] is False
        assert "This engine" in webui._RUN_STATE.get("last_note", "")

    def test_switching_back_to_the_engine_allows_it_again(self):
        webui.set_toggles({"mail_transport": "dms"})
        webui.set_toggles({"mail_transport": "engine"})
        t = webui.set_toggles({"rewrite_drive_links": True})["toggles"]
        assert t["rewrite_drive_links"] is True

    def test_the_note_clears_once_the_state_is_valid(self):
        """A stale explanation beside a valid setting is its own confusion."""
        webui.set_toggles({"mail_transport": "dms"})
        webui.set_toggles({"mail_transport": "engine"})
        webui.set_toggles({"rewrite_drive_links": True})
        assert "last_note" not in webui._RUN_STATE

    def test_the_scope_manifest_records_the_trade(self):
        """A customer deciding on DMS needs to know what it costs, and the
        manifest is the document that goes in front of them."""
        import scope
        rows = [i for i in scope.GMAIL_SCOPE if "DMS carries the mail" in i.item]
        assert rows, "the manifest does not mention the DMS trade"
        assert rows[0].status == scope.NONE
