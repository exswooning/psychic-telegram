"""A dead man switch built from the wrong signal destroys working systems.

The login history on this host settles it: `last` shows a THIRTEEN DAY gap
(2026-08-28 to 2026-09-10) during which migrations ran and the code was
deployed daily, because `last` records interactive sessions only and an ssh
command with no pty leaves no wtmp entry. Over the same window sshd logged
6,553 accepted authentications.

Measured on the same data: 3 gaps of >= 12 hours in 21 days, the longest
27.9 hours. So "wipe if no login for 12 hours" -- the obvious way to build
this -- would have fired three times in three weeks.
"""
from __future__ import annotations

import inspect
import json
import os
import re
import tempfile

import pytest

import deadman


def _code(fn) -> str:
    src = inspect.getsource(fn)
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    return re.sub(r"#.*$", "", src, flags=re.M)


class TestItMeasuresLifeNotLogins:
    def test_it_uses_several_independent_signals(self):
        sig = deadman.signals()
        for name in ("interactive login", "sshd auth", "web UI login",
                     "deploy", "manual touch"):
            assert name in sig, name

    def test_the_newest_signal_wins(self):
        """Any one of them means somebody is here. Requiring all of them
        would make the switch fire on the quietest channel."""
        src = _code(deadman.last_seen)
        assert "max(" in src

    def test_a_running_job_is_not_a_sign_of_life(self):
        """A job left running says nothing about whether its owner still
        exists, and counting it would let a stuck process hold the switch
        open forever."""
        assert "active_job" not in _code(deadman.signals)
        assert "running migration" not in str(deadman.signals())

    def test_the_wtmp_parser_finds_the_date_by_content(self):
        """It counted columns from the left and was off by one, so the
        interactive-login signal read "never" -- which on a dead man switch
        means "nobody has ever been here"."""
        src = _code(deadman._last_interactive_login)
        assert "days" in src and "for i, tok in enumerate(parts)" in src


class TestItRefusesToFireOnBadInput:
    def test_it_does_nothing_when_no_signal_can_be_read(self):
        """A switch that fires when it cannot measure fires at random."""
        src = _code(deadman.main)
        assert "no aliveness signal could be read" in inspect.getsource(deadman.main)

    def test_it_will_not_arm_below_a_week(self, monkeypatch, tmp_path):
        """Against a measured 27.9-hour quiet gap during normal use."""
        monkeypatch.setattr(deadman, "CONFIG", str(tmp_path / "c.json"))
        with pytest.raises(SystemExit, match="refusing a deadline under 7 days"):
            deadman.main(["--arm", "--days", "0.5"])

    def test_arming_is_separate_from_permitting_destruction(self, monkeypatch, tmp_path):
        """--arm writes the schedule; --yes is what allows it to destroy. So
        the default of an armed switch is to warn and not act."""
        monkeypatch.setattr(deadman, "CONFIG", str(tmp_path / "c.json"))
        monkeypatch.setattr(deadman, "DISARM", str(tmp_path / "off"))
        deadman.main(["--arm", "--days", "30"])
        ok, state = deadman.armed()
        assert ok is False
        assert "never confirmed" in state

    def test_the_disarm_file_beats_everything(self, monkeypatch, tmp_path):
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({"armed": True, "confirmed": True, "days": 30}))
        monkeypatch.setattr(deadman, "CONFIG", str(cfg))
        off = tmp_path / "off"
        monkeypatch.setattr(deadman, "DISARM", str(off))
        assert deadman.armed()[0] is True
        off.write_text("stop")
        assert deadman.armed()[0] is False


class TestItWarnsBeforeItActs:
    def test_there_are_several_thresholds(self):
        """Silence until the moment of destruction is how this takes out
        someone who was a day from returning."""
        assert len(deadman.WARN_AT) >= 3
        assert min(deadman.WARN_AT) <= 0.5

    def test_each_threshold_emails_once(self):
        """A cron every few minutes would otherwise send hundreds of
        identical warnings and train the recipient to ignore exactly the
        message that matters."""
        src = _code(deadman.main)
        assert 'cfg.get("warned"' in src
        assert "sent.add(" in src

    def test_signs_of_life_reset_the_warnings(self):
        """Otherwise the next quiet spell skips straight to destruction
        without warning."""
        src = _code(deadman.main)
        assert 'cfg["warned"] = []' in src

    def test_a_mail_failure_cannot_stop_the_switch_working(self):
        """A switch that crashes because it could not send mail stops
        warning and then fires silently -- the worst of both."""
        src = _code(deadman.notify)
        assert "except Exception" in src
        assert "raise" not in src

    def test_credentials_come_from_a_root_only_file(self):
        """It is a password, and argv is readable by every process."""
        assert deadman.EMAIL_ENV.startswith("/etc/bitport")
        assert "argv" not in _code(deadman._email_config)


