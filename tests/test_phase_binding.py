"""Phase rates need phase-local evidence; missing telemetry cannot pass as NOTHING."""
import json
import time
from datetime import datetime, timezone

import pytest

import phase_binding as P
from db import MigrationDB


def _stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ledger(tmp_path, case, *, rejection_count=0, cpu=20.0, tx=100.0, idle=0.5,
            rejection_baseline=True):
    path = tmp_path / f"{case}.db"
    db = MigrationDB(str(path))
    now = time.time()
    start, end = now - 30, now + 30
    with db.write() as conn:
        conn.execute("INSERT INTO run_records(started_at,pid) VALUES(?,?)", (_stamp(start), 123))
        conn.execute("INSERT INTO rate_limiter_ceiling(tenant,ceiling,updated_at) VALUES('target',1000,?)",
                     (_stamp(start),))
        for i in range(10):
            conn.execute("INSERT INTO id_mapping(source_user,source_id,target_id,type,created_at) "
                         "VALUES('u@src','f'||?,'t'||?,'file',?)", (i, i, _stamp(now)))
        rows = [(10, 0), (20, rejection_count)]
        if rejection_baseline:
            rows.insert(0, (-10, 0))
        for offset, cumulative in rows:
            payload = {
                "pid": 123,
                "limiters": {"target": {"rate": 80.0, "ceiling": 1000.0,
                                          "rejections": cumulative, "backoffs": cumulative}},
                "limiter_events": {"target": []},
                "worker_slots_configured": 10,
                "worker_slots_busy": round((1 - idle) * 10),
                "idle_workers_share": idle,
            }
            conn.execute("INSERT INTO run_metrics(recorded_at,payload) VALUES(?,?)",
                         (_stamp(start + offset), json.dumps(payload)))
    db.close()

    sample_path = tmp_path / f"{case}.jsonl"
    rows = []
    for offset in (10, 20):
        rows.append({"timestamp": _stamp(start + offset), "interface": "wlp2s0",
                     "cpu": {"busy_pct": cpu},
                     "network": {"tx_bytes_per_sec": tx, "rx_bytes_per_sec": 50.0,
                                 "bytes_per_sec": tx + 50.0}})
    sample_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(path), str(sample_path), start, end


@pytest.mark.parametrize(("case", "expected", "rejections", "cpu", "tx", "idle"), [
    ("quota", P.GOOGLE_QUOTA, 2, 20.0, 100.0, 0.5),
    ("line", P.LINE, 0, 20.0, 950.0, 0.5),
    ("cpu", P.CPU, 0, 94.0, 100.0, 0.05),
    ("nothing", P.NOTHING, 0, 20.0, 100.0, 0.5),
])
def test_each_binding_verdict_uses_phase_local_evidence(tmp_path, case, expected,
                                                        rejections, cpu, tx, idle):
    ledger, sampler, start, end = _ledger(
        tmp_path, case, rejection_count=rejections, cpu=cpu, tx=tx, idle=idle)
    report = P.analyze(7, ledger=ledger, sample_path=sampler,
                       phase_windows=[f"drive={_stamp(start)},{_stamp(end)}"],
                       line_bytes_per_sec=1000)
    drive = report["phases"]["drive"]
    assert drive["verdict"] == expected
    assert drive["items"] == 10
    assert drive["itemsPerMinute"] == pytest.approx(10.0)
    assert drive["limiter"]["target"]["ratePerSec"] == 80.0
    assert drive["limiter"]["target"]["ceilingPerSec"] == 1000.0
    assert drive["rejections"] == rejections
    assert drive["cpuSharePct"] == cpu
    assert drive["bytesPerSecond"] == tx + 50.0
    assert drive["txBytesPerSecond"] == tx
    assert drive["idleWorkersShare"] == idle


def test_insufficient_samples_are_unknown_not_a_bitport_defect(tmp_path):
    ledger, _sampler, start, end = _ledger(tmp_path, "unknown")
    report = P.analyze(7, ledger=ledger,
                       phase_windows=[f"drive={_stamp(start)},{_stamp(end)}"],
                       line_bytes_per_sec=1000)
    assert report["phases"]["drive"]["verdict"] == P.UNKNOWN
    assert "sampled CPU" in report["phases"]["drive"]["reason"]


def test_nothing_requires_an_uplink_capacity_reference(tmp_path):
    ledger, sampler, start, end = _ledger(tmp_path, "no-line-capacity", rejection_count=0)
    report = P.analyze(7, ledger=ledger, sample_path=sampler,
                       phase_windows=[f"drive={_stamp(start)},{_stamp(end)}"],
                       line_bytes_per_sec=None)
    assert report["phases"]["drive"]["verdict"] == P.UNKNOWN
    assert "uplink capacity" in report["phases"]["drive"]["reason"]


def test_unbased_cumulative_rejections_cannot_be_misattributed_to_phase(tmp_path):
    ledger, sampler, start, end = _ledger(
        tmp_path, "missing-rejection-baseline", rejection_count=2, rejection_baseline=False)
    report = P.analyze(7, ledger=ledger, sample_path=sampler,
                       phase_windows=[f"drive={_stamp(start)},{_stamp(end)}"],
                       line_bytes_per_sec=1000)
    drive = report["phases"]["drive"]
    assert drive["rejections"] is None
    assert drive["verdict"] == P.UNKNOWN


def test_historical_plan_baselines_are_rates_not_binding_verdicts():
    baselines = P.historical_baselines()
    assert baselines["drive"]["itemsPerMinute"] == pytest.approx(38_513 / 103)
    assert baselines["mail"]["itemsPerMinute"] is None
    assert baselines["dms_import"]["itemsPerMinute"] == pytest.approx(141_977 / (23 * 60))
    assert baselines["dms_check"]["itemsPerMinute"] == 830
    assert all(row["verdict"] == P.UNKNOWN for row in baselines.values())


def test_seed_analysis_selects_one_saved_seed_summary(tmp_path):
    path = tmp_path / "seed.db"
    db = MigrationDB(str(path))
    start = time.time() - 60
    run = {"kind": "seed", "pid": 42, "started_at": _stamp(start),
           "source_domain": "source.example", "target_domain": None}
    db.record_run_summary(run, {"calls": 30, "elapsed_sec": 60, "worker_slots_configured": 4})
    db.close()
    report = P.analyze(7, ledger=str(path), kind="seed")
    assert report["run"]["id"] == "seed:42:" + _stamp(start)
    assert report["phases"]["seeding"]["window"]["source"] == "run_metric_summary"
    # A migration report never borrows a seed's summary as its own phase.
    migration_report = P.analyze(7, ledger=str(path), kind="migrate")
    assert migration_report["phases"]["seeding"]["window"] is None


def test_metrics_worker_slots_are_distinct_from_live_thread_count():
    from metrics import Metrics

    m = Metrics()
    m.worker_started(4)
    m.worker_started(4)
    assert m.snapshot()["idle_workers_share"] == 0.5
    m.worker_finished()
    assert m.snapshot()["idle_workers_share"] == 0.75

