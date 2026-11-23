"""The 26 failpoint scenarios run through the campaign: alone (steps) and batched onto twins (`--batch`), no tenant."""
import pytest

import campaign as C
import campaign_failures as CF
import campaign_scenarios as S
from tests.test_campaign_executor import RecDriver, kinds


class FpDriver(RecDriver):
    """A box whose fault file works: a kill spec ends the next run with 137 and is struck, a lost one fires without a death,
    a follow-on's is the API's own (no job dies); `unreached` points never fire."""

    def __init__(self, unreached=(), extra_deaths=0, blind_user=None, **kw):
        super().__init__(**kw)
        self.file, self.unreached, self.deaths, self.extra, self.blind = [], set(unreached), 0, extra_deaths, blind_user

    def arm(self, spec): self.calls.append(("arm", spec)); self.file = [s for s in spec.split("\n") if s]
    def disarm(self): self.calls.append(("disarm",)); self.file = []
    def armed(self): return list(self.file)

    def exit_of(self, pid):
        self.running = False
        for line in list(self.file):
            point = line.split("@")[0]
            if point in self.unreached:
                continue
            self.file.remove(line)
            if ":lost" not in line and not point.startswith("followon."):
                self.deaths += 1
                return 137
        return 0

    def facts(self, user, since=None):
        out = {**self._facts, "exits": {"stopped": 0, "crashed": self.deaths + self.extra}}
        if user == self.blind:
            out.pop("equal_to_control")
        return out


FP = [s for s in S.all_scenarios() if s.id.startswith("fp:")]
RUNNABLE = [s for s in FP if not s.inject.get("unreachable")]
UNREACHABLE = {"drive.moved_out", "shareddrive.created", "followon.dms.start"}


def point(sc):
    return sc.inject["env"]["BITPORT_FAULT"].split("@")[0]


def test_every_failpoint_scenario_runs_or_says_why_it_cannot():
    assert len(FP) == 26 and {point(s) for s in FP if s.inject.get("unreachable")} == UNREACHABLE
    assert all(s.inject.get("steps") for s in RUNNABLE) and len(RUNNABLE) == 21
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


def test_a_point_that_is_never_reached_fails_the_scenario_instead_of_passing_vacuously():
    sc = S.by_id()["fp:tasks.created:lost"]
    row = C.run_scenario(sc, FpDriver(unreached={"tasks.created"}), "n", set())
    assert row["status"] == "FAIL" and "never reached" in row["reason"]


def test_the_batches_hold_what_can_share_a_run_and_leave_the_rest_alone():
    batches, alone = CF.fp_batches(FP, 6)
    assert [len(b) for b in batches] == [6, 6, 6, 2] and sum(map(len, batches)) == 20
    assert {s.id for s in alone} == {s.id for s in FP if s.inject.get("unreachable")} | {"fp:followon.repair.start:kill"}
    assert all(len(b) <= 2 for b in CF.fp_batches(FP, 2)[0])


def test_a_batch_arms_each_fault_for_its_own_twin_runs_once_per_kill_plus_one_and_judges_every_twin():
    batch = [S.by_id()[i] for i in ("fp:drive.copy.landed:kill", "fp:gmail.inserted:lost", "fp:calendar.imported:kill",
                                    "fp:tasks.created:lost", "fp:run.pass_done:kill")]
    d = FpDriver()
    rows = C._run_batch(batch, d, "n", set())
    assert [r["status"] for r in rows] == ["PASS"] * 5 and {tuple(r["batch"]) for r in rows} == {tuple(s.id for s in batch)}
    assert kinds(d).count("twin") == 5 and len({r["user"] for r in rows}) == 5       # one twin each, one take each
    spec = next(c[1] for c in d.calls if c[0] == "arm").split("\n")
    assert spec == ["drive.copy.landed@1#t02@a.test", "gmail.inserted@1:lost#t03@a.test", "calendar.imported@1#t04@a.test",
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
