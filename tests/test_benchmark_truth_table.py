"""P2.8 (a): the benchmarks' truth table, offline.

Every check in benchmarks.py has one row: a clean control reads PASS, its own fault reads what
the table says, and a check whose evidence is missing reads UNKNOWN -- and a required UNKNOWN
makes the report UNVERIFIED, never PASS. A check with no row fails test_every_check_has_a_row.
The facts come from a real temp ledger through run_report.collect_facts (and from a transcript
through collect_seed_facts for a seed), so what is proven is the whole path, not only evaluate().
The same table, with the live fault for each row, is in docs/benchmark-truth-table.md.
"""
from __future__ import annotations

import pytest

import benchmarks as B
import run_report as RR

START, END = "2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z"
GOOD_METRICS = {"calls": 1000, "retries": 5, "p95": 1.0, "limiters": {"drive": {"backoffs": 0}}}
GOOD_FID = {"countParity": 1.0, "checksumFailures": 0, "aclFidelity": 1.0, "extraGrants": 0,
            "timestampsPreserved": 1.0}


def _without(d, *keys):
    return {k: v for k, v in d.items() if k not in keys}


def _build(db, settings, monkeypatch, *, rc=0, ok_items=600, failed=0, blocked=0, users=3, users_failed=0,
           metrics=GOOD_METRICS, fid=GOOD_FID, timed=True, end=END, break_ledger=False):
    settings.user_workers = 4
    for i in range(users):
        db.conn.execute("INSERT INTO identity_map(source_email,target_email,entity_type,status) VALUES(?,?,?,?)",
                        (f"u{i}@a.com", f"u{i}@b.com", "user", "FAILED" if i < users_failed else "DONE"))
    rows = [("SUCCESS", None)] * ok_items + [("FAILED", "boom")] * failed + [("BLOCKED", "licence")] * blocked
    db.conn.executemany("INSERT INTO audit_log(source_user,item_id,item_type,status,error_message,bytes_moved)"
                        " VALUES('u0@a.com',?,'file',?,?,1000)", [(f"i{n}", s, m) for n, (s, m) in enumerate(rows)])
    db.conn.commit()
    if metrics is not None:
        db.record_metrics(metrics)
    if fid is not None:
        db.record_fidelity(fid)
    if break_ledger:
        def _broken(conn):
            raise RuntimeError("ledger unreadable")
        monkeypatch.setattr(RR, "_ledger", _broken)
    run = {"returnCode": rc, "startedAt": START, "finishedAt": end} if timed else {"returnCode": rc}
    return RR.build_report(RR.collect_facts(db, settings, run=run), account_id=3)


def _by(report):
    return {r["id"]: r["status"] for r in report["benchmarks"]["results"]}


def _required(kind):
    return {b.id for b in B.for_kind(kind) if b.required}


# check id: (fault kwargs, status the fault reads, missing-evidence kwargs)
MIGRATION = {
    "run_completed": (dict(rc=-6), "fail", dict(rc=None)),
    "migrated_something": (dict(ok_items=0), "fail", dict(break_ledger=True)),
    "item_failure_rate": (dict(failed=100), "fail", dict(break_ledger=True)),
    "users_failed": (dict(users_failed=1), "fail", dict(users=0)),
    "blocked_items": (dict(blocked=5), "warn", dict(break_ledger=True)),
    "retry_rate": (dict(metrics={**GOOD_METRICS, "retries": 200}), "fail",
                   dict(metrics=_without(GOOD_METRICS, "retries"))),
    "quota_pushbacks": (dict(metrics={**GOOD_METRICS, "limiters": {"drive": {"backoffs": 7}}}), "warn",
                        dict(metrics=_without(GOOD_METRICS, "limiters"))),
    "p95_latency": (dict(metrics={**GOOD_METRICS, "p95": 12.0}), "fail",
                    dict(metrics=_without(GOOD_METRICS, "p95"))),
    "items_per_min_per_worker": (dict(end="2026-01-01T10:00:00Z"), "fail", dict(timed=False)),
    "count_parity": (dict(fid={**GOOD_FID, "countParity": 0.95}), "fail",
                     dict(fid=_without(GOOD_FID, "countParity"))),
    "checksum_failures": (dict(fid={**GOOD_FID, "checksumFailures": 1}), "fail",
                          dict(fid=_without(GOOD_FID, "checksumFailures"))),
    "acl_fidelity": (dict(fid={**GOOD_FID, "aclFidelity": 0.9}), "fail",
                     dict(fid=_without(GOOD_FID, "aclFidelity"))),
    "extra_grants": (dict(fid={**GOOD_FID, "extraGrants": 1}), "fail",
                     dict(fid=_without(GOOD_FID, "extraGrants"))),
    "timestamps_preserved": (dict(fid={**GOOD_FID, "timestampsPreserved": 0.5}), "fail",
                             dict(fid=_without(GOOD_FID, "timestampsPreserved"))),
}


