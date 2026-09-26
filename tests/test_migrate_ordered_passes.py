"""An interleaved run cannot promise that Drive links in mail resolve.

Rewriting a link needs the target id of the file it names, and a link in one
mailbox names whoever OWNED the file. The engine's own guard only asks whether
ANY Drive has migrated -- true the moment the first user finishes -- so mail
read while another user's Drive is still to come keeps that link pointed at the
source tenant for good: the message is in the ledger, every later pass skips
it, and the run reports success. `--ordered` runs Drive for every user first.

The passes live inside the shared runner (_run_with_memory_pause), not in the
command: only the shared runner may register a run, because two registrations
for one run hold two rows of a two-job cap.
"""
import argparse
import contextlib
import os

import pytest

import main


class TestThePasses:
    def test_drive_then_mail_then_the_rest(self):
        got = main.ordered_passes({"drive", "gmail", "calendar", "contacts", "tasks", "chat"})
        assert got == [{"drive"}, {"gmail"}, {"calendar", "contacts", "tasks", "chat"}]

    def test_only_what_was_asked_for_and_never_an_empty_pass(self):
        assert main.ordered_passes({"gmail"}) == [{"gmail"}]
        assert main.ordered_passes({"drive", "chat"}) == [{"drive"}, {"chat"}]
        assert main.ordered_passes(set()) == []

    def test_a_service_is_in_exactly_one_pass(self):
        every = set(main.PER_USER_SERVICES)
        flat = [s for p in main.ordered_passes(every) for s in p]
        assert sorted(flat) == sorted(every)

    def test_every_service_migrate_knows_has_a_place(self):
        """A service added to PER_USER_SERVICES and forgotten here would be silently
        skipped by an ordered run."""
        assert {s for p in main.ORDERED_PASSES for s in p} == set(main.PER_USER_SERVICES)


@pytest.fixture
def runner(monkeypatch, settings, db, capsys):
    """The shared runner with everything around run_batch stubbed out."""
    passes, registered = [], []

    @contextlib.contextmanager
    def fake_registered(name, account):
        registered.append(name)
        yield

    def fake_batch(auth, d, s, services, **kw):
        passes.append(set(services))
        if hook:
            hook[0](len(passes))
        return [{"source": f"u{len(passes)}", "services": {}}]

    hook = []
    monkeypatch.setattr(main, "_registered", fake_registered)
    monkeypatch.setattr(main, "run_batch", fake_batch)
    monkeypatch.setattr(main, "_gate_on_delegation", lambda s: None)
    monkeypatch.setattr(main, "demote_stale_running", lambda d: 0)
    monkeypatch.setattr(main, "reconcile_service_markers", lambda d: [])
    monkeypatch.setattr(main.memtrace, "start", lambda: None)
    monkeypatch.setattr(main, "_memory_watchdog", lambda stop, *a, **k: stop.wait(0.01))
    monkeypatch.setattr(main, "_metrics_flusher", lambda stop, *a, **k: stop.wait(0.01))
    main.MEMORY_PAUSE.clear(); main.SHUTDOWN.clear()

    def run(services, plan=None, delta=False, after=None):
        return main._run_with_memory_pause(None, db, settings, services, delta=delta, delta_days=0, passes=plan, after=after)
    run.passes, run.registered, run.hook, run.out = passes, registered, hook, capsys
    yield run
    main.MEMORY_PAUSE.clear(); main.SHUTDOWN.clear()


class TestTheSharedRunner:
    ALL = {"drive", "gmail", "calendar", "contacts", "tasks", "chat"}

    def test_ordered_runs_the_passes_in_order_and_returns_every_passs_results(self, runner):
        results = runner(self.ALL, main.ordered_passes(self.ALL))
        assert runner.passes == [{"drive"}, {"gmail"}, {"calendar", "contacts", "tasks", "chat"}]
        assert [r["source"] for r in results] == ["u1", "u2", "u3"]

    def test_the_whole_run_registers_once_not_once_per_pass(self, runner):
        """A pass registering and releasing would drop the run out of the admission
        table between passes: the slot cap, the dashboard and the run watcher would
        all see a run that had finished."""
        runner(self.ALL, main.ordered_passes(self.ALL))
        assert runner.registered == ["migrate"]

    def test_it_says_which_pass_it_is_on_and_which_process_is_saying_so(self, runner):
        runner(self.ALL, main.ordered_passes(self.ALL))
        lines = [l for l in runner.out.readouterr().out.splitlines() if l.startswith("PASS ")]
        assert lines == [f"PASS 1/3 pid={os.getpid()}: drive", f"PASS 2/3 pid={os.getpid()}: gmail",
                         f"PASS 3/3 pid={os.getpid()}: calendar,chat,contacts,tasks"]

    def test_an_unordered_run_is_one_pass_and_prints_no_marker(self, runner):
        runner(self.ALL)
        assert runner.passes == [self.ALL] and runner.registered == ["migrate"]
        assert "PASS " not in runner.out.readouterr().out

    def test_a_single_pass_plan_prints_no_marker_either(self, runner):
        runner({"gmail"}, main.ordered_passes({"gmail"}))
        assert "PASS " not in runner.out.readouterr().out

    def test_a_stop_during_one_pass_ends_the_run_not_just_the_pass(self, runner):
        """The operator asked it to stop. Starting mail after that is the opposite."""
        runner.hook.append(lambda n: main.SHUTDOWN.set() if n == 1 else None)
        runner(self.ALL, main.ordered_passes(self.ALL))
        assert runner.passes == [{"drive"}]

    def test_a_memory_pause_ends_the_run_and_still_exits_paused(self, runner):
        runner.hook.append(lambda n: main.MEMORY_PAUSE.set() if n == 1 else None)
        with pytest.raises(SystemExit) as e:
            runner(self.ALL, main.ordered_passes(self.ALL))
        assert e.value.code == main.EXIT_PAUSED and runner.passes == [{"drive"}]

    def test_a_delta_still_registers_as_a_delta(self, runner, monkeypatch):
        names = []
        monkeypatch.setattr(main, "_registered", contextlib.contextmanager(lambda n, a: (names.append(n), (yield))[1]))
        runner({"gmail"}, delta=True)
        assert names == ["delta"]


