"""A tally that is not an exact copy sets its fix going: SHORT is migrated again for
just the short services, DIFFERS is repaired -- once per user per window, no loop."""
import pytest

import api_server
from db import MigrationDB


@pytest.fixture
def wired(tmp_path, monkeypatch):
    path = str(tmp_path / "l.db")
    db = MigrationDB(path)
    with db.write() as conn:
        for u in ("short@src", "differs@src", "fine@src", "owed@src"):
            conn.execute("INSERT INTO identity_map(source_email, target_email, entity_type, status) "
                         "VALUES (?, ?, 'user', 'DONE')", (u, u.replace("src", "tgt")))
    db.set_services_done("short@src", {"drive", "gmail", "calendar"})
    view = {"users": [
        {"user": "short@src", "verdict": "SHORT", "services": {
            "mail": {"expected": 100, "target": 97}, "drive_files": {"expected": 5, "target": 5}}},
        {"user": "differs@src", "verdict": "DIFFERS", "services": {}},
        {"user": "fine@src", "verdict": "COMPLETE", "services": {}},
        {"user": "owed@src", "verdict": "OWED_TO_DMS", "services": {}},
    ]}
    seen = {"run": [], "repair": [], "tally": []}
    monkeypatch.setattr(api_server, "_tally_view", lambda a: view)
    monkeypatch.setattr(api_server, "_ledger_path", lambda a: path)
    monkeypatch.setattr(api_server, "_run_admitted",
                        lambda argv, a, name, **k: seen["run"].append((argv, name, k.get("then"))) or (True, "started"))
    monkeypatch.setattr(api_server, "_start_repair", lambda a, why, **k: seen["repair"].append(why) or (True, ""))
    monkeypatch.setattr(api_server, "_start_tally_after_repair",
                        lambda a, users=None: seen["tally"].append(users) or (True, ""))
    return db, seen


def test_short_is_migrated_again_for_only_what_is_short(wired):
    db, seen = wired
    api_server._fix_after_tally(3)
    (argv, name, then), = seen["run"]
    assert name == "migrate" and argv[argv.index("--services") + 1] == "gmail"
    assert argv[argv.index("--user") + 1] == "short@src" and "fine@src" not in argv
    assert then == ["repair", "tally@short@src"]
    assert db.services_done("short@src") == {"drive", "calendar"}      # gmail reopened, nothing else


def test_differs_is_repaired_and_tallied_again(wired):
    db, seen = wired
    api_server._fix_after_tally(3)
    assert len(seen["repair"]) == 1 and seen["tally"] == [["differs@src"]]


def test_complete_and_owed_are_left_alone_and_nothing_loops(wired):
    db, seen = wired
    api_server._fix_after_tally(3)
    assert "owed@src" not in str(seen) and "fine@src" not in str(seen["run"])
    seen["run"].clear(); seen["repair"].clear(); seen["tally"].clear()
    api_server._fix_after_tally(3)              # within the window: the same gaps are not retried
    assert seen == {"run": [], "repair": [], "tally": []}


def test_only_the_named_users_are_looked_at(wired):
    db, seen = wired
    api_server._fix_after_tally(3, ["differs@src"])
    assert seen["run"] == [] and len(seen["repair"]) == 1


def test_follow_ons_carry_their_users():
    assert api_server._tally_follow("tally", ["a@x", "b@x"]) == "tally@a@x,b@x"
    assert api_server._follow_users("tally_fix@a@x,b@x") == ["a@x", "b@x"]
    assert api_server._follow_users("tally") is None
