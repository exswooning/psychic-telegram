"""The 26 failpoint scenarios run through the campaign: alone (steps) and batched onto twins (`--batch`), no tenant."""
import json
from pathlib import Path

import pytest

import campaign as C
import campaign_failures as CF
import campaign_scenarios as S
from tests.test_campaign_executor import RecDriver, kinds


GMAIL_PROOF = {"pid": 4242, "startedAt": "2026-11-26T15:00:00.111Z", "exitCode": 0,
               "archive": "migrate.20261126T150000Z.log", "fault": "gmail.inserted@1:lost"}


class FpDriver(RecDriver):
    """A box whose fault file works: a kill spec ends the next run with 137 and is struck, a lost one fires without a death,
    a follow-on's is the API's own (no job dies); `unreached` points never fire."""

    def __init__(self, unreached=(), extra_deaths=0, blind_user=None, **kw):
        super().__init__(**kw)
        self.file, self.unreached, self.deaths, self.extra, self.blind = [], set(unreached), 0, extra_deaths, blind_user
        self.fired = set()

    def arm(self, spec): self.calls.append(("arm", spec)); self.file = [s for s in spec.split("\n") if s]
    def disarm(self): self.calls.append(("disarm",)); self.file = []
    def armed(self): return list(self.file)

    def api(self, method, path, body):
        reply = super().api(method, path, body)
        if path.endswith("/migrate/start") and reply.get("ok") is not False:
            return {"ok": True, "detail": "started pid 4242: main.py"}
        return reply

    def exit_of(self, pid):
        self.running = False
        for line in list(self.file):
            point = line.split("@")[0]
            if point in self.unreached:
                continue
            self.file.remove(line)
            self.fired.add(line)
            if ":lost" not in line and not point.startswith("followon."):
                self.deaths += 1
                return 137
        return 0

    def gmail_lost_reply_evidence(self, pid, user, timeout):
        assert self.exit_of(pid) == 0
        if "gmail.inserted@1:lost" not in self.fired:
            raise C.X.StepFailed(f"no gmail.inserted@1 lost-reply marker in Gmail shot PID {pid}")
        return {**GMAIL_PROOF, "pid": pid}

    def facts(self, user, since=None):
        out = {**self._facts, "exits": {"stopped": 0, "crashed": self.deaths + self.extra}}
        if user == self.blind:
            out.pop("equal_to_control")
        return out


class LostReplyDriver(FpDriver):
    """A clean migration exit can leave faults unfired or lack their log evidence."""

    def __init__(self, missing_marker=None, **kw):
        super().__init__(**kw)
        self.missing_marker = missing_marker
        self.markers = {"drive.copy.landed@2", "tasks.created@2"}

    def exit_of(self, pid):
        armed = set(self.file)
        rc = super().exit_of(pid)
        self.markers.update(
            point for spec in armed.difference(self.file)
            if (point := spec.partition(":")[0]) != self.missing_marker
        )
        return rc

    def log_text(self, job):
        assert job == "migrate"
        return "\n".join(f"FAILPOINT {point}: reply lost" for point in sorted(self.markers))


def test_lost_reply_chain_arms_every_point_before_start_and_checks_the_twin():
    d = LostReplyDriver()
    row = C.run_scenario(S.by_id()["fail:F-lost"], d, "n", set())

    assert row["status"] == "PASS", row
    arm = next(i for i, call in enumerate(d.calls) if call[0] == "arm")
    start = next(i for i, call in enumerate(d.calls)
                 if call[0] == "api" and call[2].endswith("/migrate/start"))
    assert arm < start
    assert d.calls[arm][1].splitlines() == CF.LOST_CHAIN
    assert sum(call[0] == "arm" for call in d.calls) == 1


