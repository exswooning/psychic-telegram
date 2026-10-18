"""A run's report is graded against the users it ran (their own tally and one-to-one
check), not left UNVERIFIED for want of a whole-tenant tally; one report per run."""
import json
import sqlite3

import run_report as R
from db import MigrationDB


def _ledger(tmp_path, tally_at="2026-10-18T06:40:00Z", verified_at="2026-10-18T06:41:00Z"):
    p = str(tmp_path / "m.db")
    d = MigrationDB(p)
    with d.write() as c:
        c.execute("INSERT INTO audit_log (source_user,item_id,item_type,status,timestamp) VALUES "
                  "('info@g.com','f1','file','SUCCESS','2026-10-18T06:33:00Z')")
        c.execute("INSERT INTO user_tally VALUES ('info@g.com','info@s.com',?,1.0,?)", (tally_at, json.dumps(
            {"services": {"drive_files": {"source": 121, "target": 136, "expected": 121},
                          "mail": {"source": 484, "target": 485, "expected": 484}}})))
        c.execute("INSERT INTO user_verification VALUES ('info@g.com','drive',?, 'IDENTICAL',25,25,136,?)",
                  (verified_at, json.dumps({"differences": [], "notes": ["sharing: all 9 grants matched"]})))
    d.close()
    return sqlite3.connect(p)


RUN = {"startedAt": "2026-10-18T06:32:48.869Z"}


def test_graded_from_the_users_the_run_wrote(tmp_path):
    f = R._run_users_fidelity(_ledger(tmp_path), RUN)
    assert f["countParity"] == 1.0 and f["checksumFailures"] == 0 and f["extraGrants"] == 0
    assert f["timestampsPreserved"] == 1.0 and f["aclFidelity"] == 1.0 and f["users"] == 1


def test_a_check_from_before_the_run_does_not_grade_it(tmp_path):
    assert R._run_users_fidelity(_ledger(tmp_path, tally_at="2026-10-17T00:00:00Z"), RUN) == {}


def test_per_user_rows_carry_services_and_counts(tmp_path):
    rows = R._per_user(_ledger(tmp_path))
    assert rows[0]["services"]["drive"]["identical"] == 25
    assert rows[0]["counts"]["drive_files"] == [121, 136]


def test_a_regrade_replaces_the_report_built_at_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "reports_dir", lambda aid: str(tmp_path))
    old = {"id": "migration-1", "kind": "migration", "facts": {"run": {"startedAt": "S"}}}
    (tmp_path / "migration-1.json").write_text(json.dumps(old))
    (tmp_path / "migration-1.human.pdf").write_text("x")
    R._drop_earlier_of_same_run({"id": "migration-2", "kind": "migration",
                                 "facts": {"run": {"startedAt": "S"}}}, 3)
    assert not (tmp_path / "migration-1.json").exists()
    assert not (tmp_path / "migration-1.human.pdf").exists()
