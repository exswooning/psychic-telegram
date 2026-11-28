"""An interrupted run picks itself back up, and a re-check does not redo work it can
prove is unchanged (2026-10-23: two memory pauses on dharan, each restarting every
user's check from zero)."""
import pytest

import verify_sample as V
from tests.test_verify_sample import SRC_USER, TGT_USER, migrated, verify  # noqa: F401 - fixture


def _versions(m, n):
    for d in (m.auth.source_drive(SRC_USER), m.auth.target_drive(TGT_USER)):
        for f in d.store.values():
            f["version"] = str(n)


def test_an_unchanged_pair_reuses_its_comparison_and_a_changed_one_does_not(migrated):  # noqa: F811
    _versions(migrated, 1)
    first = verify(migrated, ("drive",))
    assert first["verdict"] == "IDENTICAL", first["reasons"]
    assert not first["users"][SRC_USER]["drive"].get("contentReused")
    tgt = migrated.auth.target_drive(TGT_USER)
    downloads = tgt.call_count("files.get_media")
    again = verify(migrated, ("drive",))
    assert again["verdict"] == "IDENTICAL", (again["reasons"], [c for c in again["users"][SRC_USER]["drive"].get("checks", []) if c.get("required") and c["status"] not in ("MATCHED", "EMPTY")])
    assert again["users"][SRC_USER]["drive"]["contentReused"] >= 2
    assert tgt.call_count("files.get_media") == downloads           # nothing opened again
    f = next(x for x in tgt.store.values() if x.get("name") == "notes.txt")
    tgt.content[f["id"]] = b"edited on the target"
    f["version"] = "2"                                               # Drive bumps it on any change
    # ... and recomputes the checksum and size from the new bytes, which the fake does not do itself
    import hashlib
    f.update(md5Checksum=hashlib.md5(b"edited on the target").hexdigest(), size=str(len(b"edited on the target")))
    changed = verify(migrated, ("drive",))
    assert any("content differs" in d for x in changed["users"][SRC_USER]["drive"]["differences"]
               for d in x["diffs"])


@pytest.fixture
def resume_world(monkeypatch):
    import api_server
    import resources
    import run_watch
    started = []
    monkeypatch.setattr(api_server, "RESUME_POLL_SEC", 0)
    monkeypatch.setattr(resources, "cached_probe", lambda ttl=0: None)
    monkeypatch.setattr(resources, "pressure_severe", lambda snap: False)
    monkeypatch.setattr(api_server.job_admission, "try_admit", lambda aid, name: (True, ""))
    monkeypatch.setattr(api_server.job_admission, "release", lambda aid, name: None)
    monkeypatch.setattr(api_server, "_start_admitted", lambda argv, aid, name, env, then: started.append(env) or (True, "started"))
    monkeypatch.setattr(api_server.cpdb, "begin_action", lambda *a, **k: 1)
    monkeypatch.setattr(api_server.cpdb, "finish_action", lambda *a, **k: None)
    monkeypatch.setattr(run_watch, "stopped_by", lambda pid, since: None)
    return api_server, run_watch, started


def test_a_memory_pause_resumes_smaller(resume_world):
    api_server, _, started = resume_world
    api_server._resume_after_memory_pause(["py", "main.py"], 5, "migrate", {"USER_WORKERS": "4"}, ["repair"], 9, "t")
    assert len(started) == 1
    env = started[0]
    assert (env["MIGRATE_PROCESSES"], env["USER_WORKERS"], env["BITPORT_RESUME_ATTEMPT"]) == ("1", "2", "1")


def test_never_after_a_person_s_stop_and_never_past_three(resume_world, monkeypatch):
    api_server, run_watch, started = resume_world
    monkeypatch.setattr(run_watch, "stopped_by", lambda pid, since: "operator")
    api_server._resume_after_memory_pause(["py"], 5, "migrate", {}, None, 9, "t")
    monkeypatch.setattr(run_watch, "stopped_by", lambda pid, since: None)
    api_server._resume_after_memory_pause(["py"], 5, "migrate", {"BITPORT_RESUME_ATTEMPT": "3"}, None, 9, "t")
    assert started == []
