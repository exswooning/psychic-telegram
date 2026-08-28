"""There were no backups. Not incomplete ones -- none.

The only record of what a migration moved lived in one SQLite file on one
VPS: 5.7 GB for account 7, 0.4 GB for account 66, on a disk 67% full. That
file is what makes a re-run idempotent and lets the tool say which items
failed, so losing it does not lose "some logs" -- it loses the ability to
finish or explain a migration that is already half done.

Two properties matter more than the rest:

  * the copy is taken with VACUUM INTO, not `cp`. A live SQLite file has
    committed pages in the WAL that the main file does not have yet, so a
    file copy during a migration is torn.
  * the copy is integrity-checked before it counts. A backup nobody has
    verified is a belief, and the moment you need it is the worst moment
    to find out it was truncated.
"""
import gzip
import os
import sqlite3

import pytest

import backup_db


def _db(path, rows=50):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    c.executemany("INSERT INTO t (v) VALUES (?)", [(f"row-{i}",) for i in range(rows)])
    c.commit()
    c.close()
    return path


class TestItTakesAConsistentCopy:
    def test_the_copy_has_the_same_rows(self, tmp_path):
        src = _db(str(tmp_path / "migration.db"), rows=120)
        dest = tmp_path / "b"
        dest.mkdir()
        out = backup_db.backup_one("acct", src, str(dest))
        assert out["ok"], out["detail"]
        copy = [f for f in os.listdir(dest) if f.endswith(".db")][0]
        n = sqlite3.connect(str(dest / copy)).execute(
            "SELECT COUNT(*) FROM t").fetchone()[0]
        assert n == 120

    def test_it_survives_an_open_writer(self, tmp_path):
        """The case a file copy gets wrong: pages committed to the WAL that
        the main file does not have yet."""
        src = _db(str(tmp_path / "migration.db"))
        live = sqlite3.connect(src)
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("INSERT INTO t (v) VALUES ('during')")
        live.commit()
        dest = tmp_path / "b"; dest.mkdir()
        out = backup_db.backup_one("acct", src, str(dest))
        live.close()
        assert out["ok"], out["detail"]
        copy = [f for f in os.listdir(dest) if f.endswith(".db")][0]
        got = sqlite3.connect(str(dest / copy)).execute(
            "SELECT COUNT(*) FROM t WHERE v='during'").fetchone()[0]
        assert got == 1, "the WAL-committed row was lost"

    def test_it_verifies_before_reporting_success(self):
        import inspect
        src = inspect.getsource(backup_db.backup_one)
        assert "integrity_check" in src or "verify(" in src

    def test_a_corrupt_copy_is_deleted_not_kept(self, tmp_path, monkeypatch):
        # A bad backup left on disk is worse than none: it looks like cover.
        src = _db(str(tmp_path / "migration.db"))
        dest = tmp_path / "b"; dest.mkdir()
        monkeypatch.setattr(backup_db, "verify", lambda p: (False, "malformed"))
        out = backup_db.backup_one("acct", src, str(dest))
        assert not out["ok"]
        assert not [f for f in os.listdir(dest) if f.endswith(".db")]


class TestItRefusesRatherThanFillingTheDisk:
    def test_no_space_means_no_attempt(self, tmp_path, monkeypatch):
        src = _db(str(tmp_path / "migration.db"))
        dest = tmp_path / "b"; dest.mkdir()
        monkeypatch.setattr(backup_db, "free_bytes", lambda p: 10)
        out = backup_db.backup_one("acct", src, str(dest))
        assert not out["ok"]
        assert "refusing" in out["detail"]

    def test_the_message_names_the_numbers(self, tmp_path, monkeypatch):
        src = _db(str(tmp_path / "migration.db"))
        dest = tmp_path / "b"; dest.mkdir()
        monkeypatch.setattr(backup_db, "free_bytes", lambda p: 10)
        d = backup_db.backup_one("acct", src, str(dest))["detail"]
        assert "GB free" in d and "available" in d

    def test_headroom_is_more_than_the_file_itself(self):
        # VACUUM INTO needs room for the copy while the original exists.
        assert backup_db.SPACE_FACTOR > 1.0


class TestRotation:
    def _make(self, dest, label, n):
        for i in range(n):
            open(os.path.join(dest, f"{label}-2026010{i}T000000Z.db"), "w").close()

    def test_it_keeps_the_newest(self, tmp_path):
        d = str(tmp_path); self._make(d, "acct", 5)
        backup_db.rotate("acct", d, keep=2)
        left = sorted(f for f in os.listdir(d))
        assert len(left) == 2 and left[-1].endswith("4T000000Z.db")

    def test_one_ledger_cannot_evict_another(self, tmp_path):
        """A global count would let a busy tenant's backups push out the
        control plane's -- the one that knows the others exist."""
        d = str(tmp_path)
        self._make(d, "account-7", 5)
        self._make(d, "control-plane", 2)
        backup_db.rotate("account-7", d, keep=1)
        assert len([f for f in os.listdir(d) if f.startswith("control-plane")]) == 2

    def test_keep_zero_is_ignored_not_obeyed(self, tmp_path):
        # Deleting every backup is never what a rotation setting means.
        d = str(tmp_path); self._make(d, "acct", 3)
        backup_db.rotate("acct", d, keep=0)
        assert len(os.listdir(d)) == 3

    def test_compressed_copies_rotate_too(self, tmp_path):
        d = str(tmp_path)
        for i in range(4):
            open(os.path.join(d, f"acct-2026010{i}T000000Z.db.gz"), "w").close()
        backup_db.rotate("acct", d, keep=1)
        assert len(os.listdir(d)) == 1


