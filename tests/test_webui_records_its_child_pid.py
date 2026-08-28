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
        # record_launch now, not record_pid: the argv is stored alongside
        # the pid so job_supervisor can put a wedged job back. See
        # test_supervisor_resumes.py.
        recorded = []
        monkeypatch.setattr(job_admission, "record_launch",
                            lambda a, n, p, argv, cwd: recorded.append((a, n, p)))
        monkeypatch.setattr(webui.job_admission, "record_launch",
                            lambda a, n, p, argv, cwd: recorded.append((a, n, p)))
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
        def boom(*a, **k):
            raise RuntimeError("control plane unreachable")

        monkeypatch.setattr(webui.job_admission, "record_launch", boom)
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


class TestARestartReAdoptsItsChildren:
    """A deploy mid-job forgot the job.

    systemd's KillMode=process deliberately keeps these children alive
    across a restart, and the fresh process reconciles the admission table
    against them. It only ever RELEASED rows whose process was gone; a row
    whose process was still running it left exactly as it found it -- with
    pid NULL, because record_pid was never called here. Two minutes later
    is_live() called that row dead and it was reaped, mid-job.
    """

    def _reconcile(self, monkeypatch, active, running):
        released, recorded = [], []
        monkeypatch.setattr(webui.job_admission, "list_active", lambda: active)
        monkeypatch.setattr(webui.job_admission, "release",
                            lambda a, n: released.append((a, n)))
        monkeypatch.setattr(webui.job_admission, "record_pid",
                            lambda a, n, p: recorded.append((a, n, p)))
        monkeypatch.setattr(webui, "_external_processes", lambda: running)
        webui._reconcile_active_jobs()
        return released, recorded

    def test_a_row_whose_process_still_runs_is_re_adopted(self, monkeypatch):
        released, recorded = self._reconcile(
            monkeypatch,
            active=[{"account_id": 66, "job_name": "seed", "pid": None}],
            running=[{"pid": 2747119, "name": "seed", "elapsed": 1900}])
        assert recorded == [(66, "seed", 2747119)]
        assert released == [], "released a job that was still running"

    def test_a_row_whose_process_is_gone_is_still_released(self, monkeypatch):
        # The original bug this function exists for: a phantom row wedged
        # the whole box at zero capacity.
        released, recorded = self._reconcile(
            monkeypatch,
            active=[{"account_id": 66, "job_name": "seed", "pid": None}],
            running=[])
        assert released == [(66, "seed")]
        assert recorded == []

    def test_a_job_this_process_does_not_own_is_left_alone(self, monkeypatch):
        released, recorded = self._reconcile(
            monkeypatch,
            active=[{"account_id": 7, "job_name": "migrate", "pid": 1}],
            running=[])
        assert released == [] and recorded == []

    def test_a_failure_to_re_adopt_does_not_block_startup(self, monkeypatch):
        def boom(*a):
            raise RuntimeError("control plane unreachable")

        monkeypatch.setattr(webui.job_admission, "list_active",
                            lambda: [{"account_id": 66, "job_name": "seed",
                                      "pid": None}])
        monkeypatch.setattr(webui.job_admission, "record_launch", boom)
        monkeypatch.setattr(webui.job_admission, "release", lambda a, n: None)
        monkeypatch.setattr(webui, "_external_processes",
                            lambda: [{"pid": 5, "name": "seed", "elapsed": 1}])
        webui._reconcile_active_jobs()      # must not raise
