"""The source guard: counts of everything before, a comparison while the migration is open,
and Google's own audit log read for anything this tool's addresses changed."""
import source_guard as G
from db import bulk_seed_identities


def _state(files=None, mail=10, errors=None):
    return {"files": files or {}, "counts": {"mail": mail, "calendar": 3, "contacts": 2}, "errors": errors or {}}


def test_compare_names_what_was_deleted_created_modified_and_counts_that_fell():
    before = _state({"a": ["Plan", "2026-01-01"], "b": ["Budget", "2026-01-02"]}, mail=10)
    after = _state({"a": ["Plan", "2026-02-01"], "c": ["Copy of Form", "2026-10-19"]}, mail=9)
    got = {(c["what"], c.get("name")) for c in G.compare(before, after)}
    assert got == {("file gone (deleted or trashed)", "Budget"), ("file created", "Copy of Form"),
                   ("file modified", "Plan"), ("mail count went down", None)}
    assert G.compare(_state(mail=5), _state(mail=9)) == []          # mail arriving is not a change


def test_a_check_finds_a_change_and_what_bitport_did_in_the_log(db, settings):
    bulk_seed_identities(db, [("a@src", "a@tgt")])
    world = {"a@src": _state({"f1": ["Form", "2026-01-01"]})}
    G.snapshot(None, settings, db, read=lambda auth, st, u: world[u])
    assert G.check(None, settings, db, "42", read=lambda auth, st, u: world[u],
                   audit_fn=lambda st, since, cid: {"byBitport": [], "deletionsByOthers": []})["verdict"] == "CLEAN"
    world["a@src"] = _state({"f1": ["Form", "2026-01-01"], "f2": ["Copy of Form", "2026-10-19"]})
    bitport = [{"event": "copy", "item": "Copy of Form"}]
    rep = G.check(None, settings, db, "42", read=lambda auth, st, u: world[u],
                  audit_fn=lambda st, since, cid: {"byBitport": bitport, "deletionsByOthers": []})
    assert rep["verdict"] == "CHANGED" and rep["changes"][0]["name"] == "Copy of Form"
    assert rep["auditLog"]["byBitport"] == bitport
    assert G.latest(db)["latest"]["verdict"] == "CHANGED"


def test_what_could_not_be_read_is_incomplete_never_clean(db, settings):
    bulk_seed_identities(db, [("a@src", "a@tgt")])
    G.snapshot(None, settings, db, read=lambda auth, st, u: _state())
    rep = G.check(None, settings, db, "42", read=lambda auth, st, u: _state(errors={"drive": "403"}),
                  audit_fn=lambda st, since, cid: {"byBitport": [], "deletionsByOthers": []})
    assert rep["verdict"] == "INCOMPLETE"
    rep = G.check(None, settings, db, "42", read=lambda auth, st, u: _state(),
                  audit_fn=lambda st, since, cid: (_ for _ in ()).throw(RuntimeError("scope refused")))
    assert rep["verdict"] == "INCOMPLETE" and "scope refused" in rep["auditLog"]["error"]


def test_the_audit_log_learns_this_tools_addresses_from_its_own_token_log():
    def ev(ip, name, **p):
        return ({"ipAddress": ip, "id": {"time": "t"}, "actor": {"email": "u@src"}}, {"name": name}, p)
    log = {"token": [ev("9.9.9.9", "authorize", client_id="42"), ev("1.1.1.1", "authorize", client_id="7")],
           "drive": [ev("9.9.9.9", "download", doc_title="read only"), ev("9.9.9.9", "create", doc_title="Copy of Form"),
                     ev("5.5.5.5", "trash", doc_title="person binned it"), ev("1.1.1.1", "edit", doc_title="other app")],
           "admin": [], "calendar": []}
    out = G.audit(None, "since", "42", events=lambda app: log[app])
    assert out["bitportAddresses"] == ["9.9.9.9"]
    assert [r["item"] for r in out["byBitport"]] == ["Copy of Form"]
    assert [r["item"] for r in out["deletionsByOthers"]] == ["person binned it"]


