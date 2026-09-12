"""Start and stop a node's work without reaching into the machine.

Doing it meant SSH-ing in and running main.py, and the Nodes page could not
offer a button because the design points the other way: nodes connect
OUTWARD and nothing reaches into one. fleet_agent.py gives the reason -- a
control plane that could reach into its nodes would need credentials for
every machine holding service-account keys for both tenants, which turns a
dashboard into a lateral-movement path across the whole migration.

So the button writes desired state and each node asks. Verified end to end
against a running server: directive off, superadmin turns it on, a node
holding the token sees it, one without gets 401, the agent starts a child
with the right argv, and flipping it back sends SIGINT -- the child exited
with -2, and main.py handles SIGINT by finishing the user it is on rather
than stranding a mailbox half-copied.
"""
from __future__ import annotations

import inspect
import os
import tempfile

import pytest

import control_plane_db as cpdb
import node_agent
from db import MigrationDB


def _code(fn) -> str:
    """Source with comments and docstrings stripped.

    These functions explain at length WHY SIGKILL is wrong, so an "is it
    absent" assertion read against the raw text finds the explanation and
    fails -- a test bug that pushes someone to delete the reasoning.
    """
    import re
    src = inspect.getsource(fn)
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    return re.sub(r"#.*$", "", src, flags=re.M)


@pytest.fixture
def db(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


class TestTheCoordinatorNeverReachesIn:
    def test_the_node_route_asks_rather_than_being_told(self):
        import api_server
        src = _code(api_server.get_node_directive)
        assert "node_auth" in str(inspect.signature(api_server.get_node_directive))
        assert "urllib" not in src and "requests" not in src

    def test_setting_it_only_writes(self):
        """No outbound call anywhere in the handler: it records an
        intention, and that is the whole mechanism."""
        import api_server
        src = _code(api_server.set_node_directive)
        assert "urlopen" not in src
        assert "ssh" not in src.lower()

    def test_turning_it_on_is_audited(self):
        """This starts writes against a live tenant from machines that are
        not this one. "Who turned this on" is the first question after a
        surprise."""
        import api_server
        src = inspect.getsource(api_server.set_node_directive)
        assert "cpdb.begin_action" in src
        assert "start node work" in src

    def test_only_a_superadmin_may_set_it(self):
        import api_server
        src = inspect.getsource(api_server.set_node_directive)
        assert "require_superadmin(op)" in src
        assert "_require_account_access(account_id, op)" in src


class TestTheAgentActsOnWhatItIsTold:
    def test_stopping_is_cooperative(self):
        """SIGINT, never SIGKILL. main.py finishes the user it is on;
        killing it strands a mailbox half-copied and, on Drive, leaves items
        the local ledger never recorded -- the one thing a resume cannot
        recover from."""
        src = _code(node_agent.Agent.stop)
        assert "SIGINT" in src
        assert "SIGKILL" not in src
        assert "kill()" not in src

    def test_it_builds_the_command_from_the_directive(self):
        src = inspect.getsource(node_agent.Agent.start)
        assert '"--account-id"' in src
        assert '"migrate"' in src
        assert '"--services"' in src

    def test_a_heartbeat_failure_does_not_stop_a_migration(self):
        """Being unable to tell the coordinator how it is doing is not a
        reason to stop. The claim calls are the ones that must not be
        guessed at, and main.py already halts on its own if those fail."""
        src = _code(node_agent.Agent.heartbeat)
        assert "except Exception" in src
        assert "raise" not in src

    def test_an_unreachable_coordinator_changes_nothing(self):
        """It must not read "cannot ask" as "stop"."""
        agent = node_agent.Agent("http://127.0.0.1:9", "t", "n", 1)
        state = agent.tick()
        assert "unreachable" in state
        assert agent.proc is None

    def test_the_child_outlives_an_agent_restart_where_it_can(self):
        src = inspect.getsource(node_agent.Agent.start)
        assert "start_new_session" in src
        # Guarded, because Windows has no setsid and ignoring that silently
        # is how a migration there dies with its parent unannounced.
        assert 'hasattr(os, "setsid")' in src

    def test_it_refuses_to_run_without_being_told_which_tenant(self):
        src = inspect.getsource(node_agent.main)
        assert "BITPORT_ACCOUNT" in src
        assert "has to be told which" in src


class TestConnectingFromTheBrowser:
    def test_it_is_an_outbound_call_by_this_machines_own_admin(self):
        import api_server
        src = inspect.getsource(api_server.connect_to_coordinator)
        assert "require_superadmin(op)" in src
        assert "urlopen" in src          # outbound, the same call the installer makes

    def test_it_accepts_a_pasted_command_line(self):
        """The line is on the clipboard already; splitting a URL out of it
        by hand is the step that gets done wrong once and blamed on the
        tool."""
        import api_server
        src = inspect.getsource(api_server.connect_to_coordinator)
        assert "/api/v2/j/" in src
        assert "re.search" in src

    def test_the_token_lands_in_a_mode_600_file(self):
        import api_server
        src = inspect.getsource(api_server.connect_to_coordinator)
        assert "0o600" in src

    def test_a_refusal_is_reported_not_swallowed(self):
        import api_server
        src = inspect.getsource(api_server.connect_to_coordinator)
        assert "refused that code" in src
        assert "cannot reach" in src


class TestTheDirectiveNamesNoServices:
    """The switch had a Services box defaulting to "gmail".

    Two things wrong with that. main.py's --services already defaults to
    "all" -- everything the tenant has configured -- so the box overrode a
    tenant-level setting from a page that is not where that setting lives.
    And gmail is the wrong default wherever Google's own Data Migration
    Service is handling the mail, which is this deployment's documented
    arrangement (see Services.tsx's own DMS panel).

    Chunking needed no setting either: user_claims already hands out users
    one at a time, which is what stops two machines starting the same
    mailbox.
    """

    def test_the_engine_default_is_everything(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "main.py"), encoding="utf-8").read()
        assert 's.add_argument("--services", default="all"' in src

    def test_the_agent_passes_no_services_when_none_are_given(self):
        src = _code(node_agent.Agent.start)
        # Only appended when the directive actually carries one, so the
        # engine's own default survives.
        assert 'if services:' in src
        assert src.index("argv = [") < src.index("if services:")


