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
