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
