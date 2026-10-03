"""A run's numbers are kept per run, labelled with its domains -- run_metrics is the
last hour of samples, rolled over, so a run's numbers were gone an hour after it."""
from db import MigrationDB

RUN = {"kind": "migrate", "pid": 7, "started_at": "2026-10-03T10:23:16Z",
       "source_domain": "src.example", "target_domain": "tgt.example"}


def test_samples_fold_into_one_row_with_peaks(tmp_path):
    db = MigrationDB(str(tmp_path / "l.db"))
    db.record_run_summary(RUN, {"calls": 100, "requests_per_sec": 40.0, "rss_mb": 300, "workers": 8})
    db.record_run_summary(RUN, {"calls": 900, "requests_per_sec": 25.0, "rss_mb": 280, "workers": 12})
    rows = db.run_summaries()
    assert len(rows) == 1
    r = rows[0]
    assert (r["sourceDomain"], r["targetDomain"], r["kind"]) == ("src.example", "tgt.example", "migrate")
    assert r["calls"] == 900 and r["requests_per_sec"] == 25.0
    assert r["peak_requests_per_sec"] == 40.0 and r["peak_rss_mb"] == 300 and r["peak_workers"] == 12


def test_runs_are_kept_apart_newest_first(tmp_path):
    db = MigrationDB(str(tmp_path / "l.db"))
    db.record_run_summary(RUN, {"calls": 1})
    db.record_run_summary({**RUN, "kind": "seed", "pid": 9, "started_at": "2026-10-03T11:00:00Z",
                           "target_domain": None}, {"calls": 2})
    assert [r["kind"] for r in db.run_summaries()] == ["seed", "migrate"]


def test_the_flusher_labels_its_samples_with_the_run(tmp_path, monkeypatch):
    import threading

    import main
    db = MigrationDB(str(tmp_path / "l.db"))
    stop = threading.Event()
    calls = []
    monkeypatch.setattr(db, "record_run_summary", lambda run, p: (calls.append(run), stop.set()))
    st = type("S", (), {"source_domain": "src.example", "target_domain": "tgt.example"})()
    main._metrics_flusher(stop, db, interval=0.01, run=main._run_label(st, "migrate"))
    assert calls and calls[0]["source_domain"] == "src.example" and calls[0]["kind"] == "migrate"
