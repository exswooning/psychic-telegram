"""
tests/test_gcloud_browser_auth.py
==================================
Mirrors test_dwd_helper.py's philosophy: the parts that are pure logic
(URL extraction, deciding which field is the right one to fill, the
login()/cleanup() orchestration around the gcloud subprocess) are covered
here with fakes; the actual browser choreography (_drive_browser) drives a
real Playwright page against Google's real sign-in and is exercised live
instead -- a mocked Locator would test the mock, not Google's console.

login()'s own tests treat _drive_browser as a seam (same idiom
full_setup.py already uses for dwd_helper.run/provision_gcp.provision_side)
so they can pin the subprocess orchestration -- URL parsing, the timeout
that stops the browser loop from outliving the timeout budget the caller
asked for, cleanup on every failure path -- without ever importing
Playwright.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gcloud_browser_auth as ga  # noqa: E402


class _FakeLocator:
    """Mirrors test_dwd_helper.py's _FakeLocator, extended with the
    is_visible/is_enabled/click/type surface _fill_visible actually uses."""

    def __init__(self, boxes):
        self._boxes = boxes  # list of _FakeBox

    def count(self):
        return len(self._boxes)

    def nth(self, i):
        return self._boxes[i]


class _FakeBox:
    def __init__(self, visible=True, enabled=True, raises=False):
        self._visible = visible
        self._enabled = enabled
        self._raises = raises
        self.clicked = False
        self.typed = None

    def is_visible(self):
        return self._visible

    def is_enabled(self):
        return self._enabled

    def click(self):
        if self._raises:
            raise Exception("detached")  # noqa: TRY002 - mirrors Playwright's
        self.clicked = True

    def type(self, value, delay=0):
        self.typed = value


class _FakePage:
    def __init__(self, boxes):
        self._locator = _FakeLocator(boxes)
        self.enters_pressed = 0

    def locator(self, selector):
        return self._locator

    class _Keyboard:
        def __init__(self, outer):
            self._outer = outer

        def press(self, key):
            self._outer.enters_pressed += 1

    @property
    def keyboard(self):
        return self._Keyboard(self)


class TestFillVisible:
    """The email box on Google's identifier page is `#identifierId`,
    type=text -- NOT type=email -- and a hidden password box is also
    present on that same page, which is why is_visible()/is_enabled() have
    to gate every candidate rather than just filling index 0."""

    def test_fills_the_first_visible_enabled_match(self):
        box = _FakeBox()
        pg = _FakePage([box])
        assert ga._fill_visible(pg, "sel", "hello@example.com") is True
        assert box.typed == "hello@example.com"
        assert pg.enters_pressed == 1

    def test_skips_an_invisible_box_and_uses_the_next_one(self):
        hidden = _FakeBox(visible=False)
        visible = _FakeBox()
        pg = _FakePage([hidden, visible])
        assert ga._fill_visible(pg, "sel", "pw") is True
        assert hidden.typed is None
        assert visible.typed == "pw"

    def test_skips_a_disabled_box(self):
        disabled = _FakeBox(enabled=False)
        pg = _FakePage([disabled])
        assert ga._fill_visible(pg, "sel", "pw") is False

    def test_no_matches_returns_false(self):
        pg = _FakePage([])
        assert ga._fill_visible(pg, "sel", "pw") is False

    def test_an_exception_on_one_box_falls_through_to_the_next(self):
        broken = _FakeBox(raises=True)
        ok = _FakeBox()
        pg = _FakePage([broken, ok])
        assert ga._fill_visible(pg, "sel", "pw") is True
        assert ok.typed == "pw"


class _FakeStdout:
    """readline() only ever serves `first_lines` -- the lines gcloud prints
    while login() is still hunting for the auth URL. read() (called once,
    after _drive_browser returns and the process is wait()ed on) serves
    `trailing` -- everything gcloud goes on to print while the browser
    finishes the flow, including the "You are now logged in as" marker
    login() actually trusts. Keeping these two separate is the point: an
    earlier version of this fake let a test overwrite the SAME buffer
    `readline()` was still supposed to be reading the auth URL from,
    which starved the URL search and made login() spin for the real 20s
    deadline instead of failing or succeeding as the test intended."""

    def __init__(self, first_lines, trailing=""):
        self._lines = list(first_lines)
        self._trailing = trailing

    def readline(self):
        return self._lines.pop(0) if self._lines else ""

    def read(self):
        return self._trailing


class _FakeProc:
    """poll() reports "still running" (None) throughout -- these tests
    bypass _drive_browser's own internal polling loop entirely (it is
    monkeypatched to a no-op), so the only place poll() is actually
    consulted here is login()'s URL-extraction loop, before a sign-in
    could plausibly have finished. wait() carries the real, final rc."""

    def __init__(self, first_lines, trailing="", rc=0):
        self.stdout = _FakeStdout(first_lines, trailing)
        self._rc = rc
        self.killed = False
        self.waited = False

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.waited = True
        return self._rc

    def kill(self):
        self.killed = True


class TestLogin:
    def test_gcloud_not_installed_fails_fast_with_no_temp_dir_left_behind(self, monkeypatch):
        monkeypatch.setattr(ga.shutil, "which", lambda name: None)
        ok, detail, cfg = ga.login("admin@example.com", "pw")
        assert ok is False
        assert "not installed" in detail
        assert cfg == ""

    def test_no_auth_url_ever_printed_fails_and_cleans_up(self, monkeypatch):
        monkeypatch.setattr(ga.shutil, "which", lambda name: "/usr/bin/gcloud")
        removed = []
        monkeypatch.setattr(ga.shutil, "rmtree", lambda d, ignore_errors=False: removed.append(d))
        monkeypatch.setattr(ga.tempfile, "mkdtemp", lambda prefix="": "/tmp/cloudsdk-x")
        # Fast-forward past the 20s URL-wait deadline instead of actually
        # sleeping through it -- first call establishes the deadline,
        # every call after reports it as already elapsed.
        monkeypatch.setattr(ga.time, "sleep", lambda s: None)
        calls = {"n": 0}

        def fake_time():
            calls["n"] += 1
            return 0 if calls["n"] == 1 else 100
        monkeypatch.setattr(ga.time, "time", fake_time)
        fake = _FakeProc(["some unrelated line\n"])
        monkeypatch.setattr(ga.subprocess, "Popen", lambda *a, **k: fake)
        ok, detail, cfg = ga.login("admin@example.com", "pw")
        assert ok is False
        assert cfg == ""
        assert removed == ["/tmp/cloudsdk-x"]

    def test_a_successful_sign_in_returns_the_cloudsdk_config_for_reuse(self, monkeypatch):
        monkeypatch.setattr(ga.shutil, "which", lambda name: "/usr/bin/gcloud")
        monkeypatch.setattr(ga.tempfile, "mkdtemp", lambda prefix="": "/tmp/cloudsdk-x")
        url_line = ("gcloud auth login --remote-bootstrap="
                    "\"https://accounts.google.com/o/oauth2/auth?foo=bar\"\n")
        # The real gcloud process's own combined stdout is what login()
        # trusts, not _drive_browser's return value -- it reads it back
        # through stdout.read() after _drive_browser returns and the
        # process is wait()ed on.
        fake = _FakeProc([url_line], trailing="You are now logged in as admin@example.com.\n", rc=0)
        monkeypatch.setattr(ga.subprocess, "Popen", lambda *a, **k: fake)
        monkeypatch.setattr(ga, "_drive_browser", lambda *a, **k: None)

        ok, detail, cfg = ga.login("admin@example.com", "pw")
        assert ok is True
        assert cfg == "/tmp/cloudsdk-x"
        assert "logged in as" in detail

    def test_gcloud_exiting_without_the_success_marker_fails_and_cleans_up(self, monkeypatch):
        """A driven browser that clicks through everything but a stalled
        2FA/captcha still left gcloud's own process without the phrase
        that proves the login actually completed -- that, not the browser
        loop's own opinion, is what must gate success."""
        monkeypatch.setattr(ga.shutil, "which", lambda name: "/usr/bin/gcloud")
        removed = []
        monkeypatch.setattr(ga.shutil, "rmtree", lambda d, ignore_errors=False: removed.append(d))
        monkeypatch.setattr(ga.tempfile, "mkdtemp", lambda prefix="": "/tmp/cloudsdk-x")
        url_line = ("gcloud auth login --remote-bootstrap="
                    "\"https://accounts.google.com/o/oauth2/auth?foo=bar\"\n")
        fake = _FakeProc([url_line], trailing="ERROR: timed out waiting for sign-in\n", rc=1)
        monkeypatch.setattr(ga.subprocess, "Popen", lambda *a, **k: fake)
        monkeypatch.setattr(ga, "_drive_browser", lambda *a, **k: None)

        ok, detail, cfg = ga.login("admin@example.com", "pw")
        assert ok is False
        assert cfg == ""
        assert removed == ["/tmp/cloudsdk-x"]

    def test_the_password_is_passed_to_drive_browser_but_never_returned_or_logged(self, monkeypatch):
        monkeypatch.setattr(ga.shutil, "which", lambda name: "/usr/bin/gcloud")
        monkeypatch.setattr(ga.tempfile, "mkdtemp", lambda prefix="": "/tmp/cloudsdk-x")
        url_line = ("gcloud auth login --remote-bootstrap="
                    "\"https://accounts.google.com/o/oauth2/auth?foo=bar\"\n")
        fake = _FakeProc([url_line], trailing="You are now logged in as admin@example.com.\n", rc=0)
        monkeypatch.setattr(ga.subprocess, "Popen", lambda *a, **k: fake)

        seen = {}

        def fake_drive(proc, url, email, password, timeout):
            seen["email"] = email
            seen["password"] = password

        monkeypatch.setattr(ga, "_drive_browser", fake_drive)

        ok, detail, cfg = ga.login("admin@example.com", "super-secret-pw")
        assert seen["email"] == "admin@example.com"
        assert seen["password"] == "super-secret-pw"
        assert "super-secret-pw" not in detail


