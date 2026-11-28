"""Seed imports must not let data-generator/verify.py shadow the guarded module."""
from __future__ import annotations

import importlib
import os
import sys

from seed_loader import load_seed_sandbox


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN_DIR = os.path.join(ROOT, "data-generator")


def _generator_paths():
    return [path for path in sys.path if os.path.abspath(path or os.curdir) == GEN_DIR]


def test_loading_seeder_does_not_leave_data_generator_on_sys_path():
    before = _generator_paths()
    load_seed_sandbox()
    assert _generator_paths() == before


def test_loading_seeder_does_not_shadow_the_guarded_verify_module():
    old_path = list(sys.path)
    old_verify = sys.modules.pop("verify", None)
    try:
        sys.path[:] = [path for path in old_path
                       if os.path.abspath(path or os.curdir) != GEN_DIR]
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        load_seed_sandbox()
        verify = importlib.import_module("verify")
        assert os.path.abspath(os.path.dirname(verify.__file__)) != GEN_DIR
        assert verify.UserReport(source="a", target="b").ok is False
    finally:
        sys.path[:] = old_path
        sys.modules.pop("verify", None)
        if old_verify is not None:
            sys.modules["verify"] = old_verify


def test_scope_and_campaign_seed_imports_do_not_add_generator_path(monkeypatch):
    import campaign_changes
    import separate_credentials
    import verify_scopes

    gen_before = _generator_paths()
    separate_credentials.seed_scopes()
    verify_scopes.required_scopes(__import__("config").Settings(), "source", include_seed=True)
    verify_scopes._seed_scopes()

    class SeederStub:
        @staticmethod
        def _use_own_key(settings):
            pass

        @staticmethod
        def _retry_factory(settings):
            return "retry"

    monkeypatch.setattr(campaign_changes, "load_seed_sandbox", lambda: SeederStub)
    assert campaign_changes._seed_services(__import__("config").Settings(), "u@example.test")[1] == "retry"
    assert _generator_paths() == gen_before