class TestWhatItDestroys:
    def test_it_takes_the_credentials_and_the_ledgers(self):
        t = deadman.targets({"include_backups": True})
        joined = " ".join(t)
        assert "keys" in joined or not t          # absent paths are filtered
        src = _code(deadman.targets)
        for name in ("keys", "/etc/bitport", "data", "migration.db",
                     "env.sh", "backups"):
            assert name in src, name

    def test_it_does_not_destroy_the_operating_system(self):
        """The secret material is those paths, and a box that still boots
        can be inspected afterwards to confirm what happened."""
        src = _code(deadman.targets)
        for dangerous in ('"/"', '"/etc"', '"/usr"', '"/var"', "/bin"):
            assert dangerous not in src, dangerous

    def test_a_dry_run_removes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            probe = os.path.join(d, "keep.txt")
            open(probe, "w").close()
            out = deadman.wipe({"include_backups": False}, dry=True)
            # Asserted on the property, not the prefix: the dry run also
            # summarises the side effects (cron, Caddyfile, journal) that
            # are not file removals, and requiring every line to start
            # "WOULD REMOVE" failed on a line that removes nothing.
            assert all(not line.startswith("removed ") for line in out)
            assert all(not line.startswith("FAILED ") for line in out)
            assert os.path.exists(probe)

    def test_services_stop_first_now_that_the_code_is_a_target(self):
        """This asserted the opposite, and the opposite was right until the
        code directory became a target: stopping last left the box quiet
        while the files were still there, which mattered when the files
        were all that went.

        Now HERE is removed too, so a service still running would be
        writing into a tree being deleted, and a systemd restart could
        bring one back up mid-wipe. Disabling happens afterwards, once
        there is nothing left for a unit to start.
        """
        src = _code(deadman.wipe)
        assert src.index("stopped the services") < src.index("for path in targets")
        assert src.index("for path in targets") < src.index("disable")


class TestTheCodeGoesToo:
    def test_the_code_directory_is_a_target(self):
        src = _code(deadman.targets)
        assert 'include_code' in src
        assert "out.append(HERE)" in src

    def test_it_is_removed_last(self):
        """HERE contains this script, its venv and everything else, so
        removing it ends the process doing the removing. The credentials
        must already be gone by then -- a failure partway through should
        have taken the material that matters, not the code that reads it."""
        paths = deadman.targets({"include_backups": True, "include_code": True})
        assert paths[-1] == deadman.HERE

    def test_services_stop_before_anything_is_deleted(self):
        """Nothing should be writing to what is about to be removed, and no
        restart should bring a service back up mid-wipe."""
        src = _code(deadman.wipe)
        assert src.index("stopped the services") < src.index("for path in targets")

    def test_the_operating_system_is_still_not_touched(self):
        src = _code(deadman.targets)
        for dangerous in ('"/"', '"/etc"', '"/usr"', '"/var"', '"/bin"', '"/home"'):
            assert dangerous not in src, dangerous

    def test_it_does_not_claim_a_forensic_wipe(self):
        """Overwriting does not reliably destroy data on an SSD -- wear
        levelling means the blocks written are usually not the blocks that
        held the old copy, and a snapshot defeats it outright. Saying so is
        the difference between a tool and a promise it cannot keep."""
        doc = inspect.getsource(deadman.targets)
        assert "wear levelling" in doc
        assert "not a forensic wipe" in doc
        assert "provider" in doc


class TestResettingIt:
    def test_a_manual_touch_is_a_signal(self):
        assert "manual touch" in deadman.signals()

    def test_touch_writes_where_the_signal_reads(self, monkeypatch, tmp_path):
        t = tmp_path / "alive"
        monkeypatch.setattr(deadman, "TOUCH", str(t))
        deadman.main(["--touch"])
        assert t.exists()
        assert deadman.signals()["manual touch"] > 0

    def test_the_endpoint_requires_a_superadmin(self):
        """It holds off the most destructive automation on the machine."""
        import api_server
        src = inspect.getsource(api_server.deadman_touch)
        assert "require_superadmin(op)" in src

    def test_being_signed_in_is_not_itself_a_signal(self):
        """The web-UI signal reads the newest SESSION, and a session is
        written at login. Someone already signed in could otherwise watch
        the countdown reach zero while looking straight at it -- which is
        why the reset is a button and not a page load."""
        src = _code(deadman._last_webui_login)
        assert "sessions" in src
        assert "MAX(expires_at)" in src


