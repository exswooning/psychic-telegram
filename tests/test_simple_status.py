"""The /simple page and its JSON: local data only, cached 15 s, scoped per account, fast."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("httpx", reason="starlette TestClient needs httpx")
from fastapi.testclient import TestClient  # noqa: E402

import accounts_auth  # noqa: E402
import control_plane_db as cpdb  # noqa: E402
import simple_status  # noqa: E402
from db import MigrationDB  # noqa: E402


def _ledger(path, users=50, audit=100_000):
    MigrationDB(path)
    c = sqlite3.connect(path)
    now = datetime.now(timezone.utc)
    st = ["DONE"] * (users - 6) + ["RUNNING", "RUNNING", "FAILED", "FAILED", "PENDING", "INTERRUPTED"]
    c.executemany("INSERT INTO identity_map(source_email,target_email,status,notes) VALUES (?,?,?,?)",
                  [(f"u{i}@a.test", f"u{i}@b.test", s, "boom " * 100 if s == "FAILED" else None)
                   for i, s in enumerate(st)])
    rows = []
    for i in range(audit):
        age = timedelta(seconds=(i % 3000))                    # a third within the last ten minutes
        rows.append((f"u{i % users}@a.test", f"item{i}", ("file", "folder", "message")[i % 3], "SUCCESS",
                     (now - age).strftime("%Y-%m-%dT%H:%M:%SZ")))
    c.executemany("INSERT INTO audit_log(source_user,item_id,item_type,status,timestamp) VALUES (?,?,?,?,?)", rows)
    c.executemany("INSERT INTO user_verification(source_user,service,verified_at,verdict,payload) "
                  "VALUES (?, 'drive', 'now', ?, '{}')",
                  [(f"u{i}@a.test", "IDENTICAL" if i % 5 else "DIFFERENCES") for i in range(users)])
    c.commit()
    c.close()


@pytest.fixture
def env(monkeypatch, tmp_path):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    monkeypatch.setenv("BITPORT_OPERATOR_TOKEN", "")
    MigrationDB(path)
    cpdb.apply_migrations()
    simple_status._cache.clear()
    import api_server
    ledgers: dict[int, str] = {}
    monkeypatch.setattr("account_context.db_path", lambda aid: ledgers.get(aid))
    mk = lambda: TestClient(api_server.app)  # noqa: E731
    with mk() as admin, mk() as alice, mk() as bob:
        ids = {}
        for name, cl in (("admin", admin), ("alice", alice), ("bob", bob)):
            r = cl.post("/api/v2/auth/signup", json={"email": f"{name}@x.test", "password": "hunter22222", "name": name})
            assert r.status_code == 200, r.text
            ids[name] = cl.get("/api/v2/auth/me").json()["id"]
            ledgers[ids[name]] = str(tmp_path / f"{name}.db")
            accounts_auth.update_tenant_config(ids[name], "source", domain=f"s.{name}.test")
            accounts_auth.update_tenant_config(ids[name], "target", domain=f"t.{name}.test")
        accounts_auth.promote_to_superadmin("admin@x.test")
        yield admin, alice, bob, ids, ledgers
    try:
        os.unlink(path)
    except OSError:
        pass


def test_shape_and_values(env):
    admin, alice, _, ids, ledgers = env
    _ledger(ledgers[ids["alice"]], users=50, audit=3000)
    with cpdb.rw() as c:
        c.execute("INSERT INTO incidents(account_id, kind, fingerprint, title) VALUES (?,?,?,?)",
                  (ids["alice"], "crashed", "fp", "migrate crashed"))
        c.execute("INSERT INTO active_jobs(account_id, job_name, pid) VALUES (?,?,?)", (ids["alice"], "migrate", os.getpid()))
    d = alice.get("/api/v2/simple").json()
    assert len(d["accounts"]) == 1 and d["generatedAt"].endswith("Z")
    a = d["accounts"][0]
    assert (a["source"], a["target"]) == ("s.alice.test", "t.alice.test")
    assert a["users"] == {"DONE": 44, "RUNNING": 2, "FAILED": 2, "PENDING": 1, "INTERRUPTED": 1}
    assert a["running"] == ["u44@a.test", "u45@a.test"]
    assert [f["user"] for f in a["failed"]] == ["u46@a.test", "u47@a.test"]
    assert all(len(f["notes"]) <= 200 for f in a["failed"])
    assert a["jobs"][0]["name"] == "migrate" and a["jobs"][0]["pid"] == os.getpid() and a["jobs"][0]["elapsedSec"] < 60
    assert a["written"] == 2000                                  # file + folder of 3000 rows, not messages
    assert a["ratePerSec"] == pytest.approx(2000 / 3000 * 600 / 600, abs=0.01)  # rows within the last 600 s
    assert a["verified"] == 50 and a["verdicts"] == {"IDENTICAL": 40, "DIFFERENCES": 10}
    assert a["incidents"][0]["kind"] == "crashed" and a["incidents"][0]["open"] is True


def test_cached_for_15_seconds(env, monkeypatch):
    _, alice, _, ids, ledgers = env
    _ledger(ledgers[ids["alice"]], users=10, audit=10)
    n = []
    real = simple_status._ledger
    monkeypatch.setattr(simple_status, "_ledger", lambda p: n.append(1) or real(p))
    alice.get("/api/v2/simple")
    alice.get("/api/v2/simple")
    assert len(n) == 1
    t = time.monotonic()
    monkeypatch.setattr(simple_status.time, "monotonic", lambda: t + simple_status.TTL + 1)
    alice.get("/api/v2/simple")
    assert len(n) == 2


def test_scoping(env):
    admin, alice, bob, ids, ledgers = env
    seen = lambda cl: {a["id"] for a in cl.get("/api/v2/simple").json()["accounts"]}  # noqa: E731
    assert seen(alice) == {ids["alice"]}
    assert seen(bob) == {ids["bob"]}
    assert {ids["alice"], ids["bob"], ids["admin"]} <= seen(admin)
    assert TestClient(__import__("api_server").app).get("/api/v2/simple").status_code in (401, 403)


def test_no_google_client_is_built(env, monkeypatch):
    _, alice, _, ids, ledgers = env
    import auth

    def boom(*a, **k):
        raise AssertionError("a Google client was constructed")
    monkeypatch.setattr(auth.AuthManager, "__init__", boom)
    assert alice.get("/api/v2/simple").status_code == 200


def test_page_is_small_and_self_contained(env):
    _, alice, *_ = env
    assert TestClient(__import__("api_server").app).get("/simple").status_code in (401, 403)
    r = alice.get("/simple")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert len(r.content) < 15_000
    assert "http://" not in r.text and "https://" not in r.text and "<button" not in r.text


def test_answers_fast_on_a_big_ledger(env, monkeypatch):
    _, alice, _, ids, ledgers = env
    # Settings() probes RAM; on macOS that is 24 subprocesses (~80 ms per call, every cpdb call), on the Linux box a /proc read.
    monkeypatch.setattr("resources._probe_macos", lambda r: setattr(r, "ram_total_gb", 8.0) or setattr(r, "ram_usable_gb", 4.0))
    _ledger(ledgers[ids["alice"]], users=50, audit=100_000)
    t = time.perf_counter()
    r = alice.get("/api/v2/simple")
    took = time.perf_counter() - t
    print(f"simple endpoint: {took * 1000:.1f} ms")
    assert r.status_code == 200 and r.json()["accounts"][0]["written"] > 60_000
    assert took < 0.1
