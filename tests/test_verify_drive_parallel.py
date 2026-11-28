"""One user's Drive files are compared VERIFY_DRIVE_WORKERS at a time (account 3, 7 Nov 2026: 20-170 min a
user at ~1 s a call, one file at a time, on an idle box). The reads run side by side; the results come back in
the window's order; each file's budget and not-owner flag are its own; nothing is accounted on a worker."""
import threading
import time
from types import SimpleNamespace

import verify_sample as V


def verifier(db, settings):
    auth = SimpleNamespace(source_drive=lambda u: object(), target_drive=lambda u: object())
    return V.Verifier(auth, db, settings, "u@src", "u@tgt")


def test_reads_run_side_by_side_and_keep_order(db, settings, monkeypatch):
    monkeypatch.setenv("VERIFY_DRIVE_WORKERS", "6")
    seen = set()

    def slow(self, kind, sid, *rest):
        seen.add(threading.current_thread().name)
        time.sleep(0.3)
        return {"sid": sid}
    monkeypatch.setattr(V.Verifier, "_drive_reads", slow)
    jobs = [(f"s{i}", f"t{i}", {}, {}) for i in range(6)]
    start = time.monotonic()
    out = verifier(db, settings)._drive_reads_all("file", jobs, None, None, {}, {})
    assert [r["sid"] for r in out] == [j[0] for j in jobs]
    assert time.monotonic() - start < 1.2            # six 0.3 s reads, not 1.8 s one after another
    assert len(seen) > 1


def test_one_worker_reads_inline_on_the_callers_thread(db, settings, monkeypatch):
    monkeypatch.setenv("VERIFY_DRIVE_WORKERS", "1")
    names = []
    monkeypatch.setattr(V.Verifier, "_drive_reads",
                        lambda self, kind, sid, *r: names.append(threading.current_thread().name) or {"sid": sid})
    verifier(db, settings)._drive_reads_all("file", [("a", "b", {}, {}), ("c", "d", {}, {})], None, None, {}, {})
    assert names == [threading.current_thread().name] * 2


def test_a_files_budget_and_not_owner_flag_stay_with_that_file(db, settings, monkeypatch):
    v = verifier(db, settings)

    def content(self, *a):
        self._file_deadline = 123.0                    # what _doc_budget sets while it runs
        return []

    def sharing(self, src, tgt, sid, tid, res):
        self._not_owner = True
        res["not_owner"] = 1
        return []
    monkeypatch.setattr(V.Verifier, "_content", content)
    monkeypatch.setattr(V.Verifier, "_sharing", sharing)
    monkeypatch.setattr(V.Verifier, "_drive_path", lambda self, svc, fid, cache: "/")
    r = v._drive_reads("file", "s1", "t1", {"name": "a", "mimeType": "text/plain"}, {"name": "a"},
                       None, None, {}, {})
    assert r["notOwner"] is True and r["local"]["not_owner"] == 1
    assert v._file_deadline is None and not getattr(v, "_not_owner", False)


def test_the_merge_counts_a_clean_file_once(db, settings):
    v = verifier(db, settings)
    v._known_unverified = set()
    res = V.Verifier._blank()
    r = {"local": V.Verifier._blank(), "sp": "/", "tp": "/", "diffs": [], "ev": {"kind": "file", "name": "a"},
         "reused": None, "content": [], "links": [], "linkError": None, "carriedIn": False, "sharing": [],
         "notOwner": False}
    v._drive_merge("file", "s1", "t1", {"name": "a"}, {"name": "a"}, r, res, {})
    assert res["identical"] == 1 and res["differences"] == [] and len(v.evidence) == 1
