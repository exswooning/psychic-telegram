"""Emptying the source is sometimes the intent, and must stay guarded.

Reseeding under different usernames needs the old accounts GONE, not
merely emptied -- otherwise the new identity map points at users that
still exist with the old corpus attached. wipe_target could only ever
delete from the target, so that was an SSH job.

Every guard still applies, just against the domain actually being
destroyed. The one that changes shape is the same-domain check: against
the target it meant "you are about to delete the corpus you are migrating",
and it can no longer double as a proxy for "this is the target", because
now it might not be.
"""
import subprocess
import sys
import os

import pytest

import reset_target

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _S:
    source_domain = "a.example"
    target_domain = "b.example"


def _guard(monkeypatch, confirm, side, sandbox="true", protected=""):
    monkeypatch.setenv("SANDBOX_MODE", sandbox)
    monkeypatch.setenv("PROTECTED_DOMAINS", protected)
    with pytest.raises(SystemExit) as e:
        reset_target.assert_sandbox(_S(), confirm, side)
    return str(e.value)


class TestTheGuardFollowsTheSide:
    def test_source_accepts_the_source_domain(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_MODE", "true")
        monkeypatch.setenv("PROTECTED_DOMAINS", "")
        reset_target.assert_sandbox(_S(), "a.example", "source")   # no raise

    def test_target_still_accepts_the_target_domain(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_MODE", "true")
        monkeypatch.setenv("PROTECTED_DOMAINS", "")
        reset_target.assert_sandbox(_S(), "b.example", "target")   # no raise

    def test_the_wrong_domain_is_refused_on_the_source_side(self, monkeypatch):
        msg = _guard(monkeypatch, "b.example", "source")
        assert "does not match SOURCE_DOMAIN" in msg

    def test_the_wrong_domain_is_refused_on_the_target_side(self, monkeypatch):
        msg = _guard(monkeypatch, "a.example", "target")
        assert "does not match TARGET_DOMAIN" in msg

    def test_sandbox_mode_is_still_required(self, monkeypatch):
        assert "SANDBOX_MODE" in _guard(monkeypatch, "a.example", "source",
                                        sandbox="")

    def test_protected_domains_still_applies(self, monkeypatch):
        msg = _guard(monkeypatch, "a.example", "source", protected="a.example")
        assert "PROTECTED_DOMAINS" in msg

    def test_one_domain_for_both_tenants_is_refused_either_way(self, monkeypatch):
        class _Same:
            source_domain = target_domain = "same.example"

        monkeypatch.setenv("SANDBOX_MODE", "true")
        monkeypatch.setenv("PROTECTED_DOMAINS", "")
        for side in ("source", "target"):
            with pytest.raises(SystemExit) as e:
                reset_target.assert_sandbox(_Same(), "same.example", side)
            assert "same" in str(e.value)

    def test_an_unknown_side_is_a_programming_error(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_MODE", "true")
        with pytest.raises(ValueError):
            reset_target.assert_sandbox(_S(), "a.example", "sideways")


class TestTheDefaultIsUnchanged:
    def test_side_defaults_to_target(self):
        import inspect
        assert inspect.signature(
            reset_target.assert_sandbox).parameters["side"].default == "target"

    def test_the_cli_defaults_to_target(self):
        src = open(os.path.join(ROOT, "wipe_target.py"), encoding="utf-8").read()
        block = src.split('"--side"')[1][:160]
        assert 'default="target"' in block

    def test_the_cli_says_what_source_costs(self):
        src = open(os.path.join(ROOT, "wipe_target.py"), encoding="utf-8").read()
        block = src.split('"--side"')[1][:400]
        assert "destroys the corpus" in block


class TestItRefusesEndToEnd:
    """The guards run in the real process, not just in-module."""

    def _run(self, env, *args):
        e = dict(os.environ, **env)
        return subprocess.run([sys.executable, "wipe_target.py", *args],
                              cwd=ROOT, capture_output=True, text=True, env=e)

    def test_a_mistyped_source_domain_stops_it(self):
        out = self._run({"SANDBOX_MODE": "true", "SOURCE_DOMAIN": "a.example",
                         "TARGET_DOMAIN": "b.example", "PROTECTED_DOMAINS": ""},
                        "--confirm-domain", "typo.example", "--side", "source")
        assert "REFUSING" in (out.stdout + out.stderr)

    def test_no_sandbox_mode_stops_it(self):
        out = self._run({"SANDBOX_MODE": "", "SOURCE_DOMAIN": "a.example",
                         "TARGET_DOMAIN": "b.example"},
                        "--confirm-domain", "a.example", "--side", "source")
        assert "SANDBOX_MODE" in (out.stdout + out.stderr)


class TestTheSourceButtonIsItsOwnButton:
    """Not a checkbox on the target card.

    These are not variations of one action. Emptying the target is routine
    between rehearsals; emptying the source destroys the corpus. A shared
    control with a mode switch is how muscle memory eventually fires the
    wrong one.
    """

    def _cfg(self, monkeypatch):
        import config
        real = config.Settings

        def fake(account_id=None, **kw):
            st = real.__new__(real)
            st.source_domain, st.target_domain = "src.example", "tgt.example"
            st.source_admin, st.source_sa_key = "a@src.example", "/k/s.json"
            st.target_admin, st.target_sa_key = "a@tgt.example", "/k/t.json"
            st.db_path, st.account_id = "/tmp/m.db", account_id
            return st

        monkeypatch.setattr(config, "Settings", fake)
        monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)

    def test_it_wants_the_source_domain(self, monkeypatch):
        import webui
        self._cfg(monkeypatch)
        argv, env, err = webui.wipe_source_argv({"confirm_domain": "src.example"}, 7)
        assert not err
        assert "--side" in argv and argv[argv.index("--side") + 1] == "source"

    def test_typing_the_target_domain_is_refused_and_explained(self, monkeypatch):
        # The dangerous confusion, named explicitly rather than a bare
        # "does not match".
        import webui
        self._cfg(monkeypatch)
        err = webui.wipe_source_argv({"confirm_domain": "tgt.example"}, 7)[2]
        assert "TARGET domain" in err and "SOURCE corpus" in err

    def test_protected_domains_applies_here_too(self, monkeypatch):
        import webui
        self._cfg(monkeypatch)
        monkeypatch.setenv("PROTECTED_DOMAINS", "src.example")
        assert "PROTECTED_DOMAINS" in webui.wipe_source_argv(
            {"confirm_domain": "src.example"}, 7)[2]

    def test_the_target_button_still_wants_the_target_domain(self, monkeypatch):
        import webui
        self._cfg(monkeypatch)
        assert not webui.wipe_target_argv({"confirm_domain": "tgt.example"}, 7)[2]
        assert webui.wipe_target_argv({"confirm_domain": "src.example"}, 7)[2]

    def test_the_two_cards_are_separate(self):
        src = open(os.path.join(ROOT, "migration-webui/src/pages/Maintenance.tsx"),
                   encoding="utf-8").read()
        assert "wipe-source-domain" in src and "wipe-target-domain" in src
        assert src.count("const WipeSourceCard") == 1

    def test_the_source_card_says_what_it_destroys(self):
        src = open(os.path.join(ROOT, "migration-webui/src/pages/Maintenance.tsx"),
                   encoding="utf-8").read()
        # Normalised: JSX wraps prose across lines, so a literal match on a
        # sentence is a test that fails on reformatting rather than on
        # meaning.
        import re
        card = re.sub(r"\s+", " ", src.split("const WipeSourceCard")[1])
        assert "corpus this migration exists to move" in card
        assert "20 days" in card, "the domain-limit cost must be on the button"
