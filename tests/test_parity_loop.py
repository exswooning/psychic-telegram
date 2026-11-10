"""The parity loop: a finished user is checked for the service that finished, and mail owed to the DMS
waits rather than failing."""
import json

import parity_loop as P
from tests.conftest import SRC_USER


def test_the_finished_line_is_read():
    line = ('2026-11-10 20:31:00,123 INFO    [ward7] migrate: [ward7@s.x] finished in 212.5s: '
            '{"drive": {"files": 12, "failed": 0}}')
    m = P.FINISHED.search(line)
    assert m.group(1) == "ward7@s.x" and float(m.group(2)) == 212.5 and json.loads(m.group(3))["drive"]["files"] == 12


def test_a_user_is_routed_to_the_checks_for_what_finished(db, settings, monkeypatch):
    calls = []
    monkeypatch.setattr(P, "check_drive", lambda *a: calls.append("drive") or ["drive mtime x1"])
    monkeypatch.setattr(P, "check_counts", lambda *a: calls.append("count") or (["gmail count 5 of 12"], {"countParity": 0.4}))
    monkeypatch.setattr(P, "check_mail", lambda db, u: (calls.append("mail") or ([], {"owedToDms": 120})))
    said = []
    monkeypatch.setattr(P, "_say", lambda lvl, msg: said.append((lvl, msg)))
    P.handle(object(), db, settings, {SRC_USER.lower(): "t@t.x"}, SRC_USER, 10.0, {"drive": {}, "gmail": {}})
    rows = dict((s, v) for s, v in db.conn.execute("SELECT stage, verdict FROM parity_user").fetchall())
    assert rows == {"drive": "NOT_AT_PARITY", "gmail-awaiting-dms": "PARITY"}
    assert ("ERROR", f"{SRC_USER}: drive: drive mtime x1") in said and calls == ["drive", "mail", "count"]


def test_a_service_is_judged_on_its_own_counts_not_the_overall_parity(db, settings, monkeypatch):
    """10 Nov 2026: mail owed to the DMS dragged the tally's overall parity to 0.04 and every calendar,
    contacts and tasks check raised a false alarm."""
    import tally
    payload = {"countParity": 0.04, "services": {"mail": {"source": 754, "target": 32, "expected": 754, "parity": 0.04},
                                                  "calendar": {"source": 3, "target": 3, "expected": 3, "parity": 1.0},
                                                  "tasks": {"source": 0, "target": 0, "expected": 0, "parity": None}}}
    monkeypatch.setattr(tally, "tally_user_and_save", lambda *a, **k: payload)
    assert P.check_counts(None, db, settings, "u@s", "u@t", "calendar")[0] == []
    assert P.check_counts(None, db, settings, "u@s", "u@t", "tasks")[0] == []
    assert P.check_counts(None, db, settings, "u@s", "u@t", "gmail")[0] == ["gmail count 32 of 754"]