def test_no_baseline_is_said_not_passed(db, settings):
    assert G.check(None, settings, db, "42", read=lambda *a: _state())["verdict"] == "NO_SNAPSHOT"


def test_the_loop_guards_an_open_migration_and_stops_it_on_a_bitport_change(monkeypatch, tmp_path):
    import json
    import api_server
    import domain_guard
    import lifecycle
    import run_watch
    import source_guard
    ledger = tmp_path / "m.db"
    ledger.write_text("")
    key = tmp_path / "k.json"
    key.write_text(json.dumps({"client_id": "42"}))
    monkeypatch.setattr(api_server, "_account_db_path", lambda aid: str(ledger))

    class St:
        source_domain, source_sa_key = "a.gov.np", str(key)
    monkeypatch.setattr("config.Settings", lambda account_id=None: St())

    class D:
        def __init__(self, path): pass
        def close(self): pass
    monkeypatch.setattr("db.MigrationDB", D)
    monkeypatch.setattr("auth.AuthManager", lambda st: None)
    monkeypatch.setattr(source_guard, "snapshot_taken", lambda d: "2026-10-19")
    opened = []
    monkeypatch.setattr(run_watch, "open_incident", lambda **k: opened.append(k) or (1, True))
    monkeypatch.setattr(source_guard, "check", lambda *a, **k: {
        "verdict": "CHANGED", "auditLog": {"byBitport": [{"app": "admin", "event": "AUTHORIZE_API_CLIENT_ACCESS", "item": ""},
                                                         {"app": "drive", "event": "copy", "item": "Copy of Form"}]}})
    monkeypatch.setattr(lifecycle, "state", lambda aid: {})
    stops = []
    monkeypatch.setattr(api_server, "_stop_account_jobs", lambda aid, why: stops.append((aid, why)) or 1)
    assert api_server._source_guard_one(5)["verdict"] == "CHANGED"
    assert opened and opened[0]["kind"] == "source_changed" and "Copy of Form" in opened[0]["summary"]
    assert stops and stops[0][0] == 5 and "Copy of Form" in stops[0][1]      # and the run is stopped
    monkeypatch.setattr(lifecycle, "state", lambda aid: {"approved_at": "2026-10-20"})
    assert api_server._source_guard_one(5) is None                     # approved: no longer guarded
    monkeypatch.setattr(domain_guard, "is_protected", lambda d: False)
    monkeypatch.setattr(lifecycle, "state", lambda aid: {})
    assert api_server._source_guard_one(5)["verdict"] == "CHANGED"     # a sandbox with a baseline: guarded too


def test_each_counted_difference_is_explained_by_the_audit_log():
    """The two sources check each other: a count difference is looked up by file id."""
    def ev(ip, name, who="u@src"):
        return ({"ipAddress": ip, "actor": {"email": who}}, {"name": name})
    log = {"f1": [ev("9.9.9.9", "download"), ev("9.9.9.9", "create")],       # this tool
           "f2": [ev(None, "copy")],                                         # Google itself
           "f3": [ev("5.5.5.5", "edit", "person@src")],                       # a person
           "f4": []}                                                         # log lagging
    changes = [{"what": "file created", "id": i} for i in ("f1", "f2", "f3", "f4")] + [{"what": "mail count went down"}]
    got = G.explain(changes, {"9.9.9.9"}, events_for=lambda i: log[i])
    assert got[0]["by"].startswith("this tool") and got[0]["byTool"]
    assert got[1]["by"].startswith("Google itself") and not got[1]["byTool"]
    assert got[2]["by"].startswith("a person: person@src from 5.5.5.5")
    assert got[3]["by"].startswith("not in Google's audit log yet")
    assert "by" not in got[4]                                                 # no id: nothing to look up