class TestTheCommand:
    def test_ordered_hands_the_shared_runner_the_passes(self, monkeypatch, settings, db):
        seen = {}
        monkeypatch.setattr(main, "_run_with_memory_pause", lambda *a, **kw: seen.update(kw) or [])
        monkeypatch.setattr(main, "_print_batch_summary", lambda *a, **k: None)
        monkeypatch.setattr(main, "_auto_repair", lambda *a, **k: None)
        main.cmd_migrate(argparse.Namespace(services="all", user=None, ordered=True), settings, db, None)
        assert seen["passes"] == [{"drive"}, {"gmail"}, {"calendar", "contacts", "tasks", "chat"}]

    def test_unordered_hands_it_none(self, monkeypatch, settings, db):
        seen = {}
        monkeypatch.setattr(main, "_run_with_memory_pause", lambda *a, **kw: seen.update(kw) or [])
        monkeypatch.setattr(main, "_print_batch_summary", lambda *a, **k: None)
        monkeypatch.setattr(main, "_auto_repair", lambda *a, **k: None)
        main.cmd_migrate(argparse.Namespace(services="all", user=None, ordered=False), settings, db, None)
        assert seen["passes"] is None

    def test_the_command_does_not_register_on_its_own(self):
        import inspect
        assert "_registered(" not in inspect.getsource(main.cmd_migrate)

    def test_the_flag_defaults_off(self):
        assert main.build_parser().parse_args(["migrate"]).ordered is False
        assert main.build_parser().parse_args(["migrate", "--ordered"]).ordered is True


class TestTheCheckAfterTheRun:
    """--verify-after: a quick migration checks its own work on the server, inside
    the run, with nobody watching."""
    ALL = {"drive", "gmail"}

    def test_it_runs_once_after_every_pass_and_gets_every_passs_results(self, runner):
        seen = []
        results = runner(self.ALL, main.ordered_passes(self.ALL), after=lambda res: seen.append(list(res)))
        assert len(seen) == 1 and [r["source"] for r in seen[0]] == ["u1", "u2"] and len(results) == 2

    def test_it_runs_INSIDE_the_registration(self, runner, monkeypatch):
        """Or the run would leave the job table before it had finished checking, the
        watcher would report on a run mid-flight, and its slot would go to someone
        else."""
        events = []

        @contextlib.contextmanager
        def reg(name, account):
            events.append("in"); yield; events.append("out")
        monkeypatch.setattr(main, "_registered", reg)
        runner(self.ALL, main.ordered_passes(self.ALL), after=lambda res: events.append("check"))
        assert events == ["in", "check", "out"]

    def test_it_does_not_run_after_a_stop(self, runner):
        runner.hook.append(lambda n: main.SHUTDOWN.set())
        called = []
        runner(self.ALL, main.ordered_passes(self.ALL), after=lambda res: called.append(1))
        assert called == []

    def test_a_check_that_crashes_is_reported_and_does_not_undo_the_copy(self, runner):
        def boom(res):
            raise RuntimeError("tenant unreachable")
        results = runner(self.ALL, main.ordered_passes(self.ALL), after=boom)
        assert len(results) == 2
        assert "VERIFY FAILED: RuntimeError: tenant unreachable" in runner.out.readouterr().out

    def test_no_check_unless_asked(self, runner):
        assert main.build_parser().parse_args(["migrate"]).verify_after is False
        assert main.build_parser().parse_args(["migrate", "--verify-after"]).verify_after is True

    def test_the_command_checks_exactly_the_users_the_run_touched(self, monkeypatch, settings, db):
        import verify_sample
        got = {}
        monkeypatch.setattr(verify_sample, "run_and_save", lambda auth, d, s, users, services, **kw: got.update(users=users, services=services))
        monkeypatch.setattr(main, "_run_with_memory_pause",
                            lambda *a, **kw: (kw["after"]([{"source": "b@x.com"}, {"source": "a@x.com"}, {"source": "a@x.com"}]) or []))
        monkeypatch.setattr(main, "_print_batch_summary", lambda *a, **k: None)
        monkeypatch.setattr(main, "_auto_repair", lambda *a, **k: None)
        main.cmd_migrate(argparse.Namespace(services="drive,gmail", user=None, ordered=True, verify_after=True), settings, db, None)
        assert got["users"] == ["a@x.com", "b@x.com"] and got["services"] == ("drive", "gmail")