class TestMigration:
    def test_every_check_has_a_row(self):
        assert set(MIGRATION) == {b.id for b in B.DEFAULTS}

    def test_the_control_passes_every_check(self, db, settings, monkeypatch):
        r = _build(db, settings, monkeypatch)
        assert set(_by(r).values()) == {"pass"} and r["verdict"] == "PASS", _by(r)

    @pytest.mark.parametrize("cid", list(MIGRATION))
    def test_its_fault_reads_what_the_table_says(self, cid, db, settings, monkeypatch):
        kw, want, _ = MIGRATION[cid]
        r = _build(db, settings, monkeypatch, **kw)
        assert _by(r)[cid] == want, _by(r)
        assert r["verdict"] == ("FAIL" if want == "fail" else "PASS")     # a warn never moves the verdict

    @pytest.mark.parametrize("cid", list(MIGRATION))
    def test_missing_evidence_is_unknown_and_never_a_pass(self, cid, db, settings, monkeypatch):
        r = _build(db, settings, monkeypatch, **MIGRATION[cid][2])
        assert _by(r)[cid] == "unknown", _by(r)
        if cid in _required("migration"):
            assert r["verdict"] in ("UNVERIFIED", "FAIL") and r["verdict"] != "PASS"
            assert cid in r["benchmarks"]["unverified"] or r["verdict"] == "FAIL"
        else:
            assert cid not in r["benchmarks"]["unverified"]          # optional: says so, does not block

    def test_no_tally_at_all_leaves_every_required_fidelity_check_unverified(self, db, settings, monkeypatch):
        r = _build(db, settings, monkeypatch, fid=None)
        assert r["verdict"] == "UNVERIFIED"
        assert set(r["benchmarks"]["unverified"]) == {"count_parity", "checksum_failures", "acl_fidelity",
                                                      "extra_grants"}

    def test_a_tally_from_before_the_run_is_unknown_not_a_pass(self, db, settings, monkeypatch):
        _build(db, settings, monkeypatch, fid=None)
        db.record_fidelity(GOOD_FID)
        db.conn.execute("UPDATE run_fidelity SET recorded_at='2025-12-01T00:00:00Z'")
        db.conn.commit()
        run = {"returnCode": 0, "startedAt": START, "finishedAt": END}
        r = RR.build_report(RR.collect_facts(db, settings, run=run), account_id=3)
        assert _by(r)["count_parity"] == "unknown" and r["verdict"] == "UNVERIFIED"


# --------------------------------------------------------------------------- seed
HEAD = ["Seeding 4 users in x.com at scale 'small'", "Workers: 2 (2 users x 14 threads)"]
BEAT = "  ... still topping up: 4/4 users done after 5m00s (0 in flight) -- {up} GB uploaded of 383 GB planned"
TOP = "  [{u}@x.com] top-up in 9s: 1.0GB -> 30.0GB (12 filler file(s))"
TOP_FAILED = "  [{u}@x.com] top-up in 9s: 0.3GB -> 0.0GB (5 filler file(s)) -- storage top-up failed: HTTP 403 (storageQuotaExceeded)"
DONE = "  [{u}@x.com] done in 10.0s: 12 files, 3 events"
DONE_CHAT = DONE + " (chat failed (HTTP 404))"
ENDED = {"returnCode": 0, "finishedAt": "2026-01-01T01:00:00Z"}


def seed_lines(*, done=(0, 1, 2, 3), failed_chat=(), topped=(0, 1), top_failed=(), up=383, warnings=0, beat=True):
    out = HEAD + [f"  [u{i}@x.com] starting (Eng)" for i in range(4)]
    out += [(DONE_CHAT if i in failed_chat else DONE).format(u=f"u{i}") for i in done]
    out += [(TOP_FAILED if i in top_failed else TOP).format(u=f"u{i}") for i in topped]
    out += ["  ! chat for u0@x.com: HTTP 404 (NOT_FOUND): x"] * warnings
    return out + ([BEAT.format(up=up)] if beat else [])


def _seed(settings, lines, run=ENDED):
    return RR.build_report(RR.collect_seed_facts(settings, run=run, transcript=lines), account_id=3)


# check id: (fault kwargs, status the fault reads, missing-evidence kwargs); kwargs are lines= and run=
NO_USERS = dict(lines=seed_lines(done=(), topped=()))
SEED = {
    "run_completed": (dict(run={"returnCode": -9, "finishedAt": "x"}), "fail", dict(run={})),
    "users_finished": (dict(lines=seed_lines(done=(0, 1), topped=())), "fail", dict(lines=[])),
    "never_finished": (dict(lines=seed_lines(done=(0, 1), topped=())), "fail", dict(run={})),
    "service_failures": (dict(lines=seed_lines(failed_chat=(2, 3))), "fail", NO_USERS),
    "fill_users_failed": (dict(lines=seed_lines(top_failed=(0,))), "fail", dict(lines=seed_lines(topped=()))),
    "warnings_per_user": (dict(lines=seed_lines(warnings=6)), "warn", NO_USERS),
    "fill_reached": (dict(lines=seed_lines(up=100)), "fail", dict(lines=seed_lines(beat=False))),
}


class TestSeed:
    def test_every_check_has_a_row(self):
        assert set(SEED) == {b.id for b in B.SEED_DEFAULTS}

    def test_the_control_passes_every_check(self, settings):
        r = _seed(settings, seed_lines())
        assert set(_by(r).values()) == {"pass"} and r["verdict"] == "PASS", _by(r)

    @pytest.mark.parametrize("cid", list(SEED))
    def test_its_fault_reads_what_the_table_says(self, cid, settings):
        kw, want, _ = SEED[cid]
        r = _seed(settings, kw.get("lines", seed_lines()), kw.get("run", ENDED))
        assert _by(r)[cid] == want, _by(r)
        assert r["verdict"] == ("FAIL" if want == "fail" else "PASS")

    @pytest.mark.parametrize("cid", list(SEED))
    def test_missing_evidence_is_unknown_and_never_a_pass(self, cid, settings):
        kw = SEED[cid][2]
        r = _seed(settings, kw.get("lines", seed_lines()), kw.get("run", ENDED))
        assert _by(r)[cid] == "unknown", _by(r)
        if cid in _required("seed"):
            assert r["verdict"] != "PASS"