class TestJoiningAlsoStartsTheAgent:
    """A node that joined but has no agent is invisible and inert.

    Live: a laptop sat "offline, 7h ago" on the Machines list. Nothing was
    broken -- it had joined, written node.env, and proved it could reach the
    coordinator. Joining and running were simply two steps and only the
    first had happened, with nothing anywhere saying so. Pressing Start did
    nothing to it either, silently.
    """

    def test_connect_starts_it(self):
        import api_server
        src = _code(api_server.connect_to_coordinator)
        assert "_start_node_agent()" in src

    def test_it_outlives_the_request_that_launched_it(self):
        """Without start_new_session the agent dies with the worker that
        served the click, and the node goes quiet for a reason nobody would
        connect to a page they clicked minutes earlier."""
        import api_server
        src = _code(api_server._start_node_agent)
        assert "start_new_session" in src
        assert 'hasattr(os, "setsid")' in src

    def test_it_does_not_start_a_second_one(self):
        """Two agents double every poll and race each other to launch the
        same migration."""
        import api_server
        src = _code(api_server._start_node_agent)
        assert "pgrep" in src
        assert "already running" in src

    def test_a_failure_is_reported_rather_than_shown_as_success(self):
        import api_server
        src = _code(api_server._start_node_agent)
        assert '"started": False' in src

    def test_boot_persistence_is_separate_from_starting(self):
        """Order matters: start it now so the machine is useful, then try to
        make that survive a reboot. Failing the second is not failing the
        first."""
        import api_server
        src = _code(api_server._start_node_agent)
        assert src.index("subprocess.Popen") < src.index("systemctl")
        assert src.rstrip().endswith('return {"started": True, "detail": detail}')


class TestTheAgentSurvivesItsOwnFailures:
    """An exception escaping tick() escapes the loop in main() too.

    The agent then exits, and the node is silently offline with no process
    left to say why -- indistinguishable from a sleeping laptop, and
    permanent. Anything that can fail once (a bad path, a full disk, a
    permission) would otherwise end that machine's participation for good.
    """

    def _agent(self, **over):
        class A(node_agent.Agent):
            pass
        for name, fn in over.items():
            setattr(A, name, fn)
        return A("http://127.0.0.1:9", "t", "n", 1)

    def test_a_failed_start_costs_one_cycle_not_the_agent(self):
        def boom(self, services):
            raise OSError("no such python")
        a = self._agent(directive=lambda self: {"run": True, "services": ""},
                        start=boom)
        assert "could not start" in a.tick()
        assert "could not start" in a.tick()      # still looping

    def test_a_failed_stop_does_not_end_it_either(self):
        class P:
            pid = 1

            def poll(self):
                return None
        def boom(self):
            raise OSError("process vanished")
        a = self._agent(directive=lambda self: {"run": False}, stop=boom)
        a.proc = P()
        assert "could not stop" in a.tick()

    def test_an_unreachable_coordinator_is_just_a_state(self):
        """A laptop coming out of standby finds its in-flight request dead.
        That is one cycle, not the end of the run."""
        a = node_agent.Agent("http://127.0.0.1:9", "t", "n", 1)
        assert "unreachable" in a.tick()
        assert "unreachable" in a.tick()

    def test_every_branch_of_tick_returns_a_string(self):
        """main() prints whatever tick returns; a None would read as a
        blank line in the one log that says what the agent is doing."""
        src = _code(node_agent.Agent.tick)
        assert src.count("return ") >= 6


