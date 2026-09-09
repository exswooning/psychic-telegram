"""
tests/test_signin_challenge.py
==============================
A 2-Step prompt on a headless VPS browser is displayed to nobody. These
check that the prompt reaches the transcript, and -- just as important --
that an ordinary sign-in page does not, because a "[2-STEP]" line on every
password screen teaches everyone to ignore the real one.
"""

from __future__ import annotations

import signin_challenge as sc

TAP_YES = """Google
Check your phone
Google sent a notification to your Pixel 7. Tap Yes on the notification, then tap 47 on your phone to verify it's you.
47
Resend it
Try another way
English (United States)
Help
Privacy
Terms"""

SMS = """Google
2-Step Verification
To help keep your account safe, Google wants to make sure it's really you trying to sign in
A text message with a 6-digit verification code was just sent to (***) ***-**23
Enter the code
Next
Try another way"""

KEY = """Google
Use your security key with admin@source.example
Touch your security key
Insert your key into the USB port
Try another way"""

PASSWORD_PAGE = """Google
Sign in
to continue to Google Admin
Email or phone
admin@source.example
Enter your password
Forgot password?
Next
English (United States)"""


class TestItRecognisesARealPrompt:
    def test_tap_yes(self):
        assert sc.describe(TAP_YES)

    def test_the_number_the_operator_has_to_tap_is_in_it(self):
        """This is the entire point -- without the number, "tap yes on your
        phone" is not actionable."""
        assert "47" in sc.describe(TAP_YES)

    def test_which_device_is_in_it(self):
        assert "Pixel 7" in sc.describe(TAP_YES)

    def test_sms_says_where_the_code_went(self):
        assert "**23" in sc.describe(SMS)

    def test_a_security_key(self):
        assert "Touch your security key" in sc.describe(KEY)


class TestItStaysQuietOtherwise:
    def test_an_ordinary_password_page_is_not_a_challenge(self):
        assert sc.describe(PASSWORD_PAGE) == ""

    def test_an_empty_page_is_not_a_challenge(self):
        assert sc.describe("") == ""

    def test_the_admin_console_itself_is_not_a_challenge(self):
        assert sc.describe("Google Admin\nDomain-wide delegation\nAdd new") == ""


class TestItIsReadableInOneLine:
    def test_chrome_boilerplate_is_dropped(self):
        out = sc.describe(TAP_YES)
        assert "Privacy" not in out and "Terms" not in out
        assert not out.startswith("Google")

    def test_it_does_not_run_on_forever(self):
        assert len(sc.describe(TAP_YES).splitlines()) == 1
        assert len(sc.describe(TAP_YES)) < 400


class FakePage:
    def __init__(self, text):
        self.text = text

    def inner_text(self, _sel):
        if self.text is None:
            raise RuntimeError("page is navigating")
        return self.text


class TestTheWatcher:
    def test_it_logs_the_prompt_once(self):
        lines = []
        w = sc.Watcher(lines.append)
        pages = [FakePage(TAP_YES)]
        w.check(pages)
        w.check(pages)
        assert sum("[2-STEP]" in ln and "47" in ln for ln in lines) == 1

    def test_it_logs_again_when_the_prompt_changes(self):
        lines = []
        w = sc.Watcher(lines.append)
        w.check([FakePage(TAP_YES)])
        w.check([FakePage(SMS)])
        assert any("**23" in ln for ln in lines)

    def test_a_page_that_is_mid_navigation_does_not_kill_the_signin(self):
        w = sc.Watcher(lambda _m: None)
        assert w.check([FakePage(None)]) == ""

    def test_it_finds_the_challenge_on_any_tab(self):
        """Google routinely leaves the challenge in the original tab and
        opens the console in a new one."""
        lines = []
        w = sc.Watcher(lines.append)
        w.check([FakePage(PASSWORD_PAGE), FakePage(TAP_YES)])
        assert any("47" in ln for ln in lines)

    def test_the_same_prompt_reappearing_after_another_is_logged_again(self):
        lines = []
        w = sc.Watcher(lines.append)
        w.check([FakePage(TAP_YES)])
        w.check([FakePage(PASSWORD_PAGE)])      # challenge gone
        w.check([FakePage(TAP_YES)])            # and back -- a retry
        assert sum("47" in ln for ln in lines) == 2


class TestEverySignInLoopIsCovered:
    """There are three of them. The watcher went into two, and the third --
    the Chat app configuration -- was the one that then sat on 'entered the
    password' for ninety seconds and reported 'likely 2FA/captcha' as a
    guess, with the answer on the screen it was already holding."""

    def _sources(self):
        import inspect

        import dwd_helper
        import gcloud_browser_auth

        return {
            "dwd console": inspect.getsource(dwd_helper._open_dwd_console),
            "gcloud auth": inspect.getsource(gcloud_browser_auth._drive_browser),
            "chat app": inspect.getsource(gcloud_browser_auth.configure_chat_app),
        }

    def test_each_one_watches_while_it_waits(self):
        """Specifically `challenge.check(` -- inside the polling loop.

        Checking only once, after the timeout, is what the code already did
        by saving a screenshot: it tells you afterwards, when the ninety
        seconds are gone. The point is to say it WHILE the phone is
        buzzing. An earlier version of this test asserted merely that the
        word appeared somewhere in the function, and passed with the
        in-loop watcher deleted.
        """
        missing = [name for name, src in self._sources().items()
                   if "challenge.check(" not in src]
        assert not missing, f"sign-in loops that never poll for 2-Step: {missing}"

    def test_the_chat_timeout_reports_what_it_saw(self):
        import inspect

        import gcloud_browser_auth

        src = inspect.getsource(gcloud_browser_auth.configure_chat_app)
        assert "signin_challenge.from_page" in src
        assert "2-Step prompt" in src
