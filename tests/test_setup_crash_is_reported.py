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
import json
import os
import tempfile

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


class TestAnInterruptedRunIsReconciledAtStartup:
    """A run killed by an API restart (every deploy does this) leaves
    {"running": true} and an EMPTY .err -- a clean kill writes no traceback.

    full_setup_status only synthesises a crash when it finds an exception
    line in the .err, and deliberately refuses to report on the marker alone
    (it survives forever and would re-report the same failure on every
    call). So a cleanly-killed run left the wizard in limbo: no result, no
    progress, no way forward. This reconciler converts the marker ONCE at
    startup into a dated interrupted result, the same way inventory scans
    are reconciled.
    """
    def _run(self, monkeypatch, state: dict, subdir: str = ""):
        import api_server
        d = tempfile.mkdtemp()
        logs = os.path.join(d, "logs", subdir) if subdir else os.path.join(d, "logs")
        os.makedirs(logs)
        path = os.path.join(logs, "full-setup-source.json")
        with open(path, "w") as fh:
            json.dump(state, fh)
        monkeypatch.setattr(api_server, "HERE", d)
        api_server._reconcile_full_setup_state()
        with open(path) as fh:
            return json.load(fh)

    def test_a_stuck_running_marker_becomes_an_interrupted_result(self, monkeypatch):
        out = self._run(monkeypatch, {"running": True})
        assert out["running"] is False
        assert out["interrupted"] is True
        assert "phases" in out, "must read as a result, not a bare marker"
        assert "interrupted" in out["error"].lower()

    def test_it_says_nothing_was_changed_that_a_rerun_will_not_redo(self, monkeypatch):
        """The operator has to know it is safe to just start again."""
        out = self._run(monkeypatch, {"running": True})
        assert "start it again" in out["error"].lower()

    def test_a_finished_result_is_left_untouched(self, monkeypatch):
        """It has phases and no running marker -- not an orphan."""
        done = {"running": False, "ok": True,
                "phases": [{"name": "setup (source)", "status": "ok"}]}
        assert self._run(monkeypatch, done) == done

    def test_a_never_set_up_tenant_is_left_untouched(self, monkeypatch):
        """No running marker means no run began -- inventing a failure here
        is the exact bug the runtime path was careful to avoid."""
        idle = {"running": False}
        assert self._run(monkeypatch, idle) == idle

    def test_a_per_account_run_is_reconciled_too(self, monkeypatch):
        """Runs started for a specific account land in
        logs/<account_id>/full-setup-source.json, not the top level. A flat
        scan caught only the legacy X-Operator path and left every
        account-scoped wizard stuck -- which is the view a signed-in user
        actually sees."""
        out = self._run(monkeypatch, {"running": True}, subdir="66")
        assert out["interrupted"] is True and out["running"] is False

    def test_it_is_wired_into_startup(self):
        import api_server
        import inspect
        src = inspect.getsource(api_server.lifespan) if hasattr(
            api_server, "lifespan") else ""
        # The reconcilers are invoked together in the startup block.
        whole = inspect.getsource(api_server)
        assert "_off_loop(_reconcile_full_setup_state)" in whole
