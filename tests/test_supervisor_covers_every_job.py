"""A wedged job needed a person. Now it does not.

Two gaps, both hit live this session:

1. The supervisor's activity signal was the ledger. seed, reset, provision,
   check_seed and teardown write no id_mapping rows at all, so
   last_ledger_write returns whatever a PREVIOUS run left -- arbitrarily
   old -- and the ledger half of the stall test is satisfied the moment
   they start. Only the CPU half kept them alive, which is luck rather
   than design. Their transcript is the honest signal.

2. It went straight to SIGKILL, and only ever killed. The case that
   actually happened was the opposite: seed_sandbox took SIGINT, unwound
   into ThreadPoolExecutor.__exit__, joined workers blocked in a Google
   API call, and sat in _wait_for_tstate_lock for 25 minutes reporting
   running=True. A person had to notice and kill it.
"""
import job_supervisor


class _Fixture:
    """One live job, with every signal under the test's control."""

    def __init__(self, *, ledger_age=None, output_age=None, cpu=(5, 5)):
        self.now = 10_000.0
        self.ledger_age = ledger_age
        self.output_age = output_age
        self._cpu = list(cpu)
        self.interrupted, self.killed = [], []
        self.job = {"pid": 4242, "job_name": "seed", "account_id": 66}

    def supervisor(self, stall=900):
        job_supervisor.job_admission.list_active = lambda: [self.job]
        job_supervisor.job_admission.is_live = lambda j: True
        job_supervisor.job_admission.reap_dead = lambda *a, **k: None
        job_supervisor.last_ledger_write = lambda db: "ledger" if self.ledger_age is not None else None
        job_supervisor._age_seconds = lambda iso, now: self.ledger_age
        return job_supervisor.Supervisor(
            db_path_for=lambda aid: "/x.db" if self.ledger_age is not None else None,
            stall_seconds=stall,
            cpu_fn=lambda pid: self._cpu[min(len(self._cpu) - 1, 0)],
            kill_fn=lambda pid: self.killed.append(pid),
            signal_fn=lambda pid: self.interrupted.append(pid),
            now_fn=lambda: self.now,
            output_fn=lambda name, aid: (None if self.output_age is None
                                         else self.now - self.output_age))

    def pass_(self, sup, advance=0.0):
        self.now += advance
        return sup.check_once()

    def until_acted(self, sup, passes=3, gap=1000.0):
        """Enough passes for the two-confirmation rule to conclude.

        Pass 1 only establishes the CPU baseline (prev is None), pass 2
        records first-seen-stale, and only a pass after stall_seconds has
        elapsed acts. One sample could straddle an idle moment between two
        units of work, which is what that rule is protecting.
        """
        for _ in range(passes):
            self.pass_(sup, gap)


class TestATranscriptCountsAsBeingAlive:
    def test_a_seed_writing_its_log_is_left_alone(self):
        """No ledger rows ever, but the transcript moved ten seconds ago."""
        f = _Fixture(ledger_age=99_999, output_age=10)
        sup = f.supervisor()
        for _ in range(4):
            f.pass_(sup, 1000)
        assert f.interrupted == [] and f.killed == []

    def test_a_job_with_no_ledger_path_is_still_watched(self):
        # db_path_for returning None used to skip the job entirely.
        f = _Fixture(ledger_age=None, output_age=5000)
        sup = f.supervisor()
        f.until_acted(sup)
        assert f.interrupted == [4242]

    def test_no_evidence_at_all_is_never_a_kill(self):
        f = _Fixture(ledger_age=None, output_age=None)
        sup = f.supervisor()
        for _ in range(4):
            f.pass_(sup, 1000)
        assert f.interrupted == [] and f.killed == []

    def test_the_freshest_signal_wins(self):
        """A migration whose ledger is quiet but whose log is moving --
        counting the ledger alone would kill it."""
        f = _Fixture(ledger_age=99_999, output_age=3)
        sup = f.supervisor()
        for _ in range(4):
            f.pass_(sup, 1000)
        assert f.killed == []


