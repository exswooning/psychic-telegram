"""The source tenant is indexed as soon as a pair is ready -- but only the pair's OWN users.

A ledger outlives the pair it was built for, so scanning "whoever is mapped" right after a link
could read the previous pair's tenant. `_start_discovery` says why it did not start instead."""
import pytest

import api_server as A
from db import MigrationDB


@pytest.fixture
def launched(monkeypatch, tmp_path):
    class Seen(list):
        path = ""
    seen = Seen()
    path = str(tmp_path / "ledger.db")
    monkeypatch.setattr(A, "_ledger_path", lambda aid: path)
    monkeypatch.setattr(A, "_run_admitted", lambda argv, aid, name, env=None, then=None: seen.append((argv, aid, name)) or (True, "started pid 7"))
    seen.path = path
    return seen


def _map(path, *pairs):
    db = MigrationDB(path)
    for s, t in pairs:
        db.conn.execute("INSERT INTO identity_map(source_email, target_email, entity_type) VALUES (?,?,'user')", (s, t))
    db.conn.commit()


def test_it_starts_a_scan_of_the_source_when_the_ledger_maps_this_pair(launched):
    _map(launched.path, ("a@src.com", "a@tgt.com"), ("b@src.com", "b@tgt.com"))
    ok, _ = A._start_discovery(3, "SRC.com", "tgt.com")
    argv, aid, name = launched[0]
    assert ok and name == "discover" and aid == 3
    assert argv[-2:] == ["discover", "--include-mail"] and "--account-id" in argv


def test_a_pair_with_no_mapped_users_says_so_instead_of_scanning_nothing(launched):
    ok, why = A._start_discovery(3, "src.com", "tgt.com")
    assert not ok and "no users are mapped" in why and not launched


def test_a_ledger_left_by_another_pair_is_never_scanned(launched):
    _map(launched.path, ("a@old.com", "a@tgt.com"))
    ok, why = A._start_discovery(3, "src.com", "tgt.com")
    assert not ok and "old.com" in why and not launched


def test_a_user_with_no_target_yet_does_not_look_like_another_pair(launched):
    _map(launched.path, ("a@src.com", ""))
    assert A._start_discovery(3, "src.com", "tgt.com")[0] and launched


class _Proc:
    def __init__(self, rc):
        self.rc = rc

    def wait(self):
        return self.rc


def test_after_an_identity_map_build_it_follows_only_a_clean_one(launched, monkeypatch):
    import config

    class S:
        source_domain, target_domain = "src.com", "tgt.com"
    monkeypatch.setattr(config, "Settings", lambda account_id=None: S())
    _map(launched.path, ("a@src.com", "a@tgt.com"))
    A._discover_when_mapped(_Proc(1), 3)
    assert not launched
    A._discover_when_mapped(_Proc(0), 3)
    assert [n for _, _, n in launched] == ["discover"]
