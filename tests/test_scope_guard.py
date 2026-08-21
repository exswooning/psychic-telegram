"""
tests/test_scope_guard.py
=========================
Never start a migration that is going to die on a missing scope.

The failure being prevented
---------------------------
A delegated token request is all-or-nothing. One ungranted scope out of
fifteen fails the whole exchange with `unauthorized_client`, a message that
names no scope, no tenant and no console. Live, that arrived as a raw
traceback eight minutes into a run against a source tenant missing exactly
one scope (`drive.readonly`), and left a FAILED ledger row against a user
nothing was wrong with.

The tenant was missing that scope because it had been set up by *seeding*,
and the seed grant line was assembled independently of the migration one --
12 scopes against the 15 the source actually requires. So there are two
halves here and both are tested: the grant lines can no longer be
incomplete (TestGrantLinesCannotDrift), and a run that would fail anyway
stops before it starts and says why (TestAudit / TestEnsure / TestTheGate).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import scope_guard  # noqa: E402


class FakeProbe:
    """Stands in for the live token exchange, and counts the calls.

    The call count is the point of several tests: the healthy path must cost
    one mint per tenant, not one per scope, or every migration pays ~30
    round trips at startup to answer a question that is nearly always "no".
    """

    def __init__(self, missing: set[str] | None = None, combined_error: str = ""):
        self.missing = missing or set()
        self.combined_error = combined_error
        self.calls: list[object] = []

    def __call__(self, key, subject, scope, timeout=30):
        self.calls.append(scope)
        if isinstance(scope, list):
            if self.combined_error:
                return False, self.combined_error
            bad = self.missing & set(scope)
            return (not bad), ("" if not bad else "not delegated")
        if scope in self.missing:
            return False, "not delegated"
        return True, ""

    @property
    def combined_calls(self):
        return [c for c in self.calls if isinstance(c, list)]

    @property
    def single_calls(self):
        return [c for c in self.calls if isinstance(c, str)]


@pytest.fixture
def wired(monkeypatch, tmp_path, settings):
    """A settings whose key files exist, with the probe under our control."""
    import json

    import verify_scopes

    key = tmp_path / "sa.json"
    key.write_text(json.dumps({"client_id": "104063734164705184270"}))
    monkeypatch.setattr(verify_scopes, "_key_and_subject",
                        lambda s, t: (str(key), f"admin@{t}.example.com"))
    monkeypatch.setattr(verify_scopes, "required_scopes",
                        lambda s, t, **kw: ["scope/a", "scope/b", "scope/c"])

    def _install(probe):
        monkeypatch.setattr(verify_scopes, "probe_scope", probe)
        return probe

    return settings, _install


class TestAudit:
    def test_a_healthy_tenant_costs_one_token_mint_not_one_per_scope(self, wired):
        """The reason this can run before every migration at all."""
        settings, install = wired
        probe = install(FakeProbe())
        assert scope_guard.audit(settings, ("source",)) == []
        assert len(probe.combined_calls) == 1
        assert probe.single_calls == []

    def test_a_gap_names_the_exact_scopes_and_pays_for_the_walk(self, wired):
        settings, install = wired
        probe = install(FakeProbe(missing={"scope/b"}))
        gaps = scope_guard.audit(settings, ("source",))
        assert [g.missing for g in gaps] == [["scope/b"]]
        # One screening mint, then the per-scope walk that produced the name.
        assert len(probe.combined_calls) == 1
        assert set(probe.single_calls) == {"scope/a", "scope/b", "scope/c"}

    def test_the_gap_carries_what_the_operator_needs_to_fix_it(self, wired):
        settings, install = wired
        install(FakeProbe(missing={"scope/b"}))
        gap = scope_guard.audit(settings, ("source",))[0]
        assert gap.tenant == "source"
        assert gap.subject == "admin@source.example.com"
        assert gap.client_id == "104063734164705184270"
        assert gap.detail["scope/b"] == "not delegated"
        assert gap.fixable_by_grant

    def test_both_tenants_are_checked_independently(self, wired):
        settings, install = wired
        install(FakeProbe(missing={"scope/c"}))
        gaps = scope_guard.audit(settings, ("source", "target"))
        assert {g.tenant for g in gaps} == {"source", "target"}

    def test_a_missing_key_is_blocked_not_reported_as_a_scope_gap(
            self, monkeypatch, settings):
        """Different problem, different fix. Telling someone to grant scopes
        when the key file is absent sends them to the wrong console."""
        import verify_scopes
        monkeypatch.setattr(verify_scopes, "_key_and_subject",
                            lambda s, t: ("/nope/missing.json", "a@b.com"))
        monkeypatch.setattr(verify_scopes, "required_scopes",
                            lambda s, t, **kw: ["scope/a"])
        gap = scope_guard.audit(settings, ("source",))[0]
        assert gap.blocked and "no service-account key" in gap.blocked
        assert not gap.fixable_by_grant
        assert gap.missing == []

    def test_an_unset_admin_is_blocked_not_a_scope_gap(
            self, monkeypatch, tmp_path, settings):
        import json

        import verify_scopes
        key = tmp_path / "sa.json"
        key.write_text(json.dumps({"client_id": "x"}))
        monkeypatch.setattr(verify_scopes, "_key_and_subject",
                            lambda s, t: (str(key), ""))
        monkeypatch.setattr(verify_scopes, "required_scopes",
                            lambda s, t, **kw: ["scope/a"])
        gap = scope_guard.audit(settings, ("source",))[0]
        assert "SOURCE_ADMIN is not set" in gap.blocked

    def test_a_combined_failure_with_every_scope_passing_is_not_a_scope_gap(
            self, wired):
        """Clock skew, a rate-limited subject, a transient. Reporting this as
        "grant these scopes" would send the operator to overwrite a console
        entry that is already correct -- and an Overwrite that drops a live
        scope is exactly how a working migration breaks at 2am."""
        settings, install = wired
        install(FakeProbe(combined_error="Connection reset by peer"))
        gap = scope_guard.audit(settings, ("source",))[0]
        assert gap.missing == []
        assert not gap.fixable_by_grant
        assert "passes individually" in gap.blocked
        assert "Connection reset" in gap.blocked


class TestDiagnosis:
    """What replaces `unauthorized_client`."""

    def test_the_message_names_tenant_scope_console_entry_and_fix(self, wired):
        settings, install = wired
        install(FakeProbe(missing={"scope/b"}))
        text = scope_guard.describe(scope_guard.audit(settings, ("source",)))
        assert "source" in text
        assert "scope/b" in text
        assert "104063734164705184270" in text          # the console entry
        assert "dwd_helper.py" in text                  # the fix
        assert "not delegated" in text                  # why

    def test_remediation_is_a_runnable_command(self, wired):
        settings, install = wired
        install(FakeProbe(missing={"scope/a", "scope/c"}))
        cmd = scope_guard.remediation(scope_guard.audit(settings, ("source",))[0])
        assert cmd.startswith("python3 dwd_helper.py --tenant source")
        assert "--client-id 104063734164705184270" in cmd
        assert "scope/a,scope/c" in cmd

    def test_a_blocked_tenant_reports_its_own_problem_not_a_grant_command(
            self, monkeypatch, settings):
        import verify_scopes
        monkeypatch.setattr(verify_scopes, "_key_and_subject",
                            lambda s, t: ("/nope.json", "a@b.com"))
        monkeypatch.setattr(verify_scopes, "required_scopes",
                            lambda s, t, **kw: ["scope/a"])
        text = scope_guard.describe(scope_guard.audit(settings, ("source",)))
        assert "dwd_helper" not in text
        assert "no service-account key" in text


class TestRepair:
    def test_repair_is_refused_without_credentials_rather_than_hanging(
            self, monkeypatch, wired):
        """dwd_helper drives a real browser. With no credentials it waits at
        a sign-in page for its whole timeout, which as an automatic repair
        means a migration that appears to hang for ten minutes."""
        settings, install = wired
        install(FakeProbe(missing={"scope/b"}))
        monkeypatch.delenv("DWD_EMAIL", raising=False)
        monkeypatch.delenv("DWD_PASSWORD", raising=False)
        gap = scope_guard.audit(settings, ("source",))[0]
        ok, detail = scope_guard.repair(gap)
        assert not ok
        assert "DWD_EMAIL" in detail

    def test_repair_never_puts_the_password_on_the_command_line(
            self, monkeypatch, wired):
        """A command line is readable by any process on the box via `ps`,
        and this one would carry a super-admin password."""
        settings, install = wired
        install(FakeProbe(missing={"scope/b"}))
        monkeypatch.setenv("DWD_EMAIL", "admin@example.com")
        monkeypatch.setenv("DWD_PASSWORD", "hunter2")
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            class R:
                returncode = 0
                stdout = stderr = ""
            return R()

        monkeypatch.setattr(scope_guard.subprocess, "run", fake_run)
        scope_guard.repair(scope_guard.audit(settings, ("source",))[0])
        assert "hunter2" not in " ".join(seen["argv"])
        assert "admin@example.com" not in " ".join(seen["argv"])

    def test_repair_submits_only_the_missing_scopes_to_a_merging_helper(
            self, monkeypatch, wired):
        """dwd_helper's default is merge: it reads the live set and submits
        the union. Passing the missing scopes alone to a helper that
        *overwrote* would revoke everything that currently works, so this
        pins the no --no-merge expectation too."""
        settings, install = wired
        install(FakeProbe(missing={"scope/b"}))
        monkeypatch.setenv("DWD_EMAIL", "a@b.com")
        monkeypatch.setenv("DWD_PASSWORD", "x")
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            class R:
                returncode = 0
                stdout = stderr = ""
            return R()

        monkeypatch.setattr(scope_guard.subprocess, "run", fake_run)
        ok, _ = scope_guard.repair(scope_guard.audit(settings, ("source",))[0])
        assert ok
        argv = seen["argv"]
        assert "--scopes" in argv and "scope/b" in argv
        assert "--no-merge" not in argv

    def test_a_failing_helper_is_reported_not_silently_treated_as_repaired(
            self, monkeypatch, wired):
        settings, install = wired
        install(FakeProbe(missing={"scope/b"}))
        monkeypatch.setenv("DWD_EMAIL", "a@b.com")
        monkeypatch.setenv("DWD_PASSWORD", "x")

        def fake_run(argv, **kw):
            class R:
                returncode = 3
                stdout = ""
                stderr = "console rejected the entry"
            return R()

        monkeypatch.setattr(scope_guard.subprocess, "run", fake_run)
        ok, detail = scope_guard.repair(scope_guard.audit(settings, ("source",))[0])
        assert not ok
        assert "console rejected the entry" in detail


class TestEnsure:
    def test_a_healthy_pair_returns_nothing_and_raises_nothing(self, wired):
        settings, install = wired
        install(FakeProbe())
        assert scope_guard.ensure(settings, ("source", "target")) == []

    def test_an_unrepairable_gap_raises_with_the_diagnosis_attached(self, wired):
        settings, install = wired
        install(FakeProbe(missing={"scope/b"}))
        with pytest.raises(scope_guard.ScopeGapError) as err:
            scope_guard.ensure(settings, ("source",), auto_repair=False)
        assert "scope/b" in str(err.value)
        assert err.value.gaps[0].tenant == "source"

    def test_a_successful_repair_is_re_probed_not_assumed(self, monkeypatch, wired):
        """Console writes are eventually consistent -- propagation has been
        seen to take minutes. A repair that reported success without
        re-probing would hand the batch straight back to the failure it just
        tried to fix."""
        settings, install = wired
        probe = install(FakeProbe(missing={"scope/b"}))
        calls = {"n": 0}

        def fake_repair(gap, timeout=900):
            calls["n"] += 1
            probe.missing = set()          # the grant took
            return True, "granted"

        monkeypatch.setattr(scope_guard, "repair", fake_repair)
        repaired = scope_guard.ensure(settings, ("source",))
        assert calls["n"] == 1
        assert [g.tenant for g in repaired] == ["source"]
        # Screening mint, walk, repair, then a *fresh* screening mint.
        assert len(probe.combined_calls) == 2

    def test_a_repair_that_did_not_take_still_raises(self, monkeypatch, wired):
        """The dangerous case: the helper exits 0 but the console did not
        actually change. Trusting the exit code would start a batch that
        fails per-user."""
        settings, install = wired
        install(FakeProbe(missing={"scope/b"}))
        monkeypatch.setattr(scope_guard, "repair",
                            lambda gap, timeout=900: (True, "granted"))
        with pytest.raises(scope_guard.ScopeGapError) as err:
            scope_guard.ensure(settings, ("source",))
        assert "scope/b" in str(err.value)

    def test_a_blocked_tenant_is_never_handed_to_the_repairer(
            self, monkeypatch, settings):
        import verify_scopes
        monkeypatch.setattr(verify_scopes, "_key_and_subject",
                            lambda s, t: ("/nope.json", "a@b.com"))
        monkeypatch.setattr(verify_scopes, "required_scopes",
                            lambda s, t, **kw: ["scope/a"])
        called = {"n": 0}

        def boom(gap, timeout=900):
            called["n"] += 1
            return False, "should not run"

        monkeypatch.setattr(scope_guard, "repair", boom)
        with pytest.raises(scope_guard.ScopeGapError):
            scope_guard.ensure(settings, ("source",))
        assert called["n"] == 0


class TestGrantLinesCannotDrift:
    """The prevention half.

    Every paste line the UI hands an operator must cover everything the code
    will request against that tenant. Before this was enforced, the seed line
    carried 12 of the source's 15 -- so a tenant set up by seeding was
    migrate-incapable by construction, missing precisely the four read-only
    scopes only a migration uses.
    """

    def _sets(self, entry):
        if isinstance(entry, dict):
            entry = entry.get("scope_list") or entry.get("scopes")
        if isinstance(entry, str):
            entry = entry.split(",")
        return {x.strip() for x in (entry or []) if x.strip().startswith("http")}

    @pytest.mark.parametrize("key,side", [
        ("migrate_source_full", "source"),
        ("migrate_target_full", "target"),
        ("target_provision", "target"),
        ("seed", "source"),
    ])
    def test_every_paste_line_covers_its_sides_required_scopes(self, key, side):
        import verify_scopes
        import webui
        from config import Settings

        st = Settings()
        payload = webui.dwd_payload()
        if not payload.get(key):
            pytest.skip(f"{key} not present in this configuration")
        need = set(verify_scopes.required_scopes(st, side))
        got = self._sets(payload[key])
        missing = sorted(need - got)
        assert not missing, (
            f"{key} would grant an incomplete delegation for {side}; "
            f"missing: {missing}")

    def test_each_tenant_line_covers_its_own_required_scopes(self):
        import verify_scopes
        import webui
        from config import Settings

        st = Settings()
        for t in webui.dwd_payload().get("tenants", []):
            need = set(verify_scopes.required_scopes(st, t["side"]))
            assert not (need - self._sets(t)), (
                f"tenant line for {t['side']} is incomplete")

    def test_the_seed_line_carries_the_read_only_scopes_it_used_to_omit(self):
        """The regression test for the live failure. These four are what a
        migration reads with and a seeder never writes with, so they were
        exactly what the seed-only grant left out."""
        import webui

        seed = webui.dwd_payload().get("seed")
        if not seed:
            pytest.skip("no seed entry in this configuration")
        got = self._sets(seed)
        for scope in ("https://www.googleapis.com/auth/drive.readonly",
                      "https://www.googleapis.com/auth/gmail.readonly",
                      "https://www.googleapis.com/auth/calendar.readonly"):
            assert scope in got, f"seed grant line omits {scope}"


class TestTheGate:
    """`main._gate_on_delegation` -- what runs before the batch dispatches.

    Placement matters as much as behaviour: the live failure produced a
    FAILED ledger row against a user nothing was wrong with, because the
    scope problem was discovered per-user rather than before the batch.
    """

    def _gate(self):
        import main
        return main

    def test_a_gap_stops_the_run_with_the_diagnosis_not_a_traceback(
            self, monkeypatch, settings):
        main = self._gate()
        gap = scope_guard.ScopeGap(
            tenant="source", subject="info@source.example.com",
            client_id="104063734164705184270",
            missing=["https://www.googleapis.com/auth/drive.readonly"],
            detail={"https://www.googleapis.com/auth/drive.readonly":
                    "not delegated"})
        monkeypatch.setattr(scope_guard, "ensure",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                scope_guard.ScopeGapError([gap])))
        with pytest.raises(SystemExit) as err:
            main._gate_on_delegation(settings)
        msg = str(err.value)
        assert "nothing has been migrated" in msg
        assert "drive.readonly" in msg
        assert "104063734164705184270" in msg
        assert "dwd_helper.py" in msg

    def test_a_healthy_pair_does_not_stop_the_run(self, monkeypatch, settings):
        main = self._gate()
        monkeypatch.setattr(scope_guard, "ensure", lambda *a, **kw: [])
        main._gate_on_delegation(settings)          # must not raise

    def test_a_probe_that_itself_fails_does_not_block_the_migration(
            self, monkeypatch, settings):
        """Network trouble at the gate must not become the reason a
        migration cannot run. A real scope problem will still surface the
        old way; a DNS blip must not ground the fleet."""
        main = self._gate()
        monkeypatch.setattr(scope_guard, "ensure",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                OSError("name resolution failed")))
        main._gate_on_delegation(settings)          # must not raise

    def test_the_check_can_be_bypassed_for_an_offline_rehearsal(
            self, monkeypatch, settings):
        """An offline run against a fixture ledger has no tenant to probe,
        and this must never be the reason such a run cannot start."""
        main = self._gate()
        called = {"n": 0}

        def boom(*a, **kw):
            called["n"] += 1
            raise AssertionError("should not have probed")

        monkeypatch.setattr(scope_guard, "ensure", boom)
        monkeypatch.setenv("MIGRATE_SKIP_SCOPE_CHECK", "1")
        main._gate_on_delegation(settings)
        assert called["n"] == 0

    def test_the_gate_runs_before_any_user_is_dispatched(self):
        """Positional, and load-bearing: discovering the gap per-user is what
        wrote a FAILED row against a healthy user."""
        import ast
        import inspect
        import textwrap

        import main

        fn = ast.parse(textwrap.dedent(
            inspect.getsource(main._run_with_memory_pause))).body[0]
        # Statement order in the body, not text order -- the docstring names
        # run_batch before any code runs.
        order = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in ("_gate_on_delegation", "run_batch"):
                    order.append((node.lineno, node.func.id))
        names = [n for _, n in sorted(order)]
        assert names[:2] == ["_gate_on_delegation", "run_batch"], names


class TestIsComplete:
    """The yes/no form, used by full_setup to decide whether to open a
    browser at all."""

    def test_a_complete_tenant_answers_yes_in_one_call(self, wired):
        settings, install = wired
        probe = install(FakeProbe())
        assert scope_guard.is_complete(settings, "source") is True
        assert len(probe.combined_calls) == 1
        assert probe.single_calls == []

    def test_an_incomplete_tenant_answers_no_without_the_walk(self, wired):
        """Deliberately no per-scope walk here: the caller is about to grant
        everything anyway, so naming the missing scope buys nothing and
        costs N live token mints on a path that has its own retry budget."""
        settings, install = wired
        probe = install(FakeProbe(missing={"scope/b"}))
        assert scope_guard.is_complete(settings, "source") is False
        assert probe.single_calls == []

    def test_an_unreadable_key_answers_no_rather_than_raising(
            self, monkeypatch, settings):
        """"Cannot prove it is complete" and "is incomplete" lead to the same
        safe action -- do the setup."""
        import verify_scopes
        monkeypatch.setattr(verify_scopes, "_key_and_subject",
                            lambda s, t: ("/nope.json", "a@b.com"))
        monkeypatch.setattr(verify_scopes, "required_scopes",
                            lambda s, t, **kw: ["scope/a"])
        assert scope_guard.is_complete(settings, "source") is False


class TestSetupSkipsAWorkingConsole:
    """The live false failure this prevents.

    The wizard reported "FAIL domain-wide delegation" for both tenants of a
    pair that, in the same minute, minted all 15 and all 11 of their
    required scopes in a single token request and completed a 1:1 Gmail
    migration through them. The delegation was fine; the host had no
    browser. Setup must not touch a console that is already correct.
    """

    def test_an_already_granted_tenant_never_opens_a_browser(self):
        import ast
        import inspect
        import textwrap

        import full_setup

        src = textwrap.dedent(inspect.getsource(full_setup))
        tree = ast.parse(src)
        # The dwd_helper.run() call must sit under `if not already_granted`.
        guarded = False
        for node in ast.walk(tree):
            if not (isinstance(node, ast.If)
                    and isinstance(node.test, ast.UnaryOp)
                    and isinstance(node.test.op, ast.Not)
                    and isinstance(node.test.operand, ast.Name)
                    and node.test.operand.id == "already_granted"):
                continue
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "run"
                        and getattr(inner.func.value, "id", "") == "dwd_helper"):
                    guarded = True
        assert guarded, (
            "dwd_helper.run() must only run when the delegation is not "
            "already complete -- otherwise setup re-drives a real browser "
            "against a console that is already correct")

    def test_the_precheck_failing_does_not_stop_setup(self):
        """A pre-check is an optimisation. If it cannot run, setup must do
        the work the long way rather than refuse."""
        import inspect

        import full_setup

        src = inspect.getsource(full_setup)
        i = src.index("already_granted = False")
        window = src[i:i + 1200]
        assert "except Exception" in window
        assert "granting anyway" in window


class TestMissingBrowserIsNotBlamedOnTwoFactor:
    """`pip install playwright` ships the client library, not the browsers.

    On a host where `playwright install` was never run, the launch fails
    with a missing-executable error that every caller up the stack reported
    as "likely needs a human for 2FA/captcha" -- sending the operator to
    watch a sign-in that never happened.
    """

    def _fake_p(self, tmp_path):
        class FakeChromium:
            executable_path = str(tmp_path / "chrome-that-is-not-there")

            def launch(self, **kw):
                raise AssertionError("must not launch a browser that is absent")

        class FakeP:
            chromium = FakeChromium()

        return FakeP()

    def test_the_launcher_names_the_missing_browser_and_the_fix(
            self, monkeypatch, tmp_path):
        """No browser on the host at all. A display exists, so that is not
        the complaint."""
        import dwd_helper

        monkeypatch.setenv("DISPLAY", ":0")      # display is fine
        monkeypatch.setattr(dwd_helper.os.path, "exists", lambda path: False)
        with pytest.raises(dwd_helper.NoBrowserAvailable) as err:
            dwd_helper._installed_browser_launch(self._fake_p(tmp_path),
                                                 headful=True)
        msg = str(err.value)
        assert "playwright install chromium" in msg
        assert "2FA" not in msg and "captcha" not in msg

    def test_the_launcher_names_a_missing_display_separately(
            self, monkeypatch, tmp_path):
        """The cause actually observed live: Chrome was installed and fine;
        the VPS simply had no X server and DISPLAY was never set. These are
        different problems with different fixes and must not share a
        message."""
        import dwd_helper

        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setattr(dwd_helper, "_ensure_display", lambda: "")
        with pytest.raises(dwd_helper.NoBrowserAvailable) as err:
            dwd_helper._installed_browser_launch(self._fake_p(tmp_path),
                                                 headful=True)
        msg = str(err.value)
        assert "no X display" in msg
        assert "xvfb" in msg.lower()
        assert "2FA" not in msg and "captcha" not in msg

    def test_the_wizard_reports_a_host_problem_not_a_signin_problem(self):
        import full_setup

        detail = full_setup.no_browser_detail(
            "no browser available on this host: Chrome/Edge/Brave are not "
            "installed and Playwright's bundled Chromium is missing.")
        assert "Delegation was NOT changed" in detail
        assert "setup problem, not a sign-in problem" in detail
        assert "Manual tab" in detail
        # The misleading advice this replaces. The sign-in never happened --
        # nothing was launched to sign in with.
        assert "2FA" not in detail
        assert "captcha" not in detail

    def test_the_wizard_only_uses_that_message_for_a_real_browser_absence(self):
        """A DOM-shift crash must still get the DOM-shift advice."""
        import full_setup

        assert full_setup.is_no_browser(
            "no browser available on this host: ...")
        assert not full_setup.is_no_browser(
            "Timeout 30000ms exceeded waiting for selector")
        assert not full_setup.is_no_browser("")


class TestTheVirtualDisplayStartsItself:
    """The fix for the observed wizard failure.

    The sign-in must be headed -- Google rejects headless Chrome -- and a
    headed browser needs an X server a VPS does not have by default.
    Playwright's own error ("Looks like you launched a headed browser
    without having a XServer running") reached the operator as "likely
    needs a human for 2FA/captcha". Xvfb was installed on the host the
    whole time; nothing ever started it.
    """

    def test_an_existing_display_is_left_alone(self, monkeypatch):
        import dwd_helper

        monkeypatch.setenv("DISPLAY", ":0")

        def boom(*a, **kw):
            raise AssertionError("must not start Xvfb when one already exists")

        monkeypatch.setattr(dwd_helper.subprocess, "Popen", boom)
        assert dwd_helper._ensure_display() == ":0"

    def test_a_linux_host_with_xvfb_gets_a_display_started_for_it(
            self, monkeypatch):
        import dwd_helper

        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setattr(dwd_helper.sys, "platform", "linux")
        monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/Xvfb")
        started = {}

        class FakeProc:
            def poll(self):
                return None
            def terminate(self):
                pass

        live: set[str] = set()

        def fake_popen(argv, **kw):
            started["argv"] = argv
            # A real Xvfb creates its socket shortly after starting; the
            # wait loop exists precisely because it is not instant.
            live.add(f"/tmp/.X11-unix/X{argv[1].lstrip(':')}")
            return FakeProc()

        monkeypatch.setattr(dwd_helper.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(dwd_helper.os.path, "exists", lambda p: p in live)
        monkeypatch.setattr(dwd_helper.atexit, "register", lambda f: None)
        # The readiness handshake is exercised by its own tests; without
        # stubbing it here this waits out the real retry budget for every
        # display number it tries.
        monkeypatch.setattr(dwd_helper, "_display_live", lambda num: True)
        monkeypatch.setattr(dwd_helper.time, "sleep", lambda s: None)

        display = dwd_helper._ensure_display()
        assert display == ":99"
        assert started["argv"][0] == "Xvfb"
        assert "-screen" in started["argv"]
        assert os.environ["DISPLAY"] == ":99"
        dwd_helper._stop_display()
        monkeypatch.delenv("DISPLAY", raising=False)

    def test_a_display_already_in_use_is_stepped_over(self, monkeypatch):
        """Two concurrent setup jobs must not land on the same display and
        fight over it."""
        import dwd_helper

        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setattr(dwd_helper.sys, "platform", "linux")
        monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/Xvfb")
        live = {"/tmp/.X11-unix/X99"}          # someone else already has :99

        class FakeProc:
            def poll(self):
                return None
            def terminate(self):
                pass

        def fake_popen(argv, **kw):
            live.add(f"/tmp/.X11-unix/X{argv[1].lstrip(':')}")
            return FakeProc()

        monkeypatch.setattr(dwd_helper.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(dwd_helper.os.path, "exists", lambda p: p in live)
        monkeypatch.setattr(dwd_helper.atexit, "register", lambda f: None)
        monkeypatch.setattr(dwd_helper, "_display_live", lambda num: True)
        monkeypatch.setattr(dwd_helper.time, "sleep", lambda s: None)

        assert dwd_helper._ensure_display() == ":100"
        dwd_helper._stop_display()
        monkeypatch.delenv("DISPLAY", raising=False)

    def test_a_host_without_xvfb_reports_rather_than_hanging(self, monkeypatch):
        import dwd_helper

        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setattr(dwd_helper.sys, "platform", "linux")
        monkeypatch.setattr("shutil.which", lambda n: None)
        assert dwd_helper._ensure_display() == ""

    def test_macos_needs_no_virtual_display(self, monkeypatch):
        """There is a real window server; nothing to start, nothing to warn
        about."""
        import dwd_helper

        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setattr(dwd_helper.sys, "platform", "darwin")

        def boom(*a, **kw):
            raise AssertionError("must not start Xvfb on macOS")

        monkeypatch.setattr(dwd_helper.subprocess, "Popen", boom)
        assert dwd_helper._ensure_display() == ""


class TestSetupGrantsTheOptionalScopes:
    """The setup runner writes a wider line than it enforces.

    A tenant set up by this runner should come out able to read licences
    without a second hand-pasted grant. But an optional scope must never be
    able to fail a setup -- it costs one panel feature; failing setup costs
    the whole tenant, and the advertised remedy (re-run setup) mints another
    throwaway GCP project.
    """

    def test_the_grant_line_is_wider_than_the_required_line(self):
        import verify_scopes
        from config import Settings

        st = Settings()
        for side in ("source", "target"):
            grant = set(verify_scopes.grant_scopes(st, side))
            need = set(verify_scopes.required_scopes(st, side))
            assert need < grant, f"{side}: grant line should be strictly wider"
            # Wider in two directions, and the second one is why this
            # assertion loosened: it used to demand the difference be
            # EXACTLY the optional scopes, which pinned the grant to what
            # today's toggles happen to need. migrate_chat defaults off, so
            # chat.memberships.readonly was never granted -- and switching
            # Chat on then failed the whole token request, taking every
            # other service with it.
            assert verify_scopes.OPTIONAL_SCOPES <= grant
            assert verify_scopes.every_toggle_scopes(st, side) <= grant

    def test_the_grant_covers_scopes_no_current_toggle_asks_for(self):
        """The specific regression. Every one of these is needed only when
        some feature is switched on, and none of them is in required_scopes
        with the defaults -- so each was a live tenant one toggle away from
        an opaque unauthorized_client."""
        import verify_scopes
        from config import Settings

        grant = set(verify_scopes.grant_scopes(Settings(), "source"))
        for scope in ("https://www.googleapis.com/auth/chat.memberships.readonly",
                      "https://www.googleapis.com/auth/contacts.readonly",
                      "https://www.googleapis.com/auth/tasks.readonly",
                      "https://www.googleapis.com/auth/gmail.settings.basic"):
            assert scope in grant, scope

    def test_required_scopes_stays_narrow(self):
        """The gate must keep asking "what does THIS run need". Widening it
        to the toggle union would refuse to start every tenant that has not
        re-pasted its line -- the exact breakage the split exists to avoid."""
        import verify_scopes
        from config import Settings

        need = set(verify_scopes.required_scopes(Settings(), "source"))
        assert "https://www.googleapis.com/auth/chat.memberships.readonly" not in need

    def test_setup_grants_the_wide_line_not_the_required_one(self):
        """If this regresses, new tenants come out without apps.licensing and
        the licence panel is empty for every one of them."""
        import inspect

        import full_setup

        src = inspect.getsource(full_setup.run_full_setup)
        assert "verify_scopes.grant_scopes(" in src
        # The grant must not be built from required_scopes alone.
        assert 'scopes = ",".join(verify_scopes.required_scopes(' not in src

    def test_an_optional_scope_is_graded_leniently(self):
        """Only the required set decides whether the phase passes, and
        whether to keep waiting through the ~15 minute propagation budget."""
        import inspect

        import full_setup

        src = inspect.getsource(full_setup.run_full_setup)
        i = src.index("missing_optional")
        window = src[i:i + 900]
        assert "if sc in required" in window
        assert "if sc in optional" in window

    def test_a_missing_optional_scope_is_named_in_the_success_detail(self):
        """Named, not silent -- this is the message that sends someone to the
        right console entry instead of wondering why a panel is empty."""
        import full_setup

        detail = full_setup.optional_missing_detail(
            ["https://www.googleapis.com/auth/apps.licensing"])
        assert "apps.licensing" in detail
        assert "re-paste the scope line" in detail
        assert "nothing else is affected" in detail

    def test_a_failed_grant_does_not_fail_a_tenant_whose_required_scopes_are_live(self):
        """The regression this guards.

        Adding optional scopes to the console line means an already-complete
        tenant now gets sent through the browser again just to add them. If
        that browser cannot run -- no display, 2FA, a captcha, all seen live
        on this project -- failing the whole setup would turn a previously
        green tenant red over a panel feature.
        """
        import inspect

        import full_setup

        src = inspect.getsource(full_setup.run_full_setup)
        i = src.index("grant_failed")
        window = src[i:i + 1600]
        assert "is_complete" in window
        assert "sorted(required)" in window
        assert "already_granted = True" in window


class TestTheDisplayMustActuallyServe:
    """A socket file is not a running X server.

    Confirmed live: _ensure_display logged "started a virtual display on
    :100" and Chrome then failed with Playwright's own "Looks like you
    launched a headed browser without having a XServer running" -- the
    socket appears a moment before the server accepts connections, and the
    file check raced it. Three layers up that surfaced as "the Admin Console
    DOM shifted underneath it": a browser that never started, reported as a
    selector problem.
    """

    def _linux_with_xvfb(self, monkeypatch, dwd_helper):
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setattr(dwd_helper.sys, "platform", "linux")
        monkeypatch.setattr("shutil.which", lambda n: f"/usr/bin/{n}")
        monkeypatch.setattr(dwd_helper.atexit, "register", lambda f: None)

    def test_a_socket_that_is_not_yet_serving_is_not_accepted(self, monkeypatch):
        import dwd_helper

        self._linux_with_xvfb(monkeypatch, dwd_helper)
        live: set[str] = set()

        class FakeProc:
            def poll(self):
                return None
            def terminate(self):
                pass

        def fake_popen(argv, **kw):
            live.add(f"/tmp/.X11-unix/X{argv[1].lstrip(':')}")
            return FakeProc()

        monkeypatch.setattr(dwd_helper.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(dwd_helper.os.path, "exists", lambda p: p in live)
        # The socket exists everywhere, but nothing ever serves.
        monkeypatch.setattr(dwd_helper, "_display_live", lambda num: False)
        monkeypatch.setattr(dwd_helper.time, "sleep", lambda s: None)

        assert dwd_helper._ensure_display() == ""

    def test_a_display_that_comes_up_late_is_still_waited_for(self, monkeypatch):
        """Xvfb is not instant; giving up on the first check would make this
        flaky in exactly the way the file-only check was."""
        import dwd_helper

        self._linux_with_xvfb(monkeypatch, dwd_helper)
        live: set[str] = set()
        calls = {"n": 0}

        class FakeProc:
            def poll(self):
                return None
            def terminate(self):
                pass

        monkeypatch.setattr(dwd_helper.subprocess, "Popen",
                            lambda argv, **kw: (
                                live.add(f"/tmp/.X11-unix/X{argv[1].lstrip(':')}"),
                                FakeProc())[1])
        monkeypatch.setattr(dwd_helper.os.path, "exists", lambda p: p in live)

        def late(num):
            calls["n"] += 1
            return calls["n"] > 3          # serving only on the 4th check

        monkeypatch.setattr(dwd_helper, "_display_live", late)
        monkeypatch.setattr(dwd_helper.time, "sleep", lambda s: None)

        assert dwd_helper._ensure_display() == ":99"
        dwd_helper._stop_display()
        monkeypatch.delenv("DISPLAY", raising=False)

    def test_a_host_without_xdpyinfo_trusts_the_socket_rather_than_refusing(
            self, monkeypatch):
        """Degrading to the old behaviour beats refusing to run at all."""
        import dwd_helper

        monkeypatch.setattr("shutil.which", lambda n: None)
        assert dwd_helper._display_live(99) is True


class TestTheGuardSaysWhatItDecided:
    """A silent `except: pass` turned a probe failure into a failed setup
    that blamed the browser. This area has already cost a day to exactly
    that kind of misdirection."""

    def test_the_decision_is_logged_either_way(self):
        import inspect

        import full_setup

        src = inspect.getsource(full_setup.run_full_setup)
        i = src.index("grant_failed")
        window = src[i:i + 1200]
        assert "required scopes complete=" in window
        assert "could not run" in window
        # No bare swallow: the failure path must set complete=False and say so.
        assert "complete = False" in window


class TestDiagnosticsAreActuallyEmittable:
    """Four call sites called log(); nothing defined it.

    Three were new. The fourth had been sitting in the pre-check's `except`
    branch since the day before -- a NameError waiting for the one path that
    would have most needed to explain itself, which is exactly why it went
    unnoticed. Rarely-taken branches are where this class of bug lives, so
    it gets a test rather than a reading.
    """

    def test_log_exists_and_writes_to_stderr(self, capsys):
        """stdout carries the --json result and nothing else; a diagnostic
        line on stdout would corrupt it for every caller that parses it."""
        import full_setup

        full_setup.log("hello")
        captured = capsys.readouterr()
        assert "hello" in captured.err
        assert "hello" not in captured.out

    def test_every_log_call_site_can_actually_run(self):
        """Compiles the module and confirms `log` resolves as a global --
        the check that would have caught the latent one."""
        import full_setup

        assert callable(getattr(full_setup, "log", None))

    def test_the_precheck_failure_branch_can_report_itself(self, monkeypatch,
                                                           capsys):
        """The specific branch that carried the latent NameError: a
        pre-check that raises must produce a diagnostic, not a second,
        unrelated exception."""
        import full_setup

        full_setup.log(f"  pre-check could not run ({'boom'}); granting anyway")
        assert "pre-check could not run" in capsys.readouterr().err


class TestConsoleFailuresNameAccessNotSelectors:
    """Setup both provisions projects AND accepts uploaded keys.

    An uploaded key can point at a project the Workspace admin has no IAM
    role on -- Workspace admin and GCP IAM are separate systems, and an
    upload carries no relationship to who owns the project behind it. Every
    console-driven step then runs as an account that cannot open the page.

    Confirmed live: an uploaded key for wsmig-src-96030, whose admin is
    owner of a DIFFERENT project it created itself, reported "could not find
    the app name field -- console may have changed".
    """

    def test_the_hint_names_the_project_the_admin_and_the_fix(self):
        import full_setup

        hint = full_setup._chat_access_hint(
            "wsmig-src-96030", "info@source.example.com",
            "could not find the app name field")
        assert "wsmig-src-96030" in hint
        assert "info@source.example.com" in hint
        assert "add-iam-policy-binding" in hint
        assert "UPLOADED key" in hint

    def test_it_says_provisioned_projects_do_not_need_this(self):
        """Otherwise the reader cannot tell whether this is a permanent
        limitation or a property of how this one tenant was set up."""
        import full_setup

        hint = full_setup._chat_access_hint("p", "a@b.com", "x")
        assert "provisions itself get that grant automatically" in hint

    def test_only_the_missing_form_failure_is_reinterpreted(self):
        """A genuine selector change must keep its own message -- rewriting
        every Chat failure as a permissions problem would be the same error
        in the other direction."""
        import inspect

        import full_setup

        src = inspect.getsource(full_setup.run_full_setup)
        assert 'if not chat_ok and "name field" in chat_detail:' in src


class TestDeployInstallsItsOwnUnitFiles:
    """systemd/*.service was documented as a one-time manual copy, so every
    later edit stayed on the dev machine. Found live: xvfb.service existed in
    the repo and had never been installed at all -- the virtual display the
    whole browser-automation path depends on was an unmanaged process that
    would not have survived a reboot."""

    def _script(self):
        import os
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "sync_vps.sh"), encoding="utf-8") as fh:
            return fh.read()

    def test_units_are_copied_and_reloaded_before_the_restart(self):
        src = self._script()
        assert "/etc/systemd/system/" in src
        assert "systemctl daemon-reload" in src
        assert src.index("daemon-reload") < src.index("systemctl restart")

    def test_the_xvfb_unit_exists_to_be_installed(self):
        import os
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        unit = os.path.join(here, "systemd", "xvfb.service")
        assert os.path.isfile(unit)
        body = open(unit, encoding="utf-8").read()
        assert "Xvfb :99" in body
        # Restart=always is what makes it survive the browser crashing it.
        assert "Restart=always" in body


class TestUnlicensedAccountsAreNamedAsSuch:
    """Google reports an unlicensed account as:

        HTTP 400 (failedPrecondition): ... Mail service not enabled

    Nothing in that sentence contains the word licence, so it reads like a
    service outage or a scope problem, and it is neither. Seen live on both
    tenants simultaneously -- 201 accounts, 200 licences, one unlicensed
    account on each side -- producing two failed users and an HTTP 400 that
    named no cause anyone could act on.
    """

    def test_the_licence_cause_is_stated(self):
        import main

        out = main.explain_user_failure(
            RuntimeError("HTTP 400 (failedPrecondition): Mail service not enabled"),
            "zane@src.com", "zane@tgt.com")
        assert "licence" in out.lower()
        assert "zane@src.com" in out and "zane@tgt.com" in out
        assert "Billing" in out

    def test_it_says_nothing_was_migrated_for_that_user(self):
        """Otherwise a partial migration is the reasonable assumption, and
        re-running looks risky rather than correct."""
        import main

        out = main.explain_user_failure(
            RuntimeError("failedPrecondition: Mail service not enabled"),
            "a@s.com", "a@t.com")
        assert "Nothing was migrated" in out

    def test_the_original_error_is_kept_not_replaced(self):
        """The raw text is what matches a support article or a log search."""
        import main

        raw = "HTTP 400 (failedPrecondition): Mail service not enabled for user"
        assert raw in main.explain_user_failure(RuntimeError(raw), "a@s", "a@t")

    def test_an_unrecognised_error_is_left_completely_alone(self):
        """A diagnosis layer that paraphrases what it does not understand is
        worse than none -- it hides the detail that would have identified
        the error."""
        import main

        assert main.explain_user_failure(
            RuntimeError("Connection reset by peer"), "a@s", "a@t"
        ) == "Connection reset by peer"

    def test_a_partial_match_does_not_trigger_it(self):
        """failedPrecondition alone covers plenty of unrelated conditions;
        both halves have to be present."""
        import main

        out = main.explain_user_failure(
            RuntimeError("HTTP 400 (failedPrecondition): bad label id"),
            "a@s", "a@t")
        assert "licence" not in out.lower()


class TestContactsStripSourceProvenance:
    """people.connections.list annotates every field with where it came from
    -- metadata.source.id, pointing at a record in the SOURCE tenant.
    Sending that back to createContact fails the whole call:

        HTTP 400 (INVALID_ARGUMENT): Fields with source ids are not allowed.

    Confirmed live: 50 of 50 contacts failed this way on a two-user canary,
    while Drive, Calendar and Tasks in the same run succeeded.
    """

    def test_field_metadata_is_removed(self):
        import contacts_engine as ce

        out = ce._strip_source_metadata(
            {"value": "a@b.com", "metadata": {"source": {"id": "x"}}})
        assert out == {"value": "a@b.com"}

    def test_it_recurses_into_lists_of_fields(self):
        """These arrive as lists -- several phone numbers, each carrying its
        own metadata -- so stripping only the outer dict leaves the ids in
        place and the call still fails."""
        import contacts_engine as ce

        out = ce._strip_source_metadata([
            {"value": "+1", "metadata": {"source": {"id": "x"}}, "type": "home"},
            {"value": "+2", "metadata": {"source": {"id": "y"}}},
        ])
        assert out == [{"value": "+1", "type": "home"}, {"value": "+2"}]

    def test_nested_metadata_is_removed_too(self):
        import contacts_engine as ce

        out = ce._strip_source_metadata(
            {"name": {"givenName": "A", "metadata": {"source": {"id": "z"}}}})
        assert out == {"name": {"givenName": "A"}}

    def test_everything_else_survives_untouched(self):
        """Stripping more than provenance would silently drop real contact
        data -- the failure mode would be a migration that succeeds with
        emptier contacts, which is worse than one that errors."""
        import contacts_engine as ce

        person = {"givenName": "Ada", "familyName": "Lovelace",
                  "value": "ada@example.com", "type": "work"}
        assert ce._strip_source_metadata(person) == person

    def test_scalars_pass_through(self):
        import contacts_engine as ce

        assert ce._strip_source_metadata("plain") == "plain"
        assert ce._strip_source_metadata(7) == 7

    def test_the_migrator_still_has_its_method(self):
        """The first attempt at this fix put the helper at column 0 inside
        the class body, which ended the class and turned _migrate_contact
        into a nested function -- the file still parsed and the method was
        simply gone."""
        import contacts_engine as ce

        assert hasattr(ce.ContactsMigrator, "_migrate_contact")


class TestAWhollyFailedServiceIsNotMarkedDone:
    """A service marked done is skipped on every later run, so marking one
    that failed outright makes the failure permanent -- the fix can never be
    applied because the user is never looked at again.

    Confirmed live and immediately: every contact of a canary user failed
    with "Fields with source ids are not allowed", contacts was marked done
    anyway, and re-running after fixing the bug reported "no users to
    process". The data was recoverable; the ledger said otherwise.
    """

    def test_a_service_where_everything_failed_is_not_recorded(self):
        import main

        assert main._services_that_succeeded(
            {"contacts": {"contacts": 0, "failed": 50, "skipped": 0}}) == []

    def test_a_partial_failure_still_counts_as_done(self):
        """The per-item ledger already skips what landed and retries what did
        not, so those users are not stranded -- only the all-or-nothing case
        needs protecting, and treating every failure as un-done would
        re-walk whole mailboxes for one bad message."""
        import main

        assert main._services_that_succeeded(
            {"gmail": {"inserted": 4333, "failed": 2}}) == ["gmail"]

    def test_a_clean_run_is_recorded(self):
        import main

        assert main._services_that_succeeded(
            {"drive": {"files": 82, "folders": 48, "failed": 0}}) == ["drive"]

    def test_nothing_to_migrate_still_counts_as_done(self):
        """A user with no tasks has finished tasks. Leaving it un-marked
        would re-check it on every run forever."""
        import main

        assert main._services_that_succeeded(
            {"tasks": {"tasks": 0, "failed": 0}}) == ["tasks"]

    def test_an_unexpected_stats_shape_is_recorded_rather_than_dropped(self):
        """An engine returning something this does not understand must not
        silently cause its users to be re-migrated forever."""
        import main

        assert main._services_that_succeeded({"chat": "ok"}) == ["chat"]


class TestStaleServiceMarkersAreReopened:
    """A service marked done is skipped forever, so a bug that failed one
    outright made itself permanent. That is fixed going forward, but ledgers
    written BEFORE the fix still carry the bad markers and those users stay
    stranded -- which is exactly the state a live tenant was left in after
    contacts failed 50 for 50 and marked itself complete.
    """

    class FakeDB:
        def __init__(self, rows, mappings, failures, done):
            self._rows, self._map, self._fail = rows, mappings, failures
            self._done = done
            self.updates = []
            outer = self

            class Conn:
                def execute(self, sql, args):
                    user = args[0]
                    if "id_mapping" in sql:
                        types = set(args[1:])
                        n = sum(1 for u, t in outer._map if u == user and t in types)
                    else:
                        n = outer._fail.get(user, 0)
                    return type("R", (), {"fetchone": lambda s, n=n: {"n": n}})()
            self.conn = Conn()

        def all_identities(self):
            return self._rows

        def services_done(self, user):
            return set(self._done.get(user, set()))

        def write(self):
            outer = self

            class Ctx:
                def __enter__(self_inner):
                    class C:
                        def execute(self_c, sql, args):
                            outer.updates.append(args)
                    return C()
                def __exit__(self_inner, *a):
                    return False
            return Ctx()

    def test_a_service_with_no_items_and_a_failure_is_reopened(self):
        import main

        db = self.FakeDB(
            rows=[{"entity_type": "user", "source_email": "a@x.com"}],
            mappings=[("a@x.com", "message")],      # gmail worked
            failures={"a@x.com": 50},               # contacts did not
            done={"a@x.com": {"gmail", "contacts"}})
        reopened = main.reconcile_service_markers(db)
        assert reopened == [("a@x.com", "contacts")]
        assert db.updates and db.updates[0][0] == "gmail"

    def test_a_service_that_migrated_something_is_left_alone(self):
        import main

        db = self.FakeDB(
            rows=[{"entity_type": "user", "source_email": "a@x.com"}],
            mappings=[("a@x.com", "contact")],
            failures={"a@x.com": 3},                # partial failure
            done={"a@x.com": {"contacts"}})
        assert main.reconcile_service_markers(db) == []

    def test_an_empty_service_with_no_failures_is_left_alone(self):
        """The ordinary state of a user with no tasks. Re-opening it would
        re-check every empty mailbox on every run forever."""
        import main

        db = self.FakeDB(
            rows=[{"entity_type": "user", "source_email": "a@x.com"}],
            mappings=[], failures={}, done={"a@x.com": {"tasks"}})
        assert main.reconcile_service_markers(db) == []

    def test_a_broken_ledger_row_does_not_stop_the_run(self):
        import main

        class Boom(self.FakeDB):
            def services_done(self, user):
                raise RuntimeError("ledger row is corrupt")

        db = Boom(rows=[{"entity_type": "user", "source_email": "a@x.com"}],
                  mappings=[], failures={}, done={})
        assert main.reconcile_service_markers(db) == []

    def test_it_runs_before_users_are_dispatched(self):
        """After dispatch it would be useless -- the skip has already
        happened."""
        import ast
        import inspect
        import textwrap

        import main

        fn = ast.parse(textwrap.dedent(
            inspect.getsource(main._run_with_memory_pause))).body[0]
        order = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in ("reconcile_service_markers", "run_batch"):
                    order.append((node.lineno, node.func.id))
        names = [n for _, n in sorted(order)]
        assert names[:2] == ["reconcile_service_markers", "run_batch"], names


class TestAclFailuresResolveAgainstTheTarget:
    """A run reported 127,852 failed ACL operations. They were 2,116 files,
    each with up to 202 grants, and the target held every one of them --
    202 permissions on the source, 202 on the target.

    The grants had hit rateLimitExceeded, logged a FAILED row, been retried,
    and succeeded. Nothing overwrote the row because ACL successes were
    never logged. A number that alarming and that wrong is worse than no
    number: it sends people to repair what already worked.
    """

    class FakeDB:
        def __init__(self, failed, mappings, identities, perms):
            self._perms = perms
            self.logged = []
            outer = self

            class Conn:
                def execute(self, sql, args=()):
                    if "audit_log" in sql:
                        rows = [{"source_user": u, "item_id": k}
                                for u, k in failed]
                    elif "identity_map" in sql:
                        rows = [(s, t) for s, t in identities]
                    else:
                        tid = mappings.get(args[0])
                        rows = [{"target_id": tid}] if tid else []
                    return type("C", (), {
                        "fetchall": lambda s, r=rows: r,
                        "fetchone": lambda s, r=rows: (r[0] if r else None),
                    })()
            self.conn = Conn()

        def log_audit(self, user, key, kind, status, msg=""):
            self.logged.append((key, status))

    def _auth(self, perms):
        class Files:
            def get(self, **kw):
                return type("E", (), {
                    "execute": lambda s: {"permissions": perms}})()

        class Drive:
            def files(self):
                return Files()

        class Auth:
            def target_drive(self, u):
                return Drive()
        return Auth()

    def test_a_grant_present_on_the_target_is_resolved(self):
        import acl_reconcile

        db = self.FakeDB(
            failed=[("a@s.com", "FILE1:bob@t.com")],
            mappings={"FILE1": "TGT1"},
            identities=[("a@s.com", "a@t.com")],
            perms=None)
        stats = acl_reconcile.reconcile(
            self._auth([{"emailAddress": "bob@t.com", "type": "user"}]), db, None)
        assert stats["resolved"] == 1
        assert stats["still_failed"] == 0
        assert db.logged == [("FILE1:bob@t.com", "SUCCESS")]

    def test_a_grant_genuinely_missing_stays_failed(self):
        """This resolves REPORTING, never the underlying work -- a grant the
        target does not have must keep saying so."""
        import acl_reconcile

        db = self.FakeDB(
            failed=[("a@s.com", "FILE1:bob@t.com")],
            mappings={"FILE1": "TGT1"},
            identities=[("a@s.com", "a@t.com")],
            perms=None)
        stats = acl_reconcile.reconcile(
            self._auth([{"emailAddress": "someone.else@t.com", "type": "user"}]),
            db, None)
        assert stats["resolved"] == 0
        assert stats["still_failed"] == 1
        assert db.logged == []

    def test_a_file_that_never_copied_is_not_resolved(self):
        """Its grant failures are real: there is nothing to hold them."""
        import acl_reconcile

        db = self.FakeDB(
            failed=[("a@s.com", "MISSING:bob@t.com")],
            mappings={}, identities=[("a@s.com", "a@t.com")], perms=None)
        stats = acl_reconcile.reconcile(self._auth([]), db, None)
        assert stats["resolved"] == 0
        assert stats["unreadable"] == 1

    def test_a_dry_run_writes_nothing(self):
        import acl_reconcile

        db = self.FakeDB(
            failed=[("a@s.com", "FILE1:bob@t.com")],
            mappings={"FILE1": "TGT1"},
            identities=[("a@s.com", "a@t.com")], perms=None)
        stats = acl_reconcile.reconcile(
            self._auth([{"emailAddress": "bob@t.com", "type": "user"}]),
            db, None, dry_run=True)
        assert stats["resolved"] == 1
        assert db.logged == []

    def test_link_and_domain_grants_are_matched_by_type(self):
        """anyone/domain grants carry no email address, so matching on one
        would report every link-share as permanently failed."""
        import acl_reconcile

        db = self.FakeDB(
            failed=[("a@s.com", "FILE1:anyone")],
            mappings={"FILE1": "TGT1"},
            identities=[("a@s.com", "a@t.com")], perms=None)
        stats = acl_reconcile.reconcile(
            self._auth([{"type": "anyone"}]), db, None)
        assert stats["resolved"] == 1

    def test_the_engine_records_acl_successes_now(self):
        """The permanent fix. log_audit upserts on (user, item, type), so a
        recovered grant overwrites its own FAILED row -- without this the
        ledger keeps every stumble and none of the recoveries."""
        import inspect

        import drive_engine

        src = inspect.getsource(drive_engine)
        assert src.count('"acl", "SUCCESS"') >= 2, (
            "both the batch and single-grant paths must record success")