class TestItFindsEveryLedger:
    def test_control_plane_and_each_account(self, tmp_path):
        root = tmp_path
        _db(str(root / "migration.db"))
        for a in ("7", "66"):
            (root / "data" / "accounts" / a).mkdir(parents=True)
            _db(str(root / "data" / "accounts" / a / "migration.db"))
        found = dict(backup_db.ledgers(str(root)))
        assert "control-plane" in found
        assert "account-7" in found and "account-66" in found

    def test_a_missing_data_dir_is_not_a_crash(self, tmp_path):
        _db(str(tmp_path / "migration.db"))
        assert len(backup_db.ledgers(str(tmp_path))) == 1


class TestVerifyIsUsableOnItsOwn:
    def test_a_good_file_passes(self, tmp_path):
        ok, _ = backup_db.verify(_db(str(tmp_path / "a.db")))
        assert ok

    def test_a_truncated_file_fails(self, tmp_path):
        p = _db(str(tmp_path / "a.db"))
        with open(p, "r+b") as fh:
            fh.truncate(200)
        assert backup_db.verify(p)[0] is False

    def test_it_can_check_a_gzipped_backup(self, tmp_path):
        p = _db(str(tmp_path / "a.db"))
        gz = str(tmp_path / "a.db.gz")
        with open(p, "rb") as s, gzip.open(gz, "wb") as d:
            d.write(s.read())
        assert backup_db.verify(gz)[0] is True

    def test_verifying_leaves_no_temp_behind(self, tmp_path):
        p = _db(str(tmp_path / "a.db"))
        gz = str(tmp_path / "a.db.gz")
        with open(p, "rb") as s, gzip.open(gz, "wb") as d:
            d.write(s.read())
        backup_db.verify(gz)
        assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]


class TestOneBadLedgerDoesNotStopTheRest:
    def test_run_continues_past_a_failure(self, tmp_path, monkeypatch):
        root = tmp_path
        _db(str(root / "migration.db"))
        (root / "data" / "accounts" / "7").mkdir(parents=True)
        _db(str(root / "data" / "accounts" / "7" / "migration.db"))
        monkeypatch.setattr(backup_db, "HERE", str(root))
        calls = []
        real = backup_db.backup_one

        def flaky(label, src, dest, compress=False, stamp=None):
            calls.append(label)
            if label == "control-plane":
                return {"label": label, "ok": False, "detail": "boom", "src": src}
            return real(label, src, dest, compress, stamp)

        monkeypatch.setattr(backup_db, "backup_one", flaky)
        out = backup_db.run(str(root / "b"), keep=2, compress=False)
        assert len(calls) == 2, "it stopped at the first failure"
        assert out["ok"] is False


class TestItIsActuallyScheduledAndReachable:
    """A backup tool nobody runs is not a backup.

    Two ways to be unrunnable, and this codebase has produced both before:
    an ACTIONS entry with no button (repair_modified_times, unreachable
    since it was written) and a systemd unit never copied to the box
    (xvfb.service, which the deploy script's own comment records finding
    uninstalled while the whole browser path depended on it).
    """

    def _root(self):
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_there_is_a_timer_not_only_a_service(self):
        sd = os.path.join(self._root(), "systemd")
        assert os.path.isfile(os.path.join(sd, "bitport-backup.timer"))
        assert os.path.isfile(os.path.join(sd, "bitport-backup.service"))

    def test_the_deploy_copies_timers(self):
        # It copied *.service only, so a timer would have stayed in git.
        sh = open(os.path.join(self._root(), "sync_vps.sh"), encoding="utf-8").read()
        assert "*.timer" in sh

    def test_the_deploy_enables_them(self):
        """An installed timer nobody started looks exactly like one that was
        never installed."""
        sh = open(os.path.join(self._root(), "sync_vps.sh"), encoding="utf-8").read()
        assert "systemctl enable --now" in sh

    def test_a_timer_that_cannot_be_enabled_is_reported(self):
        sh = open(os.path.join(self._root(), "sync_vps.sh"), encoding="utf-8").read()
        assert "will not fire" in sh

    def test_the_timer_catches_up_after_downtime(self):
        t = open(os.path.join(self._root(), "systemd/bitport-backup.timer"),
                 encoding="utf-8").read()
        assert "Persistent=true" in t, "a box that was off skips the day silently"

    def test_the_backup_yields_to_the_migration(self):
        # It runs while migrations do; starving them would be a bad trade.
        u = open(os.path.join(self._root(), "systemd/bitport-backup.service"),
                 encoding="utf-8").read()
        assert "IOSchedulingClass=idle" in u and "Nice=" in u

    def test_it_has_a_button(self):
        import webui
        assert "backup_now" in webui.ACTIONS
        page = open(os.path.join(self._root(),
                                 "migration-webui/src/pages/Maintenance.tsx"),
                    encoding="utf-8").read()
        assert "backup_now" in page
