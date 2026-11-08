"""The error sniffer: users chosen by evidence, every prediction named, nothing written."""
import sniff


def _discovery(db, rows):
    for u, files, byt, msgs, native, depth in rows:
        db.conn.execute("INSERT INTO discovery(source_user, file_count, total_bytes, messages_total, native_count, max_depth) "
                        "VALUES (?,?,?,?,?,?)", (u, files, byt, msgs, native, depth))
    db.conn.commit()


def test_users_are_chosen_by_evidence_and_the_smallest_is_included(db):
    _discovery(db, [("big@s", 900, 5e9, 10, 5, 3), ("mail@s", 5, 1e6, 40000, 0, 1), ("tiny@s", 2, 10, 3, 0, 1)])
    chosen = sniff.pick_users(db, 8)
    assert "most files" in chosen["big@s"] and "most email" in chosen["mail@s"] and "smallest" in chosen["tiny@s"]
    assert list(sniff.pick_users(db, 1)) == [next(iter(chosen))]


def test_a_service_that_fails_its_read_is_a_finding_and_the_rest_still_run(monkeypatch):
    monkeypatch.setattr(sniff, "probe_access", lambda a, u: {"drive": "ok", "gmail": "403 no scope", "calendar": "ok",
                                                             "contacts": "ok", "tasks": "ok"})
    monkeypatch.setattr(sniff, "sniff_drive", lambda s, u: {"sampled": 3, "drive.needs_copy": 2, "drive.cannot_download": 0})
    monkeypatch.setattr(sniff, "sniff_calendar", lambda a, u: {"sampled": 1, "calendar.typed_event": 0})
    monkeypatch.setattr(sniff, "sniff_contacts", lambda a, u: {"sampled": 1, "contacts.photo": 4})
    monkeypatch.setattr(sniff, "sniff_tasks", lambda a, u: (_ for _ in ()).throw(RuntimeError("boom")))
    r = sniff.sniff_user(None, None, "u@s", 10)
    assert r["findings"] == {"access.failed": 2, "drive.needs_copy": 2, "contacts.photo": 4}
    assert r["access"]["tasks"].startswith("probe failed")


def test_the_report_says_what_to_fix_before_the_run_and_what_is_already_handled():
    text = sniff.render([{"user": "u@s", "why": ["named"], "access": {"gmail": "ok"},
                          "findings": {"drive.needs_copy": 2, "gmail.too_large": 1, "made.up": 1}}])
    assert "ok  drive.needs_copy x2" in text and "FIX gmail.too_large x1" in text
    assert "no entry: add one to WHAT_HAPPENS" in text
    assert "TO FIX BEFORE THE RUN: gmail.too_large, made.up" in text


def test_it_reads_only(monkeypatch):
    """No write verb anywhere in the module."""
    import re
    src = open(sniff.__file__).read()
    assert not re.search(r"\.(insert|import_|create|update|patch|delete|trash|modify|batchDelete|copy)\(", src)


def test_a_throttle_is_retried_and_never_reported_as_a_failed_grant(monkeypatch):
    monkeypatch.setattr(sniff.time, "sleep", lambda s: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError('<HttpError 429 returned "Too many concurrent requests for user">')
    assert sniff._first(flaky) == "ok" and len(calls) == 3
    always = lambda: (_ for _ in ()).throw(RuntimeError('<HttpError 429 returned "Too many concurrent requests">'))
    assert sniff._first(always).startswith("throttled")
    assert sniff._first(lambda: (_ for _ in ()).throw(RuntimeError("403 delegation denied"))) == "403 delegation denied"
    monkeypatch.setattr(sniff, "probe_access", lambda a, u: {"drive": "throttled: x", "gmail": "ok", "calendar": "ok",
                                                             "contacts": "ok", "tasks": "ok"})
    monkeypatch.setattr(sniff, "sniff_gmail", lambda a, u, n: {"sampled": 1})
    for name in ("sniff_calendar", "sniff_contacts", "sniff_tasks"):
        monkeypatch.setattr(sniff, name, lambda a, u: {"sampled": 1})
    assert sniff.sniff_user(None, None, "u@s", 1)["findings"] == {}
