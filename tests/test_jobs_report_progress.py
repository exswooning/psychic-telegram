"""Every long job counts itself as [done/total], the one thing the Jobs page turns into
Progress, a rate and an ETA. Delete users reported to its log file every 25th account,
tally printed "3/300" without the brackets the page reads, and a ledger reset, a verify
and a sharing restore printed no count at all: each showed "--" for its whole run."""
from __future__ import annotations

import webui
import wipe_target
from db import bulk_seed_identities
from tests.conftest import SRC_USER, TGT_USER


def test_delete_users_counts_every_account():
    class Dir:
        def users(self):
            return self

        def delete(self, userKey):
            return type("C", (), {"execute": lambda s: {}})()
    seen = []
    wipe_target.delete_users(Dir(), ["a@t", "b@t", "c@t"], dry_run=False,
                             on_progress=lambda i, n, e: seen.append((i, n, e)))
    assert seen == [(1, 3, "a@t"), (2, 3, "b@t"), (3, 3, "c@t")]


def test_the_ledger_reset_numbers_each_user(monkeypatch, settings, db, capsys):
    import reset_drive_ledger
    bulk_seed_identities(db, [(SRC_USER, TGT_USER), ("bob@tenanta.com", "bob@tenantb.com")])
    monkeypatch.setattr(reset_drive_ledger, "Settings", lambda: settings)
    reset_drive_ledger.main(["--confirm-domain", settings.source_domain, "--yes"])
    lines = capsys.readouterr().out.splitlines()
    assert webui._counter_progress_pct(lines) == 100
    assert any("[1/2]" in l for l in lines)


def test_a_sharing_restore_numbers_each_user(auth, db, settings, identity):
    import repair
    db.set_identity_status(SRC_USER, "DONE")
    said = []
    repair.restore_direct_grants(auth, db, settings, progress=said.append)
    assert said[0].strip().startswith("[0/1]") and said[-1].strip().startswith("[1/1]")


def test_the_counts_each_job_prints_are_read():
    for line in ("tally: [3/300] ann@a.com", "verify: [3/300] ann@a.com checked",
                 "  [3/300] ann@a.com: 12 item(s), 0 to put back",
                 "  [3/300] Engineering", "  [3/300] ann@a.com"):
        assert webui._counter_progress_pct([line]) == 1.0, line


def test_a_mirror_pass_is_a_phase_of_its_own():
    lines = ["Mirror pass: Drive", "  [3/3] a", "Mirror pass: mail and calendar", "  [1/3] a"]
    assert webui._counter_progress_pct(webui._current_phase(lines)) == 33.33