@pytest.mark.parametrize(("driver", "reason"), [
    ({"unreached": {"gmail.inserted"}}, "never reached"),
    ({"missing_marker": "gmail.inserted@2"}, "no lost-reply log evidence"),
])
def test_lost_reply_chain_cannot_reuse_an_unrelated_fault_marker(driver, reason):
    d = LostReplyDriver(**driver)
    row = C.run_scenario(S.by_id()["fail:F-lost"], d, "n", set())

    assert row["status"] == "FAIL", row
    assert "gmail.inserted@2" in row["reason"] and reason in row["reason"]
    assert not any(call[0] == "api" and call[2].endswith("/one-to-one/run") for call in d.calls)


FP = [s for s in S.all_scenarios() if s.id.startswith("fp:")]
RUNNABLE = [s for s in FP if not s.inject.get("unreachable")]
UNREACHABLE = {"drive.moved_out", "shareddrive.created", "followon.dms.start", "groups.created", "groups.member.created",
               "rooms.building.created", "rooms.feature.created", "rooms.room.created"}   # tenant-wide: no twin reaches them


def point(sc):
    return sc.inject["env"]["BITPORT_FAULT"].split("@")[0]


def test_every_failpoint_scenario_runs_or_says_why_it_cannot():
    assert len(FP) == 36 and {point(s) for s in FP if s.inject.get("unreachable")} == UNREACHABLE
    assert all(s.inject.get("steps") for s in RUNNABLE) and len(RUNNABLE) == 26
    for s in FP:
        if s.inject.get("unreachable"):
            row = C.run_scenario(s, FpDriver(), "n", set())
            assert row["status"] == "UNVERIFIED" and "cannot be caused on a twin" in row["reason"]


@pytest.mark.parametrize("sc", RUNNABLE, ids=lambda s: s.id)
def test_a_failpoint_scenario_runs_alone_on_one_twin_with_its_service_only(sc):
    d = FpDriver()
    row = C.run_scenario(sc, d, "n", set())
    assert row["status"] == "PASS", row
    arm = next(c for c in d.calls if c[0] == "arm")
    assert arm[1] == sc.inject["env"]["BITPORT_FAULT"] and kinds(d).count("twin") == 1
    starts = [c[3] for c in d.calls if c[0] == "api" and c[2].endswith("/migrate/start")]
    svc = CF.FP_SERVICE[point(sc).split(".")[0]]
    assert starts[0]["services"] == [svc] and starts[-1]["services"] == ["all"]      # the restart has no fault and every service
    assert d.calls.index(arm) < d.calls.index(("disarm",))
    if sc.id == "fp:gmail.inserted:lost":
        assert row["faultEvidence"] == GMAIL_PROOF


def test_a_point_that_is_never_reached_fails_the_scenario_instead_of_passing_vacuously():
    sc = S.by_id()["fp:tasks.created:lost"]
    row = C.run_scenario(sc, FpDriver(unreached={"tasks.created"}), "n", set())
    assert row["status"] == "FAIL" and "never reached" in row["reason"]


def test_single_gmail_lost_reply_cannot_pass_when_someone_clears_the_arm():
    class ClearedArm(FpDriver):
        def exit_of(self, pid):
            self.file.clear()
            self.running = False
            return 0

        def log_text(self, job):
            return "FAILPOINT gmail.inserted@1: reply lost"  # a previous run's log

    sc = S.by_id()["fp:gmail.inserted:lost"]
    d = ClearedArm()
    row = C.run_scenario(sc, d, "n", set())
    assert row["status"] == "FAIL" and "no gmail.inserted@1 lost-reply marker" in row["reason"], row
    assert not any(call[0] == "api" and call[2].endswith("/one-to-one/run") for call in d.calls)


def test_single_gmail_lost_reply_does_not_follow_an_unacknowledged_pid():
    class QueuedStart(FpDriver):
        def api(self, method, path, body):
            reply = super().api(method, path, body)
            if path.endswith("/migrate/start"):
                return {"ok": True, "detail": "queued for capacity"}
            return reply

        def job_pid(self, **kw):
            pytest.fail("queued Gmail shot must not select a PID from another run")

    d = QueuedStart()
    row = C.run_scenario(S.by_id()["fp:gmail.inserted:lost"], d, "n", set())
    assert row["status"] == "FAIL" and "Start did not acknowledge a PID" in row["reason"], row
    assert not any(call[0] == "api" and call[2].endswith("/one-to-one/run") for call in d.calls)