class TestItLeavesNoTraceOutsideTheInstall:
    """"Why does it not wipe everything" -- because it did not.

    Removing /root/migration takes the code and every secret inside it, but
    a machine was left advertising what it used to be: six systemd units, a
    Caddyfile carrying the public domain, a cron entry firing every ten
    minutes at a deleted interpreter, and -- the one that matters -- the
    Chrome profile the browser automation signed into Google with.
    """

    def test_the_browser_profile_goes(self):
        """full_setup drives a real Chrome through a super-admin sign-in and
        the profile keeps the SESSION COOKIES. Credentials gone and a
        logged-in browser still there is most of what the credentials were
        for."""
        src = _code(deadman.targets)
        assert "google-chrome" in src
        assert "playwright" in src

    def test_the_systemd_units_go(self):
        src = _code(deadman.system_traces)
        assert "bitport-*" in src
        assert "xvfb" in src

    def test_the_cron_entry_goes(self):
        """It outlives everything it refers to and keeps firing at an
        interpreter that is no longer there."""
        src = _code(deadman.wipe)
        assert "crontab" in src
        assert "HERE not in ln" in src

    def test_the_caddyfile_is_restored_not_just_deleted(self):
        """install.sh saved whatever was there before. Putting it back beats
        leaving the box with no web server config at all."""
        src = _code(deadman.wipe)
        assert "Caddyfile.bitport-backup" in src
        assert "shutil.move" in src

    def test_the_journal_is_vacuumed(self):
        """It quotes tenant domains, user addresses and file names on every
        run."""
        src = _code(deadman.wipe)
        assert "--vacuum-time" in src

    def test_a_dry_run_still_changes_nothing_outside_the_tree(self):
        """The new steps are side effects rather than file removals, so the
        dry path has to return before them rather than skipping each."""
        out = deadman.wipe({"include_code": False, "include_backups": False},
                           dry=True)
        assert any("WOULD remove the cron entry" in line for line in out)
        assert all(not line.startswith("removed ") for line in out)


class TestTheCheckIn:
    """"An authenticator code that I put in once per 12 hours."

    Every other signal in this module is INCIDENTAL -- a deploy ran, a cron
    authenticated, a session existed. Those say the machine is in use, which
    is a different claim from the owner being alive and holding their second
    factor. Measured on this host, 12 hours of incidental quiet happened
    three times in three weeks; 12 hours without somebody deliberately
    typing a code means they did not type it.
    """

    @staticmethod
    def _seeded(monkeypatch, tmp_path):
        import base64
        import totp
        monkeypatch.setattr(totp, "SECRETS_FILE", str(tmp_path / "totp.env"))
        monkeypatch.setattr(deadman, "CHECKIN", str(tmp_path / "checkin"))
        secret = base64.b32encode(b"12345678901234567890").decode()
        totp.save_secret("admin@src.test", secret)
        return totp, secret

    def test_a_wrong_code_does_not_reset_the_clock(self, monkeypatch, tmp_path):
        self._seeded(monkeypatch, tmp_path)
        ok, _ = deadman.record_checkin("000000")
        assert ok is False
        assert not os.path.exists(deadman.CHECKIN)

    def test_a_current_code_resets_it(self, monkeypatch, tmp_path):
        totp, secret = self._seeded(monkeypatch, tmp_path)
        ok, who = deadman.record_checkin(totp.code_at(secret))
        assert ok is True and who == "admin@src.test"
        assert deadman.signals()["2-Step check-in"] > 0

    def test_the_previous_window_is_accepted(self, monkeypatch, tmp_path):
        """Clocks drift, and a code read at second 29 is typed in the next
        window. Rejecting it would reject CORRECT codes on the one control
        standing between the operator and the destruction of everything."""
        import time as _t
        totp, secret = self._seeded(monkeypatch, tmp_path)
        assert deadman.record_checkin(
            totp.code_at(secret, when=_t.time() - totp.PERIOD))[0] is True

    def test_but_not_an_indefinitely_old_one(self, monkeypatch, tmp_path):
        import time as _t
        totp, secret = self._seeded(monkeypatch, tmp_path)
        assert deadman.record_checkin(
            totp.code_at(secret, when=_t.time() - 3 * totp.PERIOD))[0] is False

    def test_it_tolerates_the_spaced_form_the_ui_shows(self, monkeypatch, tmp_path):
        """The page renders "481 920" because that is how an authenticator
        app shows it. Copy, paste, rejected-as-wrong is a terrible failure
        mode for a control whose failure destroys the machine."""
        totp, secret = self._seeded(monkeypatch, tmp_path)
        c = totp.code_at(secret)
        assert deadman.record_checkin(f"{c[:3]} {c[3:]}")[0] is True

    def test_it_says_so_when_no_seed_is_stored(self, monkeypatch, tmp_path):
        monkeypatch.setattr(deadman, "CHECKIN", str(tmp_path / "checkin"))
        import totp
        monkeypatch.setattr(totp, "SECRETS_FILE", str(tmp_path / "absent.env"))
        ok, why = deadman.record_checkin("123456")
        assert ok is False and "no authenticator seed" in why

    def test_the_secrets_path_is_resolved_when_called(self):
        """It was a default argument, which binds at DEFINITION time -- so
        relocating SECRETS_FILE left every one of these reading the original
        path while appearing to honour the change."""
        import totp
        assert inspect.signature(totp.load_secrets).parameters["path"].default is None

    def test_the_endpoint_needs_the_second_factor_not_just_a_session(self):
        """Which is the entire difference from /touch: a session can be held
        by automation that outlives its owner."""
        import api_server
        src = inspect.getsource(api_server.deadman_checkin)
        assert "require_superadmin(op)" in src
        assert "record_checkin" in src

    def test_a_failed_code_is_not_a_401(self):
        """It would be indistinguishable from an expired session, and the
        one thing this form must not do is fail for a reason nobody can
        act on."""
        import api_server
        src = inspect.getsource(api_server.deadman_checkin)
        assert '"ok": False' in src
        assert "HTTPException" not in src


