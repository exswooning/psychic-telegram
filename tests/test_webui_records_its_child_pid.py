"""Every job webui launched left pid NULL in active_jobs.

record_pid was called only from api_server.py. is_live() reads a pid-less
row as dead once it is more than 120 seconds old, so two minutes into any
webui-launched job the row was reaped and three things broke at once:

  * Running Now went blank while the job was plainly still running --
    confirmed live on a 32-minute seed, mid-run, with active-jobs [] and
    /api/job reporting {"name": "seed", "running": true, "pid": 2747119}
  * MAX_CONCURRENT_TENANT_JOBS stopped applying, so a second heavy job
    could start on top of the first
  * job_supervisor never saw it at all, because it iterates that table --
    so the stall detection could not have protected any of these jobs
"""
import os

import job_admission
import webui

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Proc:
    pid = 4242

    def poll(self):
        return None


class TestTheLaunchRecordsThePid:
    def test_start_records_the_child_pid(self, monkeypatch, tmp_path):
        recorded = []
        monkeypatch.setattr(job_admission, "record_pid",
                            lambda a, n, p: recorded.append((a, n, p)))
        monkeypatch.setattr(webui.job_admission, "record_pid",
                            lambda a, n, p: recorded.append((a, n, p)))
        monkeypatch.setattr(webui.subprocess, "Popen", lambda *a, **k: _Proc())
        monkeypatch.setattr(webui.threading, "Thread",
                            lambda *a, **k: type("T", (), {"start": lambda s: None})())
        monkeypatch.setattr(webui, "job_log_path",
                            lambda aid, name: str(tmp_path / "j.log"))

        job = webui.Job(66)
        ok, msg = job.start("seed", ["/bin/true"])
        assert ok, msg
        assert recorded == [(66, "seed", 4242)]

    def test_a_failure_to_record_does_not_fail_the_launch(self, monkeypatch,
                                                          tmp_path):
        """The job is already running by then -- refusing to report it
        started would leave a real child nobody is tracking."""
        def boom(*a):
            raise RuntimeError("control plane unreachable")

        monkeypatch.setattr(webui.job_admission, "record_pid", boom)
        monkeypatch.setattr(webui.subprocess, "Popen", lambda *a, **k: _Proc())
        monkeypatch.setattr(webui.threading, "Thread",
                            lambda *a, **k: type("T", (), {"start": lambda s: None})())
        monkeypatch.setattr(webui, "job_log_path",
                            lambda aid, name: str(tmp_path / "j.log"))
        ok, _ = webui.Job(66).start("seed", ["/bin/true"])
        assert ok

    def test_it_records_the_name_admission_used(self):
        """try_admit and record_pid must agree on the job name, or the row
        is never matched and stays pid NULL anyway."""
        src = open(os.path.join(ROOT, "webui.py"), encoding="utf-8").read()
        for path, job in (('"/api/seed"', '"seed"'),
                          ('"/api/reset_target"', '"reset target"'),
                          ('"/api/wipe_target"', '"wipe target"')):
            block = src.split(f"if self.path == {path}:")[1][:1200]
            assert f"try_admit(account_id, {job})" in block, path
            assert f"get_job(account_id).start(\n                {job}" in block \
                or f"start(\n                {job}" in block, path


class TestAPidlessRowIsWhyThisMattered:
    def test_is_live_gives_a_pidless_row_only_two_minutes(self):
        stale = {"pid": None, "started_at": "2020-01-01T00:00:00.000Z"}
        assert not job_admission.is_live(stale)

    def test_a_row_with_a_live_pid_survives(self):
        assert job_admission.is_live({"pid": os.getpid(),
                                      "started_at": "2020-01-01T00:00:00.000Z"})