class TestCleanup:
    def test_revokes_then_removes_the_directory(self, monkeypatch):
        calls = []
        monkeypatch.setattr(ga.subprocess, "run",
                            lambda argv, **kw: calls.append((argv, kw.get("env"))))
        removed = []
        monkeypatch.setattr(ga.shutil, "rmtree", lambda d, ignore_errors=False: removed.append(d))

        ga.cleanup("/tmp/cloudsdk-x")

        assert calls[0][0][:3] == ["gcloud", "auth", "revoke"]
        assert calls[0][1]["CLOUDSDK_CONFIG"] == "/tmp/cloudsdk-x"
        assert removed == ["/tmp/cloudsdk-x"]

    def test_a_failed_revoke_still_removes_the_directory(self, monkeypatch):
        """Best-effort: a network blip on revoke must not strand the
        ephemeral config directory on disk forever."""
        def boom(*a, **k):
            raise OSError("network unreachable")
        monkeypatch.setattr(ga.subprocess, "run", boom)
        removed = []
        monkeypatch.setattr(ga.shutil, "rmtree", lambda d, ignore_errors=False: removed.append(d))

        ga.cleanup("/tmp/cloudsdk-x")
        assert removed == ["/tmp/cloudsdk-x"]

    def test_an_empty_path_is_a_no_op(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(ga.subprocess, "run", lambda *a, **k: called.update(n=called["n"] + 1))
        ga.cleanup("")
        assert called["n"] == 0


class _FakeCheckbox:
    def __init__(self, container_text: str):
        self._text = container_text
        self._checked = False

    def is_visible(self):
        return True

    def locator(self, xpath):
        return self  # container == self is enough for inner_text() below

    def inner_text(self, timeout=None):
        return self._text

    def is_checked(self):
        return self._checked

    def check(self, timeout=None):
        self._checked = True


class _FakeCheckboxLocator:
    def __init__(self, texts: list):
        self._boxes = [_FakeCheckbox(t) for t in texts]

    def count(self):
        return len(self._boxes)

    def nth(self, i):
        return self._boxes[i]


class _FakeButton:
    def __init__(self, matches: bool, page, starts_disabled: bool = False):
        self._matches = matches
        self._page = page
        self._starts_disabled = starts_disabled

    def count(self):
        return 1 if self._matches else 0

    @property
    def first(self):
        return self

    def is_visible(self):
        return True

    def is_enabled(self):
        return (not self._starts_disabled) or self._page.any_checkbox_checked()

    def click(self):
        self._page.clicked = True


class _FakeTosPage:
    """Just enough of a Playwright Page for _accept_cloud_console_tos's
    own logic -- goto, one checkbox locator, one button-by-role lookup.
    The real click-through against Google's actual console DOM is
    exercised live (confirmed against a real, previously-blocked trial
    account), same reasoning as this file's own module docstring.

    The checkbox locator is built ONCE and reused, not rebuilt on every
    `.locator()` call -- otherwise a checkbox checked on one poll of the
    real function's loop would forget it was checked the next time
    around, which a real Playwright locator never does."""

    def __init__(self, checkbox_texts=(), continue_label=None, button_starts_disabled=False):
        self._checkboxes = _FakeCheckboxLocator(list(checkbox_texts))
        self.continue_label = continue_label
        self.button_starts_disabled = button_starts_disabled
        self.clicked = False
        self.goto_calls = []

    def any_checkbox_checked(self) -> bool:
        return any(b.is_checked() for b in self._checkboxes._boxes)

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append(url)

    def locator(self, selector):
        assert selector == 'input[type="checkbox"]'
        return self._checkboxes

    def get_by_role(self, role, name=None):
        assert role == "button"
        return _FakeButton(name == self.continue_label, self,
                           starts_disabled=self.button_starts_disabled)

    def wait_for_timeout(self, ms):
        pass


class TestAcceptCloudConsoleTos:
    def _fast_forward(self, monkeypatch):
        """_accept_cloud_console_tos tries multiple candidate URLs in
        turn, each running its own independent copy of the same
        wait-for-a-checkbox loop with its own freshly-captured `start` --
        a fixed "0 then some big number" fake time.time() only works for
        ONE such loop and spins forever on the second, since every call
        after its own `start` would see zero elapsed time. A steadily
        advancing clock instead guarantees both the 5s "no checkbox at
        all" grace period and the overall `timeout` deadline eventually
        clear, no matter how many times a fresh `start` gets captured."""
        monkeypatch.setattr(ga.time, "sleep", lambda s: None)
        state = {"t": 0.0}

        def fake_time():
            state["t"] += 2.0
            return state["t"]
        monkeypatch.setattr(ga.time, "time", fake_time)

    def test_not_needed_when_no_tos_checkbox_ever_appears(self, monkeypatch):
        """The ordinary case, every time after the very first: every
        candidate URL just loads normally, nothing to accept anywhere."""
        self._fast_forward(monkeypatch)
        page = _FakeTosPage(checkbox_texts=[])
        outcome = ga._accept_cloud_console_tos(page, timeout=30)
        assert outcome == "not_needed"
        assert page.goto_calls == list(ga._TOS_CANDIDATE_URLS)

    def test_unrelated_checkboxes_are_left_alone(self, monkeypatch):
        """A page with SOME checkbox that has nothing to do with the ToS
        (e.g. an unrelated preference) must not be treated as the gate --
        only text actually naming the Terms of Service counts."""
        self._fast_forward(monkeypatch)
        page = _FakeTosPage(checkbox_texts=["Send me product updates"])
        outcome = ga._accept_cloud_console_tos(page, timeout=30)
        assert outcome == "not_needed"

    def test_accepts_when_a_tos_checkbox_and_button_are_present(self, monkeypatch):
        page = _FakeTosPage(
            checkbox_texts=["Yes, I have read and agree to the Terms of Service"],
            continue_label="Agree and continue")
        outcome = ga._accept_cloud_console_tos(page, timeout=30)
        assert outcome == "accepted"
        assert page.clicked is True

    def test_a_button_disabled_until_checked_is_not_clicked_prematurely(self, monkeypatch):
        """Confirmed live against Google's real "New Project" welcome
        modal: "Agree and continue" renders disabled until the ToS
        checkbox above it is checked. The button-only fallback branch
        (for consent pages with no checkbox at all) used to try clicking
        it on the very first pass regardless -- Playwright raises on a
        click against a disabled element, which this must not do, and
        must still reach "accepted" once the checkbox is actually
        checked."""
        page = _FakeTosPage(
            checkbox_texts=["I agree to the Google Cloud Platform Terms of Service"],
            continue_label="Agree and continue", button_starts_disabled=True)
        outcome = ga._accept_cloud_console_tos(page, timeout=30)
        assert outcome == "accepted"
        assert page.clicked is True

    def test_checkbox_found_but_no_matching_button_reports_could_not_find_prompt(self, monkeypatch):
        """A future console redesign changing the button's wording must
        degrade to a clear, distinguishable outcome -- not silently look
        like success."""
        page = _FakeTosPage(
            checkbox_texts=["Terms of Service"], continue_label="Some New Wording")
        outcome = ga._accept_cloud_console_tos(page, timeout=30)
        assert outcome == "could_not_find_prompt"

    def test_a_goto_failure_is_reported_not_raised(self, monkeypatch):
        class BoomPage:
            def goto(self, *a, **k):
                raise Exception("net::ERR_CONNECTION_RESET")  # noqa: TRY002
        assert ga._accept_cloud_console_tos(BoomPage(), timeout=5) == "could_not_find_prompt"


class _FakeNameField:
    def __init__(self, existing_value: str = ""):
        self._value = existing_value
        self.typed = None

    def is_visible(self):
        return True

    def input_value(self):
        return self._value

    def click(self):
        pass

    def type(self, text, delay=None):
        self.typed = text
        self._value = text


class _FakeClickable:
    def __init__(self, visible: bool = True, enabled: bool = True):
        self.visible = visible
        self.enabled = enabled
        self.clicked = False

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def click(self):
        self.clicked = True


class _FakeLocatorResult:
    """Generic count()/first wrapper reused for the name-field locator,
    get_by_text, and get_by_role results alike -- all three follow the
    same Playwright shape."""
    def __init__(self, target=None):
        self._target = target

    def count(self):
        return 1 if self._target is not None else 0

    @property
    def first(self):
        return self._target


class _FakeChatFormPage:
    """Just enough of a Playwright Page for _fill_chat_app_form's own
    logic -- one name-field locator, one status control, one save button.
    The real click-through against Google's actual Chat API config page is
    exercised live once a real tenant needs it, same reasoning as this
    file's own module docstring: a mocked Locator tests the mock, not
    Google's console."""

    def __init__(self, name_field=None, status_label=None, save_label=None,
                save_enabled=True):
        self.name_field = name_field
        self.status_label = status_label
        self.save_label = save_label
        self.status_control = _FakeClickable() if status_label else None
        self.save_button = _FakeClickable(enabled=save_enabled) if save_label else None
        self.url = ("https://console.cloud.google.com/apis/api/"
                   "chat.googleapis.com/hangouts-chat")

    def locator(self, selector):
        return _FakeLocatorResult(self.name_field)

    def get_by_text(self, label, exact=True):
        return _FakeLocatorResult(self.status_control if label == self.status_label else None)

    def get_by_role(self, role, name=None):
        assert role == "button"
        return _FakeLocatorResult(self.save_button if name == self.save_label else None)

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, path=None):
        pass

    def inner_text(self, selector):
        return ""