class TestRequiringTheCheckIn:
    def test_only_the_deliberate_signal_counts_in_that_mode(self, monkeypatch):
        """Otherwise a deploy holds a 12-hour proof-of-life open, and the
        deadline quietly degrades into "has anything happened lately"."""
        monkeypatch.setattr(deadman, "signals", lambda: {
            "2-Step check-in": 100.0, "deploy": 9_999_999.0,
            "sshd auth": 9_999_999.0})
        assert deadman.last_seen({"require_checkin": True}) == (100.0,
                                                                "2-Step check-in")

    def test_without_it_the_newest_of_any_signal_still_wins(self, monkeypatch):
        monkeypatch.setattr(deadman, "signals", lambda: {
            "2-Step check-in": 100.0, "deploy": 9_999_999.0})
        assert deadman.last_seen({})[1] == "deploy"

    def test_a_short_deadline_is_allowed_only_with_it(self, monkeypatch, tmp_path):
        """The 7-day floor exists because 12 hours of INCIDENTAL quiet is
        normal -- measured, 27.9 hours during active use. A deliberate
        check-in is a different signal, so the floor does not apply to it."""
        TestTheCheckIn._seeded(monkeypatch, tmp_path)
        monkeypatch.setattr(deadman, "CONFIG", str(tmp_path / "c.json"))
        monkeypatch.setattr(deadman, "DISARM", str(tmp_path / "off"))
        with pytest.raises(SystemExit, match="refusing a deadline under 7 days"):
            deadman.main(["--arm", "--days", "0.5"])
        assert deadman.main(["--arm", "--days", "0.5", "--require-checkin"]) == 0
        assert deadman.load()["require_checkin"] is True

    def test_it_will_not_arm_a_deadline_that_cannot_be_met(self, monkeypatch, tmp_path):
        """require_checkin makes the 2-Step code the ONLY signal. With no
        seed stored there is no way to produce one, so the countdown would
        run to zero no matter what anybody did."""
        import totp
        monkeypatch.setattr(totp, "SECRETS_FILE", str(tmp_path / "absent.env"))
        monkeypatch.setattr(deadman, "CONFIG", str(tmp_path / "c.json"))
        with pytest.raises(SystemExit, match="needs an authenticator seed"):
            deadman.main(["--arm", "--days", "0.5", "--require-checkin"])