class TestItInterruptsBeforeItKills:
    def _wedged(self):
        f = _Fixture(ledger_age=99_999, output_age=99_999)
        return f, f.supervisor()

    def test_the_first_action_is_an_interrupt_not_a_kill(self):
        f, sup = self._wedged()
        f.until_acted(sup)
        assert f.interrupted == [4242]
        assert f.killed == [], "SIGKILL throws away committed state"

    def test_a_child_that_obeys_is_never_killed(self):
        f, sup = self._wedged()
        f.until_acted(sup)
        assert f.interrupted == [4242]
        # it starts writing again -- the cooperative path worked
        f.output_age = 2
        for _ in range(3):
            f.pass_(sup, 1000)
        assert f.killed == []

    def test_a_child_that_ignores_the_interrupt_is_killed(self):
        """seed_sandbox in _wait_for_tstate_lock: the exact live case."""
        f, sup = self._wedged()
        f.until_acted(sup)
        assert f.interrupted == [4242] and f.killed == []
        f.pass_(sup, 1000)        # grace window elapsed, still silent
        assert f.killed == [4242]

    def test_it_does_not_kill_immediately_after_interrupting(self):
        f, sup = self._wedged()
        f.until_acted(sup)
        f.pass_(sup, 10)          # well inside the grace window
        assert f.killed == []

    def test_recovery_clears_the_interrupt_so_it_starts_over(self):
        f, sup = self._wedged()
        f.until_acted(sup)
        assert f.interrupted == [4242]
        f.output_age = 1          # alive again
        f.pass_(sup, 1000)
        f.output_age = 99_999     # wedges later
        f.pass_(sup, 1000); f.pass_(sup, 1000)
        # interrupted a second time rather than jumping straight to kill
        assert f.interrupted == [4242, 4242]
        assert f.killed == []


class TestAnOldTranscriptIsNotEvidence:
    """The trap the ledger already had, rediscovered in the new signal.

    logs/jobs/7/delta.log survives the run that wrote it. Reading its mtime
    without checking it belongs to THIS run reported a healthy job as
    "written nothing for 102487s" and interrupted it -- caught by an
    existing test that only stubbed kill_fn, which is also how the first
    version of this sent a real SIGINT to the pytest process running it.
    """

    def test_a_transcript_older_than_the_run_is_ignored(self):
        f = _Fixture(ledger_age=None, output_age=99_999)
        f.job["started_at"] = "2026-08-27T12:00:00Z"
        sup = f.supervisor()
        # transcript mtime a day before the job started
        sup.output_fn = lambda name, aid: job_supervisor._epoch(
            "2026-08-26T12:00:00Z")
        f.until_acted(sup, passes=4)
        assert f.interrupted == [] and f.killed == []

    def test_a_transcript_written_by_this_run_still_counts(self):
        f = _Fixture(ledger_age=None, output_age=None)
        f.job["started_at"] = "2026-08-27T12:00:00Z"
        sup = f.supervisor()
        started = job_supervisor._epoch("2026-08-27T12:00:00Z")
        f.now = started + 200_000
        sup.output_fn = lambda name, aid: started + 10   # this run, long ago
        f.until_acted(sup, passes=4)
        assert f.interrupted == [4242]

    def test_a_row_with_no_start_time_still_uses_the_transcript(self):
        # Older rows predate started_at; refusing to look would silently
        # stop watching them.
        f = _Fixture(ledger_age=None, output_age=50_000)
        f.job.pop("started_at", None)
        sup = f.supervisor()
        f.until_acted(sup, passes=4)
        assert f.interrupted == [4242]


class TestASignalStubCoversBothPaths:
    def test_supplying_only_kill_fn_never_sends_a_real_signal(self):
        """A caller that took control of how the process ends did so to stop
        real signals. A second signalling path with its own os.kill default
        quietly re-armed that."""
        sent = []
        sup = job_supervisor.Supervisor(db_path_for=lambda a: None,
                                        kill_fn=lambda pid: sent.append(pid))
        assert sup.signal_fn is not None
        sup.signal_fn(999)
        assert sent == [999], "signal_fn bypassed the injected kill_fn"