def test_google_acting_for_a_user_is_its_own_finding():
    """The 10-19 pattern: a cross-organisation copy made Google write 'Copy of' a Form into
    the owner's Drive -- no app, no address. It stops a run like a change of Bitport's."""
    def ev(ip, name, title):
        return ({"ipAddress": ip, "id": {"time": "t"}, "actor": {"email": "u@src"}}, {"name": name},
                {"doc_title": title})
    log = {"token": [ev("9.9.9.9", "authorize", "")], "drive": [ev(None, "copy", "Copy of Form"),
                                                               ev("5.5.5.5", "edit", "a person's edit")],
           "admin": [], "calendar": []}
    out = G.audit(None, "since", "42", events=lambda app: log[app])
    assert [r["item"] for r in out["byGoogle"]] == ["Copy of Form"] and out["byBitport"] == []


def test_the_fast_watch_stops_the_run_on_a_change(monkeypatch, tmp_path):
    import json
    import api_server
    import source_guard
    ledger = tmp_path / "m.db"
    ledger.write_text("")
    key = tmp_path / "k.json"
    key.write_text(json.dumps({"client_id": "42"}))
    monkeypatch.setattr(api_server, "_account_db_path", lambda aid: str(ledger))
    monkeypatch.setattr(api_server, "_account_jobs", lambda aid: [{"pid": 77, "job_name": "migrate"}])

    class St:
        source_domain, source_sa_key = "a.gov.np", str(key)
    monkeypatch.setattr("config.Settings", lambda account_id=None: St())

    class D:
        def __init__(self, path): pass
        def close(self): pass
    monkeypatch.setattr("db.MigrationDB", D)
    monkeypatch.setattr(source_guard, "snapshot_taken", lambda d: "2026-10-19")
    stops = []
    monkeypatch.setattr(api_server, "_stop_account_jobs", lambda aid, why: stops.append(why) or 1)
    monkeypatch.setattr(source_guard, "audit", lambda st, since, cid: {"byBitport": [], "byGoogle": []})
    assert api_server._source_guard_fast(5) == [] and stops == []
    monkeypatch.setattr(source_guard, "audit", lambda st, since, cid: {
        "byBitport": [], "byGoogle": [{"event": "copy", "item": "Copy of Form", "app": "drive"}]})
    assert api_server._source_guard_fast(5) and "Copy of Form" in stops[0]


def test_a_stop_by_the_guard_is_never_resumed_as_a_memory_pause():
    import run_watch
    assert "source-guard.auto-stop" in run_watch.STOP_ACTIONS


def test_first_changed_at_is_the_start_of_the_current_unbroken_run_of_changed(db):
    G.ensure_tables(db)
    with db.write() as c:
        for at, v in [("t1", "CHANGED"), ("t2", "CLEAN"), ("t3", "CHANGED"), ("t4", "CHANGED")]:
            c.execute("INSERT INTO source_guard_checks(checked_at, since, verdict, report) VALUES (?,?,?,?)",
                      (at, "s", v, "{}"))
    assert G.latest(db)["firstChangedAt"] == "t3"


# -- per-user check, kill on a change Bitport made ---------------------------------
def _hook(monkeypatch, db, settings, report):
    import domain_guard
    import main
    import run_watch
    import source_guard
    calls = {"kill": 0, "incidents": []}
    monkeypatch.setattr(domain_guard, "is_protected", lambda d: True)
    monkeypatch.setattr(source_guard, "snapshot_taken", lambda d: "t")
    if isinstance(report, Exception):
        def boom(*a, **k):
            raise report
        monkeypatch.setattr(source_guard, "check_user", boom)
    else:
        monkeypatch.setattr(source_guard, "check_user", lambda *a, **k: report)
    monkeypatch.setattr(main, "_kill_run", lambda: calls.__setitem__("kill", calls["kill"] + 1))
    monkeypatch.setattr(run_watch, "open_incident", lambda **k: calls["incidents"].append(k))
    result = {}
    main._guard_source_after_user(None, db, settings, "a@src", result)
    return calls, result


