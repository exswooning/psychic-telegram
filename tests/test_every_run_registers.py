"""
tests/test_every_run_registers.py
=================================
A delta is a migration too.

_registered() exists to announce a CLI run in active_jobs, and its own
docstring lists what breaks without it: the dashboard draws a running
tenant as idle, MAX_CONCURRENT_TENANT_JOBS cannot see the run so a SECOND
one can start on top of it, and config._concurrent_jobs sizes the worker
pool as though this run owned the machine alone.

It was applied to cmd_migrate and not to cmd_delta, so all three were live
for every delta ever run -- found by pointing the new `status` command at a
delta that had been running for three hours and watching it print "IDLE".

Registration now lives in _run_with_memory_pause, which every path into
run_batch goes through, because a guard at one call site is a guard the
next call site forgets.
"""

from __future__ import annotations

import inspect

import main


class TestRegistrationIsOnTheSharedPath:
    def test_the_shared_runner_registers(self):
        assert "_registered(" in inspect.getsource(
            main._run_with_memory_pause)

    def test_it_names_the_job_by_what_the_run_actually_is(self):
        """A delta must not register as "migrate": the dashboard and the
        operator both read that name."""
        src = inspect.getsource(main._run_with_memory_pause)
        assert '"delta" if delta else "migrate"' in src

    def test_no_command_registers_on_its_own_any_more(self):
        """Two registrations for one run held two rows of a two-job cap.
        The shared path is now the only one."""
        for name in ("cmd_migrate", "cmd_delta"):
            src = inspect.getsource(getattr(main, name))
            assert "_registered(" not in src, (
                f"{name} registers separately; the shared runner already "
                "does it and two rows for one run exhaust the job cap")

    def test_both_commands_still_go_through_the_shared_runner(self):
        """The property the whole fix rests on."""
        for name in ("cmd_migrate", "cmd_delta"):
            assert "_run_with_memory_pause" in inspect.getsource(
                getattr(main, name))


class TestTheLedgerIsWhatStatusReads:
    def test_status_asks_the_admission_ledger(self):
        """Liveness has to come from the same table admission writes, or
        the two can disagree about whether a run exists."""
        import run_status

        assert "job_admission" in inspect.getsource(run_status._live_jobs)
