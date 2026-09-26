"""
tests/test_node_seed_directive.py
==================================
A node can be told to seed, not just migrate.

node_directives meant exactly one thing when it was built: "migrate this
tenant." A helper machine sitting idle while the coordinator's own box was
pinned at 95% CPU could not be given seed work instead, because the whole
pull-based mechanism (Nodes page writes desired state, node_agent.py polls
and acts) only knew how to build one command.

kind='migrate' is the default every existing directive keeps meaning. A
seed directive is validated with webui.seed_argv() -- the SAME function a
local seed uses -- before a single byte is written, so a bad request dies
in the request that made it, not silently on a machine an operator has no
log access to.
"""

from __future__ import annotations

import inspect
import json

import pytest

import node_agent


class TestTheDirectiveCarriesWhatKindOfWork:
    def test_migrate_is_still_the_default(self):
        """Every directive ever written, before this column existed, must
        keep meaning exactly what it always meant."""
        import control_plane_db as cpdb

        src = inspect.getsource(cpdb._apply_column_upgrades)
        assert "'migrate'" in src

    def test_a_seed_body_is_validated_before_it_is_written(self):
        """The same validator a LOCAL seed goes through -- domain_guard,
        typed-domain confirmation, scale, users, the worker ceiling -- so a
        bad request dies here, not silently on a machine with no log
        access."""
        import api_server

        src = inspect.getsource(api_server.set_node_directive)
        assert "webui.seed_argv(req.seed" in src
        assert "raise HTTPException(400, err)" in src

    def test_a_bad_kind_is_refused_outright(self):
        import api_server

        src = inspect.getsource(api_server.set_node_directive)
        assert "kind must be" in src


class TestTheNodeBuildsTheSameCommandALocalSeedWould:
    def test_start_seed_reuses_webuis_own_argv_builder(self):
        """One seed-request validator, not two copies that can drift."""
        src = inspect.getsource(node_agent.Agent.start_seed)
        assert "webui.seed_argv(body" in src

    def test_a_refused_body_raises_rather_than_launching_anything(self):
        agent = node_agent.Agent("http://x", "t", "n", 2)

        class FakeWebui:
            @staticmethod
            def seed_argv(body, account_id):
                return [], {}, "not a sandbox domain"

        import sys

        # Put back whatever was there, not delete it: a real webui already in
        # sys.modules that vanishes here makes every later `import webui` build
        # a second copy, and monkeypatches on the first never reach it.
        real = sys.modules.get("webui")
        sys.modules["webui"] = FakeWebui  # short-circuits the real import
        try:
            with pytest.raises(RuntimeError, match="not a sandbox"):
                agent.start_seed({"confirm_domain": "prod.example.com"})
        finally:
            if real is not None:
                sys.modules["webui"] = real
            else:
                del sys.modules["webui"]
        assert agent.proc is None

    def test_it_launches_from_the_data_generator_directory(self):
        """seed_sandbox.py writes identities.csv relative to its cwd; the
        wrong cwd is how a seed silently writes the wrong tenant's file."""
        src = inspect.getsource(node_agent.Agent.start_seed)
        assert '"data-generator"' in src


class TestTickDispatchesOnKind:
    def test_kind_seed_calls_start_seed_not_start(self, monkeypatch):
        agent = node_agent.Agent("http://x", "t", "n", 2)
        called = {}
        monkeypatch.setattr(agent, "directive", lambda: {
            "run": True, "kind": "seed",
            "seed": {"confirm_domain": "sandbox.example.com"}})
        monkeypatch.setattr(agent, "start_seed", lambda body: called.setdefault("seed", body))
        monkeypatch.setattr(agent, "start", lambda services: called.setdefault("migrate", services))
        state = agent.tick()
        assert "seed" in called and "migrate" not in called
        assert state == "started seed"

    def test_an_absent_kind_still_migrates(self, monkeypatch):
        """Backward compatibility with every directive row written before
        this column existed."""
        agent = node_agent.Agent("http://x", "t", "n", 2)
        called = {}
        monkeypatch.setattr(agent, "directive", lambda: {"run": True, "services": "drive"})
        monkeypatch.setattr(agent, "start", lambda services: called.setdefault("migrate", services))
        agent.tick()
        assert called.get("migrate") == "drive"

    def test_a_refusal_is_reported_as_state_not_raised(self, monkeypatch):
        agent = node_agent.Agent("http://x", "t", "n", 2)
        monkeypatch.setattr(agent, "directive", lambda: {
            "run": True, "kind": "seed", "seed": {}})
        monkeypatch.setattr(agent, "start_seed",
                            lambda body: (_ for _ in ()).throw(RuntimeError("no domain")))
        state = agent.tick()
        assert "could not start" in state
        assert agent.proc is None


class TestHeartbeatNamesTheRightJob:
    def test_a_running_seed_reports_seed_not_migrate(self):
        agent = node_agent.Agent("http://x", "t", "n", 2)
        agent.kind = "seed"
        agent.seed_domain = "sandbox.example.com"

        class FakeProc:
            pid = 4242
            def poll(self):
                return None

        agent.proc = FakeProc()
        sent = {}

        def fake_post(url, token, body, timeout=20.0):
            sent.update(body)

        import node_agent as na
        orig = na._post
        na._post = fake_post
        try:
            agent.heartbeat()
        finally:
            na._post = orig
        assert sent["active_job"] == "seed sandbox.example.com"
        assert sent["job_pid"] == 4242

    def test_an_idle_agent_still_reports_kind_migrate_by_default(self):
        agent = node_agent.Agent("http://x", "t", "n", 2)
        assert agent.kind == "migrate"


class TestTheApiRoundTripsBothFields:
    def test_get_directive_returns_kind_and_seed_body(self, monkeypatch):
        """The GET side node_agent.directive() actually reads."""
        import api_server

        class FakeConn:
            def execute(self, sql, params=()):
                class Row(dict):
                    def __getitem__(self, k):
                        return dict.get(self, k)
                if "node_directives" in sql:
                    return _one(Row(run=1, services="", kind="seed",
                                    seed_body=json.dumps({"confirm_domain": "s.example.com"}),
                                    updated_at="2026-09-21T00:00:00Z"))
                return _one(Row(takes_work=1))

        def _one(row):
            class R:
                def fetchone(self_):
                    return row
            return R()

        import contextlib

        @contextlib.contextmanager
        def fake_ro():
            yield FakeConn()

        monkeypatch.setattr(api_server.cpdb, "ro", fake_ro)
        import asyncio

        out = asyncio.run(api_server.get_node_directive(account_id=2, node_id=""))
        assert out["kind"] == "seed"
        assert out["seed"] == {"confirm_domain": "s.example.com"}