def test_a_change_bitport_made_kills_the_run_and_opens_an_incident(monkeypatch, db, settings):
    rep = {"verdict": "CHANGED", "changes": [], "byBitport": [{"event": "copy", "item": "Copy of Form"}]}
    calls, _ = _hook(monkeypatch, db, settings, rep)
    assert calls["kill"] == 1 and calls["incidents"][0]["kind"] == "source_changed_by_bitport"
    assert db.conn.execute("SELECT COUNT(*) FROM audit_log WHERE status='SOURCE_CHANGED_BY_BITPORT'").fetchone()[0] == 1


def test_a_change_by_the_sources_own_user_is_recorded_not_fatal(monkeypatch, db, settings):
    rep = {"verdict": "CHANGED", "byBitport": [], "changes": [{"what": "file modified", "name": "Plan"}]}
    calls, result = _hook(monkeypatch, db, settings, rep)
    assert calls["kill"] == 0 and calls["incidents"] == [] and result["source_guard"] == rep
    assert db.conn.execute("SELECT COUNT(*) FROM audit_log WHERE status='SOURCE_CHANGED_BY_USER'").fetchone()[0] == 1


def test_a_check_that_raises_or_is_incomplete_never_kills(monkeypatch, db, settings):
    assert _hook(monkeypatch, db, settings, RuntimeError("boom"))[0]["kill"] == 0
    assert _hook(monkeypatch, db, settings, {"verdict": "INCOMPLETE", "byBitport": [], "changes": []})[0]["kill"] == 0


def test_check_user_splits_bitport_from_a_person(db, settings, tmp_path):
    bulk_seed_identities(db, [("a@src", "a@tgt")])
    G.snapshot(None, settings, db, read=lambda *a: _state({"f1": ["Plan", "t1"]}))
    key = tmp_path / "k.json"
    key.write_text('{"client_id": "42"}')
    settings.source_sa_key = str(key)
    log = {"byBitport": [{"actor": "a@src", "app": "drive", "event": "create", "item": "x"},
                         {"actor": "b@src", "app": "drive", "event": "create", "item": "other user"}],
           "byGoogle": [], "deletionsByOthers": [{"actor": "a@src", "event": "trash", "item": "Plan"}]}
    rep = G.check_user(settings, None, "a@src", "since", db=db, read=lambda *a: _state({"f1": ["Plan", "t1"]}),
                       audit_fn=lambda *a: log)
    assert [r["item"] for r in rep["byBitport"]] == ["x"] and rep["verdict"] == "CHANGED"
    assert rep["changes"][0]["name"] == "Plan"
    clean = G.check_user(settings, None, "a@src", "since", db=db, read=lambda *a: _state({"f1": ["Plan", "t1"]}),
                         audit_fn=lambda *a: {})
    assert clean["verdict"] == "CLEAN"


def test_stop_account_jobs_escalates_to_sigkill_after_30s(monkeypatch):
    import api_server
    import run_watch
    sent, sleeps = [], []
    monkeypatch.setattr(api_server, "_account_jobs", lambda aid: [{"pid": 4242, "job_name": "migrate"}])
    monkeypatch.setattr(api_server.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(api_server.os, "getpgid", lambda pid: 1)
    monkeypatch.setattr(api_server.subprocess, "run", lambda *a, **k: sent.append(("pkill", a[0][-1])))
    monkeypatch.setattr(api_server.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(api_server.job_admission, "note_stop", lambda pid: None)
    monkeypatch.setattr(api_server.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    monkeypatch.setattr(run_watch, "open_incident", lambda **k: (1, True))
    monkeypatch.setattr(api_server.cpdb, "begin_action", lambda *a, **k: 1)
    monkeypatch.setattr(api_server.cpdb, "finish_action", lambda *a, **k: None)
    api_server._stop_account_jobs(5, "test")
    assert (4242, 2) in sent and (4242, 0) in sent and (4242, 9) in sent and sleeps == [30]
    assert "source-guard.kill" in run_watch.STOP_ACTIONS