class TestPushingTheCodeBeforeTheWipe:
    """"Before wipe it should push the code to GitHub."

    The repository is PUBLIC -- install_node.sh clones it with no credential
    at all. A mistake in this filter does not lose data; it publishes
    service-account keys for live tenants and every customer's password
    hash, permanently, to an archive that is mirrored the moment it lands.
    """

    def test_the_push_happens_before_anything_is_destroyed(self):
        src = _code(deadman.wipe)
        assert src.index("push_code") < src.index("for path in targets")
        assert src.index("push_code") < src.index("stopped the services")

    def test_a_push_failure_cannot_stop_the_wipe(self):
        """The destruction is the point. Saving the code is a courtesy, and
        a courtesy that could block the destruction defeats the mechanism."""
        src = _code(deadman.push_code)
        assert "except Exception" in src
        assert "raise" not in src

    def test_nothing_under_keys_can_ever_be_staged(self):
        for rel in ("keys/sa.json", "keys/target/admin.json", "a/keys/x.json",
                    "keys", "data/migration.db", "backups/2026-09-01.tar.gz",
                    "env.sh", "node.env", "identities.csv", "target-sa.json",
                    "migration.db", "migration.db-wal", "cert.pem",
                    "client.p12", "id_rsa.key", "logs/run.log",
                    "/etc/bitport/totp.env", "deadman.env", "deadman.git"):
            assert deadman._safe_to_push(rel) is False, rel

    def test_the_actual_source_still_goes(self):
        for rel in ("deadman.py", "drive_engine.py", "install.sh",
                    "migration-webui/src/App.tsx", "tests/test_deadman.py",
                    "package.json", "SCOPE.md"):
            assert deadman._safe_to_push(rel) is True, rel

    def test_the_allowlist_comes_from_the_remote_not_this_machine(self):
        """A file the remote has never tracked is never sent, so a stray
        keys/ directory cannot be swept in. Two independent filters fail
        independently."""
        src = _code(deadman.push_code)
        assert "ls-files" in src
        assert "add", "--" in src
        assert '"add", "."' not in src and "add -A" not in src

    def test_it_re_checks_what_git_actually_staged(self):
        """Checked AFTER staging rather than trusted before it: refusing to
        push is always better than publishing a key."""
        src = _code(deadman.push_code)
        assert "diff" in src and "--cached" in src
        assert "REFUSED to push" in src

    def test_the_token_never_reaches_the_process_table(self):
        """https://<token>@github.com/... is the obvious way to do this and
        every other account on this shared VPS can read it with `ps`."""
        src = _code(deadman.push_code)
        assert "credential.helper" in src
        assert f"https://{{token}}@" not in src
        assert "env=genv" in src

    def test_a_dry_run_pushes_nothing(self, monkeypatch, tmp_path):
        env = tmp_path / "git.env"
        env.write_text("DEADMAN_GIT_REMOTE=https://github.test/x/y\n"
                       "DEADMAN_GIT_TOKEN=secret\n")
        monkeypatch.setattr(deadman, "GIT_ENV", str(env))
        monkeypatch.setattr(deadman, "EMAIL_ENV", str(tmp_path / "absent"))
        out = deadman.push_code({}, dry=True)
        assert any("WOULD push" in line for line in out)

    def test_an_empty_remote_is_not_reported_as_up_to_date(self):
        """The allowlist IS the remote's file list, so an empty one means
        nothing can ever be staged -- and the next branch called that
        "nothing to push, the deployed code matches the remote", a false
        all-clear delivered moments before the code is destroyed. Seen for
        real: a --depth 1 clone of a repo whose HEAD names a branch that
        does not exist comes back empty and silent."""
        src = _code(deadman.push_code)
        assert "if not tracked:" in src
        assert src.index("if not tracked:") < src.index("matches the remote")

    def test_it_says_so_rather_than_silently_not_pushing(self, monkeypatch, tmp_path):
        """Believing the code is being saved when it is not is worse than
        knowing it is not."""
        monkeypatch.setattr(deadman, "GIT_ENV", str(tmp_path / "absent"))
        monkeypatch.setattr(deadman, "EMAIL_ENV", str(tmp_path / "absent"))
        out = deadman.push_code({}, dry=False)
        assert any("NOT pushed" in line for line in out)


class TestTheWarningTellsTheTruthAboutStoppingIt:
    def test_under_require_checkin_only_the_code_is_offered(self):
        """It said "sign in to the web UI" in every mode. Under
        require_checkin that is worse than saying nothing: they sign in, see
        that they are signed in, believe they are safe, and the machine is
        destroyed on schedule anyway."""
        how = deadman._how_to_stop({"require_checkin": True})
        assert "2-Step code" in how
        assert "NOT enough" in how
        assert "sign in to the web UI\n" not in how

    def test_otherwise_the_incidental_signals_are_still_listed(self):
        how = deadman._how_to_stop({})
        assert "ssh to the box" in how

    def test_disarming_is_offered_in_both(self):
        for cfg in ({}, {"require_checkin": True}):
            assert "--disarm" in deadman._how_to_stop(cfg)
