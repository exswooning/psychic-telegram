"""A ledger serves one source -> target pair; another pair's run archives it and starts
fresh, so nothing from a previous pair reaches the next (live: a new pair set up on
account 3 was offered the old pair's 300 users to provision)."""
import os
import sqlite3

import db as ledger
from db import MigrationDB


def _with_users(path, src_dom, tgt_dom, n=3):
    d = MigrationDB(path)
    with d.write() as c:
        for i in range(n):
            c.execute("INSERT INTO identity_map (source_email, target_email, status) VALUES (?,?,?)",
                      (f"u{i}@{src_dom}", f"u{i}@{tgt_dom}", "DONE"))
    d.close()


def _users(path):
    with sqlite3.connect(path) as c:
        return c.execute("SELECT COUNT(*) FROM identity_map").fetchone()[0]


def test_a_new_ledger_is_stamped_and_kept(tmp_path):
    p = str(tmp_path / "m.db")
    assert ledger.claim_for_pair(p, "a.com", "b.com") is None
    _with_users(p, "a.com", "b.com")
    assert ledger.claim_for_pair(p, "a.com", "b.com") is None       # same pair: resumes
    assert _users(p) == 3


def test_another_pair_archives_it_and_starts_fresh(tmp_path):
    p = str(tmp_path / "m.db")
    ledger.claim_for_pair(p, "a.com", "b.com")
    _with_users(p, "a.com", "b.com")
    archived = ledger.claim_for_pair(p, "gorkhatweed.com", "b.com")
    assert archived and os.path.exists(archived) and ".pair-a.com-b.com-" in archived
    assert _users(archived) == 3                                     # kept, not deleted
    assert _users(p) == 0
    with sqlite3.connect(p) as c:
        assert c.execute("SELECT source_domain, target_domain FROM ledger_pair").fetchone() \
            == ("gorkhatweed.com", "b.com")


def test_only_the_target_changing_also_archives_a_stamped_ledger(tmp_path):
    p = str(tmp_path / "m.db")
    ledger.claim_for_pair(p, "a.com", "b.com")
    _with_users(p, "a.com", "b.com")
    assert ledger.claim_for_pair(p, "a.com", "c.com")
    assert _users(p) == 0


def test_an_unstamped_ledger_is_judged_by_its_source_users(tmp_path):
    p = str(tmp_path / "m.db")
    _with_users(p, "a.com", "alias.b.com")                         # no stamp: from before it
    assert ledger.claim_for_pair(p, "a.com", "b.com") is None       # target on an alias: kept
    assert _users(p) == 3
    q = str(tmp_path / "n.db")
    _with_users(q, "old.com", "b.com")
    assert ledger.claim_for_pair(q, "new.com", "b.com")
    assert _users(q) == 0


def test_no_pair_configured_touches_nothing(tmp_path):
    p = str(tmp_path / "m.db")
    _with_users(p, "a.com", "b.com")
    assert ledger.claim_for_pair(p, "", "b.com") is None
    assert _users(p) == 3
