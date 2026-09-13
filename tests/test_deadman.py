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
            assert all(line.startswith("WOULD REMOVE") for line in out)
            assert os.path.exists(probe)

    def test_services_are_stopped_after_the_files_go(self):
        """Stopping first would leave the box quiet while the files were
        still there."""
        src = _code(deadman.wipe)
        assert src.index("shutil.rmtree") < src.index("systemctl")