class TestFillChatAppForm:
    def test_fills_an_empty_name_field_sets_status_and_saves(self):
        page = _FakeChatFormPage(name_field=_FakeNameField(""),
                                 status_label="LIVE", save_label="SAVE")
        ok, detail = ga._fill_chat_app_form(page, "wsmig-src-12345", timeout=30)

        assert ok is True
        assert "wsmig-src-12345" in detail
        assert page.name_field.typed == "wsmig-src-12345 sandbox"
        assert page.status_control.clicked is True
        assert page.save_button.clicked is True

    def test_does_not_overwrite_an_existing_app_name(self):
        """A retry against an already-configured project (every re-run of
        a setup that failed for an unrelated later reason) must not
        clobber a name an operator may have hand-edited since."""
        page = _FakeChatFormPage(name_field=_FakeNameField("My Existing App"),
                                 status_label="LIVE", save_label="SAVE")
        ga._fill_chat_app_form(page, "wsmig-src-12345", timeout=30)
        assert page.name_field.typed is None

    def test_missing_name_field_reports_clearly_without_touching_anything_else(self):
        """A console that does not show the name field must degrade to a
        distinguishable failure, not silently click Save on a blank form.

        The message changed once the Configuration tab was understood: this
        fake renders neither the tab nor the field, which is now reported as
        the tab being absent rather than as a form redesign. That is the
        more accurate of the two -- the old wording sent readers looking for
        a console change when the real cause was a missing click.
        """
        page = _FakeChatFormPage(name_field=None, status_label="LIVE", save_label="SAVE")
        ok, detail = ga._fill_chat_app_form(page, "wsmig-src-12345", timeout=30)

        assert ok is False
        assert "Configuration tab" in detail or "app name field" in detail
        # The part that actually matters and has not changed.
        assert page.status_control.clicked is False
        assert page.save_button.clicked is False

    def test_no_save_button_reports_the_form_was_filled_but_not_saved(self):
        page = _FakeChatFormPage(name_field=_FakeNameField(""),
                                 status_label="LIVE", save_label=None)
        ok, detail = ga._fill_chat_app_form(page, "wsmig-src-12345", timeout=30)

        assert ok is False
        assert "Save" in detail

    def test_a_disabled_save_button_is_not_clicked(self):
        page = _FakeChatFormPage(name_field=_FakeNameField(""), status_label="LIVE",
                                 save_label="SAVE", save_enabled=False)
        ok, detail = ga._fill_chat_app_form(page, "wsmig-src-12345", timeout=30)

        assert ok is False
        assert page.save_button.clicked is False

    def test_no_status_control_found_still_proceeds_to_save(self):
        """The status may already read LIVE from a prior successful run --
        finding no matching label must not block Save on that basis alone."""
        page = _FakeChatFormPage(name_field=_FakeNameField(""),
                                 status_label=None, save_label="SAVE")
        ok, _ = ga._fill_chat_app_form(page, "wsmig-src-12345", timeout=30)

        assert ok is True
        assert page.save_button.clicked is True
