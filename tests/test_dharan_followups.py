"""Four things the dharan 1:1 work found on 2026-10-23, each pinned."""
import base64
import json
import sqlite3

import known_issues
from gmail_engine import GmailMigrator


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode()


class TestMessageIdIsReadFromTheWholeHeaderBlock:
    """Live: Message-IDs at bytes 8,818 and 11,426 read as missing (an 8 KB window)."""

    def test_a_message_id_past_eight_kilobytes_is_found(self):
        hdr = "".join(f"Received: from relay{i}.example by mx.example; Mon, 1 Jan 2024 +0000\r\n" for i in range(160))
        assert len(hdr) > 8192
        assert GmailMigrator._message_id_header(_b64((hdr + "Message-ID: <deep@x>\r\n\r\nbody").encode())) == "<deep@x>"

    def test_one_in_the_body_is_not_the_header(self):
        assert GmailMigrator._message_id_header(_b64(b"Subject: s\r\n\r\nMessage-ID: <in-body@x>\r\n")) is None

    def test_an_ordinary_message_still_reads(self):
        assert GmailMigrator._message_id_header(_b64(b"Message-ID: <a@b>\nSubject: s\n\nhi")) == "<a@b>"


def _queue(conn, with_issue_column):
    extra = ", issue TEXT" if with_issue_column else ""
    conn.execute("CREATE TABLE verification_repair_queue (source_user TEXT, service TEXT, finding_key TEXT, "
                 f"category TEXT, item_id TEXT, state TEXT, detail TEXT, first_seen TEXT, last_seen TEXT, resolved_at TEXT{extra})")
    rows = [("info@a", "gmail", "k1", "differences", "s1", "QUEUED",
             json.dumps({"diffs": ["labels added on the target: ['INBOX']"]})),
            ("ward7@a", "gmail", "k2", "differences", "s2", "QUEUED",
             json.dumps({"diffs": ["labels added on the target: ['INBOX']"]})),
            ("info@a", "drive", "k3", "errors", None, "QUEUED",
             json.dumps("file 1: HTTP 403 ACCESS_TOKEN_SCOPE_INSUFFICIENT"))]
    for r in rows:
        conn.execute("INSERT INTO verification_repair_queue (source_user,service,finding_key,category,item_id,state,detail,"
                     "first_seen,last_seen) VALUES (?,?,?,?,?,?,?,'t','t')", r)


def test_a_queue_from_before_kinds_is_still_counted():
    """Live: the card read '0 findings fixed, 0 open' -- the column is only added on the
    next write, and the read-only summary counted nothing without it."""
    conn = sqlite3.connect(":memory:")
    _queue(conn, with_issue_column=False)
    kinds = {k["id"]: k for k in known_issues.summary(conn)["kinds"]}
    assert kinds["gmail.labels_added"]["open"] == 2
    assert kinds["drive.sheets_scope"]["openByCategory"] == {"errors": 1}      # couldn't-check, its own bar
    assert [u["user"] for u in known_issues.users_for(conn, "gmail.labels_added")] == ["info@a", "ward7@a"]


def test_the_dependency_preflight_reports_and_never_refuses(monkeypatch, auth, db, settings, identity):
    import migration_acceptance
    import user_profiles
    monkeypatch.setattr(user_profiles, "sync_profiles", lambda *a, **k: {"profiles": [
        {"status": "ERROR", "error": "Profile read-back still differs: name, languages"}]})
    settings.migrate_groups = False
    from tests.conftest import SRC_USER, TGT_USER
    got = migration_acceptance.dependency_preflight(auth, db, settings, SRC_USER, TGT_USER)
    assert got == ["Profile not reconciled: Profile read-back still differs: name, languages"]


def test_a_bot_message_reposted_with_attribution_is_named_not_lumped(auth, db, settings, identity):
    """Live: all 22 chat differences were Drive-bot messages the user re-posted as
    '[originally from users/...] <text>'. Still a difference -- just a named one."""
    import verify_sample as V
    from tests.conftest import SRC_USER, TGT_USER
    src, tgt = auth.source_chat(SRC_USER), auth.target_chat(TGT_USER)
    assert known_issues.classify("chat", {"diffs": ["posted by the user with an attribution: x"]}) == "chat.attributed"
    assert known_issues.classify("chat", {"diffs": ["Chat text differs"]}) == "chat.text"
    assert V.ALL_SERVICES  # module loads


def test_a_repair_reports_each_step_in_order(monkeypatch, auth, db, settings):
    import repair
    seen = []
    monkeypatch.setattr(repair, "fix_modified_times", lambda *a, **k: {})
    monkeypatch.setattr(repair, "reapply_owed_grants", lambda *a, **k: {})
    monkeypatch.setattr(repair, "survey", lambda d: {"total": 0})
    repair.run_all(db, auth, settings, apply=False, progress=lambda i, n, name: seen.append((i, n, name)))
    assert seen == [(1, 10, "file times"), (2, 10, "owed shares"), (3, 10, "failure survey")]  # nothing failed: stops


def test_a_running_repair_is_listed_with_its_step_and_no_pid(monkeypatch):
    import threading
    import api_server
    from fastapi.testclient import TestClient
    stop = threading.Event()
    t = threading.Thread(target=stop.wait, daemon=True)
    t.start()
    monkeypatch.setitem(api_server._REPAIR_THREADS, 5, t)
    monkeypatch.setitem(api_server._REPAIR_PROGRESS, 5, {"index": 7, "total": 10, "step": "Drive stragglers (re-walk)",
                                                          "startedAt": "2026-10-23T11:12:03Z"})
    monkeypatch.setattr(api_server.job_admission, "list_active", lambda: [])
    monkeypatch.setattr(api_server, "require_reader", lambda op: None)
    try:
        rows = TestClient(api_server.app).get("/api/v2/active-jobs").json()
    finally:
        stop.set()
    assert rows == [{"account_id": 5, "job_name": "repair", "pid": None, "started_at": "2026-10-23T11:12:03Z",
                     "progress": {"index": 7, "total": 10, "step": "Drive stragglers (re-walk)",
                                  "startedAt": "2026-10-23T11:12:03Z"}}]


def test_a_repair_re_walk_leaves_verification_to_the_one_to_one_run(db, settings):
    import repair
    from db import bulk_seed_identities
    bulk_seed_identities(db, [("u@src", "u@tgt")])
    settings.first_pass_acceptance = settings.verify_on_complete = True
    seen = []
    repair.retry_drive_stragglers(None, db, settings, apply=True, users=["u@src"],
                                  _migrate_user=lambda a, d, st, s, t, *x, **k: seen.append(st) or {"status": "DONE"})
    st = seen[0]
    assert (st.first_pass_acceptance, st.verify_on_complete, st.tally_on_complete) == (False, False, False)
    assert settings.first_pass_acceptance is True               # the run's own settings untouched