class TestPickingWhichMachineRuns:
    """One directive per tenant meant every node acted on it.

    "Can I select what device runs migration" -- no, until now. Excluding a
    laptop meant stopping the whole tenant, or stopping its agent by hand on
    the machine itself.
    """

    def test_the_node_says_who_it_is(self):
        src = _code(node_agent.Agent.directive)
        assert "node_id=" in src

    def test_the_answer_ands_the_two_switches(self):
        import api_server
        src = _code(api_server.get_node_directive)
        assert "run and takes" in src

    def test_an_excluded_node_can_tell_that_apart_from_a_stopped_tenant(self):
        """Both give run=false, and they are completely different
        situations -- one is "you are sitting out", the other is "nobody is
        working". The node's own log should not conflate them."""
        import api_server
        src = _code(api_server.get_node_directive)
        assert '"tenantRun"' in src
        assert '"takesWork"' in src

    def test_a_machine_that_never_checked_in_still_works(self):
        """Defaulting an unknown node to idle would make a first poll look
        exactly like a broken install."""
        import api_server
        src = _code(api_server.get_node_directive)
        assert "takes = bool(n[\"takes_work\"]) if n else True" in src

    def test_the_column_defaults_to_taking_work(self):
        """Every node that joined before this column existed was already
        working; upgrading must not silently idle a fleet."""
        import control_plane_db as db
        src = open(db.__file__, encoding="utf-8").read()
        assert '("takes_work", "INTEGER NOT NULL DEFAULT 1")' in src

    def test_excluding_an_unknown_machine_is_an_error(self):
        """Not a silent no-op: a typo would otherwise read as success and
        the machine would keep working."""
        import api_server
        src = _code(api_server.set_node_takes_work)
        assert "cur.rowcount == 0" in src
        assert "404" in src

    def test_both_reads_share_one_connection(self):
        """The second sat outside the `with` once, which every caller saw as
        a 500 -- caught live rather than in review."""
        import api_server
        src = _code(api_server.get_node_directive)
        body = src[src.index("with cpdb.ro()"):src.index("return {")]
        assert body.count("conn.execute") == 2


class TestNodesReportWhatTheyAre:
    """cpu_pct/ram_pct/disk_pct were stored from the start and say "78% of
    something" without ever saying of what. The denominator is the part an
    operator deciding where to put work actually needs.
    """

    def test_specs_are_measured_once_not_every_poll(self):
        """Cores and RAM do not change between polls; re-probing every 20
        seconds spends real work to re-learn the same answer."""
        src = _code(node_agent.Agent.specs)
        assert "self._specs is not None" in src
        assert "self._specs = out" in src

    def test_load_is_measured_every_time(self):
        """The opposite property, and the reason they are separate methods:
        these change constantly."""
        src = _code(node_agent.Agent.load)
        assert "_specs" not in src

    def test_a_failed_reading_is_omitted_rather_than_zeroed(self):
        """upsert_node leaves a stored value alone for None. 0.0 would read
        as "idle" on a node that is flat out -- and Windows genuinely has no
        getloadavg, so cpu_pct is null there rather than wrong."""
        src = _code(node_agent.Agent.load)
        assert src.count("except Exception") == 3
        assert "= 0" not in src.replace("[0]", "")

    def test_disk_has_a_windows_path(self):
        """os.statvfs does not exist there; shutil.disk_usage does."""
        src = _code(node_agent.Agent.specs)
        assert "statvfs" in src and "shutil.disk_usage" in src

    def test_specs_never_break_the_heartbeat(self):
        """A missing heartbeat reads as a dead node, which is a far worse
        report than a missing core count."""
        src = _code(node_agent.Agent.heartbeat)
        assert "except Exception" in src
        beat = _code(node_agent.Agent.specs)
        assert "except Exception" in beat

    def test_the_heartbeat_model_accepts_them_all_optionally(self):
        import api_server
        fields = api_server.Heartbeat.model_fields
        for name in ("cpu_cores", "ram_gb", "disk_gb", "platform"):
            assert name in fields, name
            assert not fields[name].is_required(), name
