"""A queued job's stored payload held the DMS Admin console passwords in plain text (8 Nov 2026)."""
import json

import pytest

import control_plane_db as cpdb
import job_queue


@pytest.fixture
def cp_db(tmp_path, monkeypatch):
    path = str(tmp_path / "cp.db")
    cpdb.apply_migrations(path)
    monkeypatch.setenv("CONTROL_PLANE_DB", path)
    monkeypatch.setattr(cpdb, "_db_path", lambda: path)
    return path


def _stored(qid):
    with cpdb.ro() as c:
        return json.loads(c.execute("SELECT payload FROM job_queue WHERE id=?", (qid,)).fetchone()["payload"])


def test_enqueue_never_stores_a_secret_but_keeps_the_rest(cp_db):
    row = job_queue.enqueue(None, "dms", {"argv": ["x"], "env": {
        "DWD_PASSWORD": "p1", "DWD_PASSWORD_SOURCE": "p2", "SOME_TOKEN": "t", "MIGRATION_DB": "/d/3/m.db"}})
    assert _stored(row["id"])["env"] == {"MIGRATION_DB": "/d/3/m.db"}


def test_scrub_removes_secrets_already_stored(cp_db):
    row = job_queue.enqueue(None, "dms2", {"argv": ["x"]})
    with cpdb.rw() as c:
        c.execute("UPDATE job_queue SET payload=? WHERE id=?",
                  (json.dumps({"argv": ["x"], "env": {"DWD_PASSWORD": "leak", "KEEP": "1"}}), row["id"]))
    assert job_queue.scrub_secrets() == 1
    assert _stored(row["id"])["env"] == {"KEEP": "1"}
    assert job_queue.scrub_secrets() == 0


def test_a_queued_dms_job_gets_its_logins_back_from_the_box_at_dispatch(monkeypatch):
    import webui
    seen = {}

    class J:
        def start(self, label, argv, env=None, **k):
            seen["env"] = env
            return True, ""

    monkeypatch.setattr(webui, "get_job", lambda _a: J())
    monkeypatch.setattr(webui, "_dms_env", lambda _a: {"DWD_PASSWORD": "from-dwd-env", "MIGRATION_DB": "/d/3/m.db"})
    ok, _ = webui._queue_starter(3, "dms", {"argv": ["python", "dms_migrate.py", "--apply"], "env": {"MIGRATION_DB": "/d/3/m.db"}})
    assert ok and seen["env"]["DWD_PASSWORD"] == "from-dwd-env"
    webui._queue_starter(3, "other", {"argv": ["python", "main.py"], "env": {}})
    assert seen["env"].get("DWD_PASSWORD") != "from-dwd-env"