@pytest.fixture
def gmail_shot_transcript(tmp_path, monkeypatch):
    import control_plane_db as cpdb
    import webui

    driver = C.LiveDriver.__new__(C.LiveDriver)
    driver.account_id, driver.started = 5, "2026-11-26T15:00:00Z"
    pid, started = 4242, "2026-11-26T15:00:00.111Z"
    argv = ["/bin/python", "main.py", "--account-id", "5", "migrate", "--services", "gmail", "--user", "t02@a.test"]
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "control.db"))
    cpdb.apply_migrations()
    with cpdb.rw() as conn:
        conn.execute("INSERT INTO active_jobs(account_id, job_name, pid, argv, started_at) VALUES(?,?,?,?,?)",
                     (5, "migrate", pid, json.dumps(argv), started))

    marker = "2026-11-26 15:00:00,900 WARNING [worker        ] failpoints: FAILPOINT gmail.inserted@1: reply lost\n"
    logs = (tmp_path / "logs" / "jobs" / "5").resolve()
    archive = logs / "runs" / GMAIL_PROOF["archive"]
    archive.parent.mkdir(parents=True)
    archive.write_text(marker)
    latest = logs / "migrate.log"
    latest.write_text(marker)
    monkeypatch.setattr(webui, "job_log_path", lambda account, job: str(latest))

    proc_root = tmp_path / "proc"
    monkeypatch.setattr(C, "PROCFS", proc_root)
    proc = proc_root / str(pid)
    fd = proc / "fd" / "1"
    fd.parent.mkdir(parents=True)
    fd.symlink_to(archive)
    (proc / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
    (proc / "stat").write_text("4242 (python 3) S " + "0 " * 18 + "987\n")

    opened, original_open = [], Path.open

    def track_transcript(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        if path == fd:
            opened.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", track_transcript)
    return driver, pid, started, archive, opened


def _record_gmail_shot_terminal(pid, started, *, rc=0, finished="2026-11-26T15:00:01.000Z"):
    import control_plane_db as cpdb
    import run_watch

    run_watch.record_started(5, "migrate", pid, started_at=started)
    with cpdb.rw() as conn:
        conn.execute("INSERT INTO run_events(account_id, job_name, event, pid, rc, at) VALUES(?,?,?,?,?,?)",
                     (5, "migrate", "finished", pid, rc, finished))


@pytest.mark.parametrize(("case", "error", "reason"), [
    ("success", None, None),
    ("latest-only marker", C.X.StepFailed, "no gmail.inserted@1 lost-reply marker"),
    ("wrong start", C.Unautomated, "no matching terminal"),
    ("older exit", C.Unautomated, "no terminal Gmail shot observed"),
    ("failed exit", C.X.StepFailed, "exited 1, expected 0"),
    ("wrong user", C.Unautomated, "own scoped transcript"),
])
def test_live_gmail_lost_reply_requires_its_admitted_process_archive_and_exit(gmail_shot_transcript, case, error, reason):
    driver, pid, started, archive, opened = gmail_shot_transcript
    user = "t03@a.test" if case == "wrong user" else "t02@a.test"
    if case == "latest-only marker":
        archive.write_text("the scoped process did not fire the fault\n")
    _record_gmail_shot_terminal(
        pid, "2026-11-26T14:59:59.900Z" if case == "wrong start" else started,
        rc=1 if case == "failed exit" else 0,
        finished="2026-11-26T15:00:00.100Z" if case == "older exit" else "2026-11-26T15:00:01.000Z",
    )
    if error:
        with pytest.raises(error, match=reason):
            driver.gmail_lost_reply_evidence(pid, user, 0)
    else:
        assert driver.gmail_lost_reply_evidence(pid, user, 0) == {
            "pid": pid, "startedAt": started, "exitCode": 0,
            "archive": archive.name, "fault": "gmail.inserted@1:lost",
        }
    assert len(opened) == (0 if case == "wrong user" else 1)
    assert all(handle.closed for handle in opened)


def test_the_batches_hold_what_can_share_a_run_and_leave_the_rest_alone():
    batches, alone = CF.fp_batches(FP, 6)
    assert [len(b) for b in batches] == [6, 6, 6, 6] and sum(map(len, batches)) == 24
    assert {s.id for s in alone} == {s.id for s in FP if s.inject.get("unreachable")} | {
        "fp:followon.repair.start:kill", "fp:gmail.inserted:lost",
    }
    assert all(len(b) <= 2 for b in CF.fp_batches(FP, 2)[0])


def test_a_batch_arms_each_fault_for_its_own_twin_runs_once_per_kill_plus_one_and_judges_every_twin():
    batch = [S.by_id()[i] for i in ("fp:drive.copy.landed:lost", "fp:gmail.inserted:kill", "fp:calendar.imported:kill",
                                    "fp:tasks.created:lost", "fp:run.pass_done:kill")]
    d = FpDriver()
    rows = C._run_batch(batch, d, "n", set())
    assert [r["status"] for r in rows] == ["PASS"] * 5 and {tuple(r["batch"]) for r in rows} == {tuple(s.id for s in batch)}
    assert kinds(d).count("twin") == 5 and len({r["user"] for r in rows}) == 5       # one twin each, one take each
    spec = next(c[1] for c in d.calls if c[0] == "arm").split("\n")
    assert spec == ["drive.copy.landed@1:lost#t02@a.test", "gmail.inserted@1#t03@a.test", "calendar.imported@1#t04@a.test",
                    "tasks.created@1:lost#t05@a.test", "run.pass_done@1"]          # a run.* point has no user to scope to
    starts = [c[3] for c in d.calls if c[0] == "api" and c[2].endswith("/migrate/start")]
    assert len(starts) == 3 + 1 + 1                                                  # 3 kills + 1 more, then the restart
    assert all(s["users"] == [r["user"] for r in rows] for s in starts)
    assert sorted(starts[0]["services"]) == ["calendar", "drive", "gmail", "tasks"] and starts[-1]["services"] == ["all"]
    assert len([c for c in d.calls if c[0] == "api" and "tally" in c[2]]) == 1       # one tally for all five twins


def test_one_unreached_point_fails_its_own_scenario_only():
    batch = [S.by_id()[i] for i in ("fp:drive.copy.landed:kill", "fp:gmail.inserted:kill", "fp:tasks.created:kill")]
    rows = C._run_batch(batch, FpDriver(unreached={"gmail.inserted"}), "n", set())
    assert [r["status"] for r in rows] == ["PASS", "FAIL", "PASS"] and "never reached" in rows[1]["reason"]


def test_a_run_that_died_for_another_reason_runs_every_scenario_alone_and_says_so():
    batch = [S.by_id()[i] for i in ("fp:drive.copy.landed:kill", "fp:contacts.created:lost")]
    d = FpDriver(extra_deaths=1)
    rows = C._run_batch(batch, d, "n", set())
    assert all("batch ambiguous" in r["reason"] and "run alone" in r["reason"] for r in rows)
    assert kinds(d).count("twin") == 2 + 2                                           # the batch's twins, then one each


def test_a_twin_whose_facts_cannot_be_read_runs_alone_and_the_others_keep_their_verdict():
    batch = [S.by_id()[i] for i in ("fp:drive.copy.landed:kill", "fp:contacts.created:lost")]
    d = FpDriver(blind_user="t03@a.test")
    rows = C._run_batch(batch, d, "n", set())
    assert "batch ambiguous" not in rows[0]["reason"] and rows[0]["status"] == "PASS"
    assert rows[1]["status"] == "PASS" and "batch ambiguous" in rows[1]["reason"] and kinds(d).count("twin") == 3


def test_run_with_batch_batches_the_failpoints_and_runs_everything_else_as_before(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "HERE", str(tmp_path))
    d = FpDriver()
    C.run("n", d, group="failures", only={"fp:drive.copy.landed:kill", "fp:gmail.inserted:kill", "fp:shareddrive.created:kill",
                                          "fp:followon.repair.start:kill"}, batch=4, out=lambda *_: None)
    done = C.read_results("n")
    assert done["fp:drive.copy.landed:kill"]["batch"] == ["fp:drive.copy.landed:kill", "fp:gmail.inserted:kill"]
    assert "batch" not in done["fp:followon.repair.start:kill"] and done["fp:followon.repair.start:kill"]["status"] == "PASS"
    assert done["fp:shareddrive.created:kill"]["status"] == "UNVERIFIED"


def test_batched_campaign_cannot_pass_gmail_lost_reply_without_run_bound_proof(tmp_path, monkeypatch):
    class ClearedGmailArm(FpDriver):
        def exit_of(self, pid):
            self.file = [line for line in self.file if not line.startswith("gmail.inserted@1:lost")]
            return super().exit_of(pid)

    monkeypatch.setattr(C, "HERE", str(tmp_path))
    gmail = "fp:gmail.inserted:lost"
    batch_cases = {"fp:drive.copy.landed:kill", "fp:tasks.created:kill"}
    rows = C.run("n", ClearedGmailArm(), only={gmail, *batch_cases}, batch=4, out=lambda *_: None)

    assert rows[gmail]["status"] == "FAIL" and "lost-reply marker" in rows[gmail]["reason"]
    assert "batch" not in rows[gmail]
    assert all(rows[sid]["status"] == "PASS" and set(rows[sid]["batch"]) == batch_cases for sid in batch_cases)


def test_legacy_gmail_lost_reply_pass_is_unverified_and_does_not_skip_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "HERE", str(tmp_path))
    gmail = "fp:gmail.inserted:lost"
    prior = {"id": gmail, "status": "PASS", "reason": "old batch found no remaining arm", "batch": [gmail]}
    C.append_result("n", prior)

    read = C.read_results("n")
    assert read[gmail]["status"] == "UNVERIFIED" and "run-bound" in read[gmail]["reason"]
    assert f"{gmail} | UNVERIFIED" in C.render_md("n", [S.by_id()[gmail]], read)

    driver = FpDriver()
    C.run("n", driver, only={gmail}, batch=4, out=lambda *_: None)
    assert kinds(driver).count("twin") == 1
    assert C.read_results("n")[gmail]["status"] == "PASS"
    path = Path(C.results_dir("n")) / "results.jsonl"
    assert json.loads(path.read_text().splitlines()[0]) == prior
    driver.calls.clear()
    C.run("n", driver, only={gmail}, batch=4, out=lambda *_: None)
    assert driver.calls == []


@pytest.mark.parametrize(("field", "value"), [
    ("pid", True), ("pid", 0), ("fault", "gmail.inserted@2:lost"), ("exitCode", False), ("exitCode", 1),
    ("startedAt", "2026-11-26"), ("startedAt", "invalid"),
    ("archive", "migrate.log"), ("archive", "arbitrary/migrate.20261126T150000Z.log"),
])
def test_gmail_lost_reply_pass_requires_credible_persisted_evidence(tmp_path, monkeypatch, field, value):
    monkeypatch.setattr(C, "HERE", str(tmp_path))
    gmail = "fp:gmail.inserted:lost"
    evidence = {**GMAIL_PROOF, field: value}
    C.append_result("n", {"id": gmail, "status": "PASS", "faultEvidence": evidence})

    assert C.read_results("n")[gmail]["status"] == "UNVERIFIED"
