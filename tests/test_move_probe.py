"""move_probe.py: the shared-drive route a move would need, tried on a one-byte file, step by step."""
import pytest

import domain_guard
import move_probe
from tests.conftest import SRC_USER, TGT_USER


def _src(auth):
    return auth._get("source", "drive", SRC_USER)


def _tgt(auth):
    return auth._get("target", "drive", TGT_USER)


@pytest.fixture
def probe(auth, settings, monkeypatch):
    settings.source_admin, settings.target_admin = SRC_USER, TGT_USER
    monkeypatch.setattr(domain_guard, "source_refuse_reason", lambda *a, **k: "")   # a declared sandbox
    return auth, settings


def _by_route(results):
    return {r["route"]: r for r in results}


def _nothing_left(auth):
    src, tgt = _src(auth), _tgt(auth)
    assert [f for f in list(src.store.values()) + list(tgt.store.values())
            if f.get("name", "").startswith("Bitport move probe")] == []
    assert src.shared_drives == {} and tgt.shared_drives == {}


def test_both_routes_work_when_the_source_lets_content_leave(probe):
    auth, settings = probe
    got = _by_route(move_probe.run(auth, settings))
    assert got["direct"]["ok"] is True and got["source-drive"]["ok"] is True
    _nothing_left(auth)


def test_a_source_that_blocks_distribution_is_refused_on_both_routes_and_says_what_to_change(probe):
    auth, settings = probe
    _src(auth).distribution_blocked = True
    got = _by_route(move_probe.run(auth, settings))
    for route in ("direct", "source-drive"):
        assert got[route]["ok"] is False and got[route]["status"] == 403
        assert "Distributing content outside" in got[route]["detail"]
    assert got["direct"]["step"] == "move it out of the source"
    assert got["source-drive"]["step"] == "the target admin takes it out"
    _nothing_left(auth)


def test_a_protected_source_is_never_written_to_and_the_probe_says_so(auth, settings, monkeypatch):
    settings.source_admin, settings.target_admin = SRC_USER, TGT_USER
    src = _src(auth)
    before = (dict(src.store), dict(src.shared_drives))
    # (the suite's own SOURCE_DOMAIN is a declared sandbox; here the guard is made to refuse)
    monkeypatch.setattr(domain_guard, "source_refuse_reason", lambda *a, **k: "protected")
    got = move_probe.run(auth, settings)
    assert [r["ok"] for r in got] == [None, None] and "protected source" in got[0]["detail"]
    assert (dict(src.store), dict(src.shared_drives)) == before


def test_results_are_kept_per_pair_and_read_back(probe, db):
    auth, settings = probe
    move_probe.save(db, settings, move_probe.run(auth, settings))
    assert {r["route"]: r["ok"] for r in move_probe.latest(db, settings)} == {"direct": True, "source-drive": True}
    settings.target_domain = "another.example"            # another pair: nothing is known about it
    assert move_probe.latest(db, settings) == []
