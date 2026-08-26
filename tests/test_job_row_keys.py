"""Job rows are read by the key they are actually written with.

job_admission.list_active() returns account_id / job_name / pid /
started_at. Three separate call sites asked for "name", which has never
been a key on those rows -- so each check silently matched nothing:

  * the Performance page read "no metrics recorded yet" through an entire
    live migration, because a superadmin always fell back to their own
    empty account instead of the run that was actually going;
  * a running test suite reported itself as finished;
  * the guard against launching a second suite never fired.

A wrong key raises nothing and logs nothing. It just quietly turns a
feature off, which is why this is a test and not a comment.
"""
import os
import tempfile

import pytest

import control_plane_db as cpdb
import job_admission
from db import MigrationDB


@pytest.fixture
def cp(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    with cpdb.rw() as conn:
        conn.execute("DELETE FROM active_jobs")
    yield
    try:
        os.unlink(path)
    except OSError:
        pass


class TestTheRowsExposeJobName:
    def test_list_active_uses_job_name(self, cp):
        job_admission.try_admit(7, "migrate")
        row = job_admission.list_active()[0]
        assert "job_name" in row
        assert row["job_name"] == "migrate"

    def test_there_is_no_name_key_to_read(self, cp):
        job_admission.try_admit(7, "migrate")
        row = job_admission.list_active()[0]
        assert row.get("name") is None, \
            "code reading .get('name') matches nothing and fails silently"


class TestNoCallSiteAsksForTheWrongKey:
    def test_the_source_has_no_name_lookups_on_job_rows(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for fname in ("api_server.py", "webui.py"):
            src = open(os.path.join(root, fname), encoding="utf-8").read()
            assert 'get("name") in _OWNED_JOB_NAMES' not in src, fname
            assert 'get("name") == "tests"' not in src, fname

    def test_owned_job_names_match_what_is_admitted(self):
        import api_server
        # If these drift apart the metrics endpoint stops finding runs again,
        # with no error anywhere.
        assert "migrate" in api_server._OWNED_JOB_NAMES
        assert "delta" in api_server._OWNED_JOB_NAMES
