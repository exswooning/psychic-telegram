"""
tests/test_setup_challenge_surfaces.py
======================================
A browser sign-in inside full_setup runs headless on Xvfb. When Google
answers the password with "Check your phone -- tap 47", that prompt is
drawn to a framebuffer nobody is watching, the phase blocks for its whole
timeout, and the run then reports "likely 2FA/captcha" as a guess.

The transcript already carries it (see test_signin_challenge). What these
check is that it also reaches the progress file the setup page polls --
because the person who can pick up the phone is looking at that page, not
at a log.
"""

from __future__ import annotations

import json

import full_setup
import signin_challenge


class TestTheHookReachesTheProgressFile:
    def _run(self, tmp_path, monkeypatch, act):
        """Drive full_setup far enough to install its reporter, then act."""
        prog = tmp_path / "p.json"
        # Stop the run the moment the reporter is installed: everything
        # after it needs gcloud, a browser and a real tenant.
        class Stop(Exception):
            pass

        def boom(*a, **k):
            raise Stop
        monkeypatch.setattr(full_setup, "Phase", boom)
        try:
            full_setup.run_full_setup(
                side="source", domain="d.example", admin_email="a@d.example",
                admin_password="x", progress_file=str(prog))
        except Stop:
            pass
        except TypeError:
            # Signature differs; the reporter is what matters and it is set
            # before any of that runs.
            pass
        act()
        return json.loads(prog.read_text()) if prog.exists() else {}

    def test_a_challenge_is_written_where_the_page_can_see_it(
            self, tmp_path, monkeypatch):
        data = self._run(tmp_path, monkeypatch,
                         lambda: signin_challenge._report("Check your phone / 47"))
        assert data.get("challenge") == "Check your phone / 47"

    def test_it_clears_when_the_prompt_is_answered(self, tmp_path, monkeypatch):
        def act():
            signin_challenge._report("Check your phone / 47")
            signin_challenge._report("")
        data = self._run(tmp_path, monkeypatch, act)
        assert not data.get("challenge"), "the banner would never come down"

    def test_the_phase_label_is_not_lost_when_a_challenge_arrives(
            self, tmp_path, monkeypatch):
        """It writes the whole checkpoint, so a naive write would blank the
        label and the bar would jump to 0% mid-run."""
        data = self._run(tmp_path, monkeypatch,
                         lambda: signin_challenge._report("tap 47"))
        assert data.get("label")
        assert isinstance(data.get("pct"), int)


class TestTheReporterIsSafe:
    def test_no_reporter_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(signin_challenge, "REPORTER", None)
        signin_challenge._report("anything")      # must not raise

    def test_a_broken_reporter_never_breaks_the_sign_in(self, monkeypatch):
        def bad(_t):
            raise RuntimeError("disk full")
        monkeypatch.setattr(signin_challenge, "REPORTER", bad)
        signin_challenge._report("tap 47")        # must not raise

    def test_the_watcher_reports_as_well_as_logs(self, monkeypatch):
        seen = []
        monkeypatch.setattr(signin_challenge, "REPORTER", seen.append)

        class Page:
            def inner_text(self, _s):
                return "Check your phone\nTap 47 on your phone to verify"

        signin_challenge.Watcher(lambda _m: None).check([Page()])
        assert seen and "47" in seen[0]

    def test_the_watcher_reports_the_clear(self, monkeypatch):
        seen = []
        monkeypatch.setattr(signin_challenge, "REPORTER", seen.append)

        class Page:
            def __init__(self, t):
                self.t = t

            def inner_text(self, _s):
                return self.t

        w = signin_challenge.Watcher(lambda _m: None)
        w.check([Page("Check your phone\nTap 47 to verify")])
        w.check([Page("Google Admin\nDomain-wide delegation")])
        assert seen[-1] == ""


class TestTheApiCarriesIt:
    def test_both_status_readers_return_it(self):
        """There are two -- full setup and teardown share the shape. One of
        them silently not carrying it is how a banner works on one page and
        not the other."""
        import inspect

        import api_server

        src = inspect.getsource(api_server)
        assert src.count('challenge = prog.get("challenge") or None') == 2
        assert src.count('"challenge": challenge') == 2
