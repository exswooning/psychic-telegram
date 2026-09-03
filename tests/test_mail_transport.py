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
              webui._RUN_STATE.get("users"))
    webui._RUN_STATE["services"] = dict(SERVICES)
    webui._RUN_STATE["mail_transport"] = "engine"
    webui._RUN_STATE["users"] = ""
    yield
    (webui._RUN_STATE["services"], webui._RUN_STATE["mail_transport"],
     webui._RUN_STATE["users"]) = before


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
