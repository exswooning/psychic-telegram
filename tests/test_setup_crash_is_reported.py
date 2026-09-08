"""A setup that dies must say so, and a working seed must not be killed.

Live: a `huge` seed across 200 users passed full_setup's 2-hour subprocess
timeout while still working -- 26% CPU, 151 threads, all 200 users created --
and subprocess killed it. The TimeoutExpired escaped run_full_setup, so no
result was written: the state file still read {"running": true} with an empty
.partial beside it.

`running` is a ps scan, so it correctly reported no. The run then vanished
from the UI with no failure and no reason, while the traceback sat unread in
the .err next to it.
"""
import inspect

import full_setup


class TestASeedIsNotKilledByAClock:
    def test_the_wait_has_no_timeout(self):
        src = inspect.getsource(full_setup.run_full_setup)
        i = src.index('if seed and side == "source":')
        j = src.index("if provision_users", i)
        blk = "\n".join(l for l in src[i:j].splitlines()
                        if not l.lstrip().startswith("#"))
        assert "proc.wait()" in blk
        assert "timeout=7200" not in blk, (
            "a seed making progress was killed at two hours")

    def test_provisioning_keeps_its_timeout(self):
        """Removing one wall clock is not an argument for removing them all:
        provision-users is a short run whose output nobody watches."""
        src = inspect.getsource(full_setup.run_full_setup)
        i = src.index("if provision_users")
        assert "timeout=" in src[i:i + 1200]


class TestACrashedRunIsReported:
    def _read_src(self) -> str:
        import api_server
        return inspect.getsource(api_server.full_setup_status)

    def test_a_dead_run_with_no_result_synthesises_one(self):
        src = self._read_src()
        assert "crashed" in src
        assert 'not running and result is None' in src

    def test_it_reads_the_reason_out_of_the_error_log(self):
        src = self._read_src()
        assert "full-setup-{side}.err" in src
        assert '"Timeout" in x' in src, (
            "TimeoutExpired is the exact case this was written for")

    def test_no_evidence_means_no_verdict(self):
        """The first version of this synthesised a failure whenever a state
        file existed, which invented a crash for a tenant that had never
        been set up -- caught by
        test_status_reports_not_running_with_no_result_by_default.

        The marker {"running": true} also survives forever after a crash, so
        keying on that alone would report the same days-old failure on every
        later call. An exception line in the .err is both the evidence that
        something died and the thing worth telling the operator; without one
        the honest answer stays "no result"."""
        src = self._read_src()
        assert "if why:" in src, "a crash is reported without a reason again"
        i, j = src.index("if why:"), src.index('"crashed": True')
        assert i < j, "the evidence check must gate the verdict"

    def test_a_finished_run_is_untouched(self):
        """The synthesis is only for the no-result case; a real result must
        never be overwritten by it."""
        src = self._read_src()
        i = src.index("if not running and result is None")
        assert "result = {" in src[i:], "the guard must precede the write"
