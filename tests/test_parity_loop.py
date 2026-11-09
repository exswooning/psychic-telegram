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
    monkeypatch.setattr(P, "check_counts", lambda *a: calls.append("count") or (["count parity 0.4"], {"countParity": 0.4}))
    monkeypatch.setattr(P, "check_mail", lambda db, u: (calls.append("mail") or ([], {"owedToDms": 120})))
    said = []
    monkeypatch.setattr(P, "_say", lambda lvl, msg: said.append((lvl, msg)))
    P.handle(object(), db, settings, {SRC_USER.lower(): "t@t.x"}, SRC_USER, 10.0, {"drive": {}, "gmail": {}})
    rows = dict((s, v) for s, v in db.conn.execute("SELECT stage, verdict FROM parity_user").fetchall())
    assert rows == {"drive": "NOT_AT_PARITY", "gmail-awaiting-dms": "PARITY"}
    assert ("ERROR", f"{SRC_USER}: drive: drive mtime x1") in said and calls == ["drive", "mail", "count"]
