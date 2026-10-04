"""A migration's end of life (lifecycle.py): approved -- by the operator, or on its own
after a quiet spell -- then torn down later, never touching what another account uses.
The operator's policy, 2026-10-04."""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

import admin_secrets
import control_plane_db as cpdb
import gcloud_signout
import lifecycle
from db import MigrationDB

NOW = datetime(2026, 12, 1, tzinfo=timezone.utc)


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = str(tmp_path / "cp.db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    monkeypatch.setattr(lifecycle, "HERE", str(tmp_path))
    monkeypatch.setattr(admin_secrets, "LOGIN_DIR", str(tmp_path / "logins"))
    monkeypatch.setattr(gcloud_signout, "busy", lambda: "")
    monkeypatch.setattr(gcloud_signout, "sign_out_all", lambda: ["stale@x"])
    monkeypatch.setattr(gcloud_signout, "throwaways", lambda: [])

    def key(account, side, project, client, name=None):
        d = tmp_path / "keys" / str(account)
        d.mkdir(parents=True, exist_ok=True)
        p = d / (name or f"{side}-sa.json")
        p.write_text(json.dumps({"project_id": project, "client_id": client}))
        return str(p)

    def config(account, side, domain, key_path):
        with cpdb.rw() as c:
            c.execute("INSERT INTO tenant_configs(account_id, side, domain, sa_key_path) "
                      "VALUES (?,?,?,?)", (account, side, domain, key_path))

    def ran(account, at):
        with cpdb.rw() as c:
            c.execute("INSERT INTO run_events(at, account_id, job_name, event) "
                      "VALUES (?, ?, 'migrate', 'finished')", (at, account))
    return key, config, ran


def _sides():
    calls = []

    def run_side(project, client, email, password):
        calls.append((project, client, email, password))
        return {"ok": True, "detail": f"deleted {project}, revoked {client}"}
    return calls, run_side


def test_auto_approval_waits_n_days_from_the_last_run_and_from_first_sight(world):
    key, config, ran = world
    config(5, "source", "client.example", key(5, "source", "p-src", "111"))
    ran(5, "2026-10-01T00:00:00.000Z")
    calls, run_side = _sides()
    # First sight is the clock's floor: an old run does not approve the day this ships.
    lifecycle.sweep(lambda a: False, run_side, now=NOW)
    assert not lifecycle.state(5).get("approved_at")
    did = lifecycle.sweep(lambda a: False, run_side, now=NOW + timedelta(days=lifecycle.AUTO_APPROVE_DAYS))
    assert lifecycle.state(5)["approved_by"] == "auto" and "approved automatically" in did[0]


def test_a_never_migrated_account_is_never_called_complete(world):
    key, config, ran = world
    config(6, "source", "fresh.example", key(6, "source", "p6", "666"))
    lifecycle.sweep(lambda a: False, _sides()[1], now=NOW)
    lifecycle.sweep(lambda a: False, _sides()[1], now=NOW + timedelta(days=400))
    assert not lifecycle.state(6).get("approved_at")


def test_teardown_signs_in_with_the_kept_login_and_leaves_what_others_use(world):
    key, config, ran = world
    shared = key(7, "source", "p-shared", "222")
    config(7, "source", "src.example", shared)
    config(7, "target", "tgt.example", key(7, "target", "p-tgt", "333"))
    config(8, "source", "src.example", key(8, "source", "p-shared", "222"))   # another account, same key
    admin_secrets.save_teardown_login(7, "target", "admin@tgt.example", "pw with = and space ")
    lifecycle.approve(7, "boss", now=NOW)
    calls, run_side = _sides()
    did = lifecycle.sweep(lambda a: False, run_side, now=NOW + timedelta(days=lifecycle.TEARDOWN_DAYS))
    assert calls == [("p-tgt", "333", "admin@tgt.example", "pw with = and space ")]
    s = lifecycle.state(7)
    assert s["torn_down_at"] and "torn down" in did[0]
    r = json.loads(s["last_result"])
    assert r["sides"]["source"].startswith("left: project p-shared")
    assert not os.path.exists(shared)                           # account 7's own copy goes
    assert os.path.exists(key(8, "source", "p-shared", "222"))  # account 8's stays
    assert admin_secrets.teardown_login(7, "target") == ("", "")
    assert r["signedOut"] == ["stale@x"]


def test_no_kept_login_is_incomplete_and_retried_a_day_later_not_every_hour(world):
    key, config, ran = world
    config(9, "target", "t9.example", key(9, "target", "p9", "999"))
    lifecycle.approve(9, "boss", now=NOW)
    due = NOW + timedelta(days=lifecycle.TEARDOWN_DAYS)
    calls, run_side = _sides()
    lifecycle.sweep(lambda a: False, run_side, now=due)
    assert not lifecycle.state(9)["torn_down_at"]
    assert "no admin login kept" in json.loads(lifecycle.state(9)["last_result"])["sides"]["target"]
    assert lifecycle.sweep(lambda a: False, run_side, now=due + timedelta(hours=2)) == []


def test_nothing_happens_while_a_job_of_the_account_runs(world):
    key, config, ran = world
    config(10, "target", "t10.example", key(10, "target", "p10", "1010"))
    lifecycle.approve(10, "boss", now=NOW)
    calls, run_side = _sides()
    lifecycle.sweep(lambda a: a == 10, run_side, now=NOW + timedelta(days=lifecycle.TEARDOWN_DAYS))
    assert calls == [] and not lifecycle.state(10).get("last_attempt_at")


def test_undo_takes_an_approval_back(world):
    lifecycle.approve(11, "boss", now=NOW)
    assert lifecycle.undo(11) and not lifecycle.state(11)["teardown_due_at"]


def test_orphaned_sign_ins_older_than_a_day_are_cleaned(world, monkeypatch):
    old, fresh = tempfile.mkdtemp(prefix="cloudsdk-"), tempfile.mkdtemp(prefix="cloudsdk-")
    os.utime(old, (0, NOW.timestamp() - 25 * 3600))
    os.utime(fresh, (0, NOW.timestamp() - 3600))
    monkeypatch.setattr(gcloud_signout, "throwaways", lambda: [old, fresh])
    monkeypatch.setattr(gcloud_signout, "_accounts", lambda c: ["dead-setup@x"] if c == old else [])
    cleaned = []
    monkeypatch.setattr(lifecycle.gcloud_browser_auth, "cleanup", lambda c: cleaned.append(c))
    assert lifecycle.clean_orphans(NOW.timestamp()) == ["dead-setup@x"] and cleaned == [old]
    os.rmdir(old)
    os.rmdir(fresh)


def test_a_kept_login_file_is_private(world):
    admin_secrets.save_teardown_login(12, "source", "a@x", "pw")
    mode = os.stat(admin_secrets._login_path(12, "source")).st_mode & 0o777
    assert mode == 0o600
