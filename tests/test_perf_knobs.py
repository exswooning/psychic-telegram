"""The perf plan's knobs that are not StartMigration fields: discovery's own
worker count, and the benchmark no longer refusing drive_file_workers > 4."""
import types

import pytest

import main
import webui


class TestDiscoveryHasItsOwnWorkerCount:
    """Discovery is read-bound and used to borrow the migration's write-bound count."""

    def test_unset_it_is_the_migrations_count(self, monkeypatch):
        monkeypatch.delenv("DISCOVERY_WORKERS", raising=False)
        assert main._discovery_workers(types.SimpleNamespace(user_workers=16)) == 16

    def test_set_it_wins(self, monkeypatch):
        monkeypatch.setenv("DISCOVERY_WORKERS", "64")
        assert main._discovery_workers(types.SimpleNamespace(user_workers=16)) == 64

    def test_the_action_passes_it_through(self):
        env, err = webui._discover_env({"workers": "32"}, {"PATH": "/bin"})
        assert err is None and env == {"PATH": "/bin", "DISCOVERY_WORKERS": "32"}

    def test_blank_changes_nothing(self):
        assert webui._discover_env({"workers": ""}, {"A": "1"}) == ({"A": "1"}, None)

    @pytest.mark.parametrize("bad", [0, 65, "lots"])
    def test_out_of_range_is_refused(self, bad):
        env, err = webui._discover_env({"workers": bad}, {})
        assert err and "DISCOVERY_WORKERS" not in env
