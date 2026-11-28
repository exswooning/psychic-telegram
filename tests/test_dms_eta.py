import json

import dms_migrate


def _hist(tmp_path, rows):
    p = tmp_path / "m.history.jsonl"
    p.write_text("".join(json.dumps({"status": s, "read_at": t, "metrics": {
        "Emails discovered": d, "Emails imported": i, "Emails skipped": 0}}) + "\n" for s, t, d, i in rows))
    return str(p)


def test_eta_from_this_imports_own_pace(tmp_path):
    p = _hist(tmp_path, [("complete", 0, 50, 50),                # an earlier import
                         ("in progress", 1000, 10000, 100),      # this one starts again near zero
                         ("in progress", 4600, 10000, 1900)])    # 1,800 in an hour
    e = dms_migrate.import_eta(p)
    assert e["perHour"] == 1800 and e["remaining"] == 8100 and e["etaSeconds"] == 16200


def test_no_estimate_without_honest_evidence(tmp_path):
    assert dms_migrate.import_eta(str(tmp_path / "none")) is None
    assert dms_migrate.import_eta(_hist(tmp_path, [("in progress", 0, 100, 10)])) is None
    assert dms_migrate.import_eta(_hist(tmp_path, [("in progress", 0, 100, 10), ("in progress", 60, 100, 20)])) is None
    assert dms_migrate.import_eta(_hist(tmp_path, [("in progress", 0, 0, 10), ("in progress", 3600, 0, 20)])) is None
    assert dms_migrate.import_eta(_hist(tmp_path, [("in progress", 0, 9, 1), ("complete", 3600, 9, 9)])) == {"done": True}
    # already past the total it is measured against: no estimate rather than a negative one
    assert dms_migrate.import_eta(_hist(tmp_path, [("in progress", 0, 50, 50), ("in progress", 3600, 90, 90)]), 80) is None


def test_the_source_scan_is_the_total_when_the_console_counts_as_it_goes(tmp_path):
    """a. -> b., 22 Nov 2026: 'Emails discovered' equalled 'Emails imported' in every reading."""
    p = _hist(tmp_path, [("in progress", 100, 1000, 1000), ("in progress", 3700, 2800, 2800)])
    assert dms_migrate.import_eta(p) is None
    e = dms_migrate.import_eta(p, 11553)
    assert e["total"] == 11553 and e["perHour"] == 1800 and e["etaSeconds"] == int(8753 / 0.5)


def test_source_mail_total_reads_the_latest_scan_per_user(tmp_path):
    import sqlite3
    db = tmp_path / "l.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE discovery(source_user TEXT, scanned_at TEXT, messages_total INTEGER)")
    c.executemany("INSERT INTO discovery VALUES (?,?,?)", [("a", "1", 10), ("a", "2", 30), ("b", "1", 5)])
    c.commit(); c.close()
    assert dms_migrate.source_mail_total(str(db)) == 35
    assert dms_migrate.source_mail_total(str(tmp_path / "none.db")) == 0
