"""
tests/test_teardown_tenant.py
==============================
The reverse of full_setup.py: revoke a DWD entry, delete a project. Two
properties matter most: either half can run alone (a prior partial
cleanup left only one side to finish), and the password never survives
past the one call that needs it.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import teardown_tenant as tt  # noqa: E402


class TestRevokeOnly:
    def test_revoke_success_is_ok(self, monkeypatch):
        monkeypatch.setattr(tt.dwd_helper, "revoke", lambda *a, **k: 0)
        result = tt.run_teardown("", "42", "admin@example.com", "pw")
        assert result["ok"] is True
        assert result["phases"][0]["status"] == "ok"

    def test_revoke_nonzero_is_failed(self, monkeypatch):
        monkeypatch.setattr(tt.dwd_helper, "revoke", lambda *a, **k: 3)
        result = tt.run_teardown("", "42", "admin@example.com", "pw")
        assert result["ok"] is False
        assert result["phases"][0]["status"] == "failed"

    def test_revoke_crash_is_recovered_not_fatal(self, monkeypatch):
        """Same crash-recovery discipline as full_setup.py's own DWD call --
        an uncaught exception from ~450 lines of browser choreography must
        not take down the whole subprocess."""
        def boom(*a, **k):
            raise RuntimeError("dialog detached mid-click")
        monkeypatch.setattr(tt.dwd_helper, "revoke", boom)
        result = tt.run_teardown("", "42", "admin@example.com", "pw")
        assert result["ok"] is False
        assert "detached" in result["phases"][0]["detail"]

    def test_no_project_means_no_gcloud_signin_attempted(self, monkeypatch):
        called = {"login": False}
        monkeypatch.setattr(tt.dwd_helper, "revoke", lambda *a, **k: 0)
        monkeypatch.setattr(tt.gcloud_browser_auth, "login",
                            lambda *a, **k: called.update(login=True) or (True, "", ""))
        tt.run_teardown("", "42", "admin@example.com", "pw")
        assert called["login"] is False


class TestDeleteProjectOnly:
    def test_delete_success_is_ok(self, monkeypatch):
        monkeypatch.setattr(tt.gcloud_browser_auth, "login",
                            lambda *a, **k: (True, "signed in", "/tmp/cloudsdk-x"))
        monkeypatch.setattr(tt.gcloud_browser_auth, "cleanup", lambda cfg: None)
        monkeypatch.setattr(tt.provision_gcp, "delete_project",
                            lambda project, env=None: (True, f"{project} deleted"))
        result = tt.run_teardown("wsmig-src-1", "", "admin@example.com", "pw")
        assert result["ok"] is True
        assert "deleted" in result["phases"][0]["detail"]

    def test_gcloud_signin_failure_is_reported(self, monkeypatch):
        monkeypatch.setattr(tt.gcloud_browser_auth, "login",
                            lambda *a, **k: (False, "captcha", ""))
        result = tt.run_teardown("wsmig-src-1", "", "admin@example.com", "pw")
        assert result["ok"] is False
        assert "captcha" in result["phases"][0]["detail"]

    def test_ephemeral_cloudsdk_config_is_always_cleaned_up(self, monkeypatch):
        cleaned = {}
        monkeypatch.setattr(tt.gcloud_browser_auth, "login",
                            lambda *a, **k: (True, "ok", "/tmp/cloudsdk-x"))
        monkeypatch.setattr(tt.gcloud_browser_auth, "cleanup",
                            lambda cfg: cleaned.setdefault("path", cfg))
        monkeypatch.setattr(tt.provision_gcp, "delete_project",
                            lambda project, env=None: (True, "deleted"))
        tt.run_teardown("wsmig-src-1", "", "admin@example.com", "pw")
        assert cleaned["path"] == "/tmp/cloudsdk-x"

    def test_cleanup_runs_even_if_delete_project_raises(self, monkeypatch):
        monkeypatch.setattr(tt.gcloud_browser_auth, "login",
                            lambda *a, **k: (True, "ok", "/tmp/cloudsdk-x"))
        cleaned = {"done": False}
        monkeypatch.setattr(tt.gcloud_browser_auth, "cleanup",
                            lambda cfg: cleaned.__setitem__("done", True))

        def boom(project, env=None):
            raise RuntimeError("gcloud not found")
        monkeypatch.setattr(tt.provision_gcp, "delete_project", boom)

        try:
            tt.run_teardown("wsmig-src-1", "", "admin@example.com", "pw")
        except RuntimeError:
            pass
        assert cleaned["done"] is True


class TestBothHalves:
    def test_both_succeed_is_ok(self, monkeypatch):
        monkeypatch.setattr(tt.dwd_helper, "revoke", lambda *a, **k: 0)
        monkeypatch.setattr(tt.gcloud_browser_auth, "login",
                            lambda *a, **k: (True, "ok", "/tmp/cloudsdk-x"))
        monkeypatch.setattr(tt.gcloud_browser_auth, "cleanup", lambda cfg: None)
        monkeypatch.setattr(tt.provision_gcp, "delete_project",
                            lambda project, env=None: (True, "deleted"))
        result = tt.run_teardown("wsmig-src-1", "42", "admin@example.com", "pw")
        assert result["ok"] is True
        assert len(result["phases"]) == 2

    def test_one_failing_makes_the_whole_result_not_ok(self, monkeypatch):
        monkeypatch.setattr(tt.dwd_helper, "revoke", lambda *a, **k: 0)
        monkeypatch.setattr(tt.gcloud_browser_auth, "login",
                            lambda *a, **k: (False, "no network", ""))
        result = tt.run_teardown("wsmig-src-1", "42", "admin@example.com", "pw")
        assert result["ok"] is False
        assert result["phases"][0]["status"] == "ok"
        assert result["phases"][1]["status"] == "failed"


class TestNothingToDo:
    def test_neither_project_nor_client_id_reports_not_ok(self):
        """run_teardown() itself, not just main()'s argparse, must never
        silently report success for a call that did nothing."""
        result = tt.run_teardown("", "", "admin@example.com", "pw")
        assert result["ok"] is False
        assert result["phases"] == []


class TestMainRefusesEmptyArgs:
    def test_neither_project_nor_client_id_is_a_usage_error(self):
        import pytest
        with pytest.raises(SystemExit):
            tt.main(["--admin", "admin@example.com"])


class TestProgressFile:
    def test_progress_checkpoints_are_written(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tt.dwd_helper, "revoke", lambda *a, **k: 0)
        monkeypatch.setattr(tt.gcloud_browser_auth, "login",
                            lambda *a, **k: (True, "ok", "/tmp/cloudsdk-x"))
        monkeypatch.setattr(tt.gcloud_browser_auth, "cleanup", lambda cfg: None)
        monkeypatch.setattr(tt.provision_gcp, "delete_project",
                            lambda project, env=None: (True, "deleted"))
        progress_file = str(tmp_path / "progress.json")
        tt.run_teardown("wsmig-src-1", "42", "admin@example.com", "pw",
                        progress_file=progress_file)
        import json
        with open(progress_file, encoding="utf-8") as fh:
            final = json.load(fh)
        assert final == {"pct": 100, "label": "done"}

    def test_a_failed_progress_write_never_fails_the_run(self, monkeypatch):
        """_progress() is best-effort by design -- an unwritable path must
        not take down a teardown that otherwise succeeded."""
        monkeypatch.setattr(tt.dwd_helper, "revoke", lambda *a, **k: 0)
        result = tt.run_teardown("", "42", "admin@example.com", "pw",
                                 progress_file="/nonexistent-dir/progress.json")
        assert result["ok"] is True


class TestPasswordNeverLeaks:
    def test_password_is_not_a_module_global_after_the_call(self, monkeypatch):
        monkeypatch.setattr(tt.gcloud_browser_auth, "login",
                            lambda *a, **k: (True, "ok", "/tmp/cloudsdk-x"))
        monkeypatch.setattr(tt.gcloud_browser_auth, "cleanup", lambda cfg: None)
        monkeypatch.setattr(tt.provision_gcp, "delete_project",
                            lambda project, env=None: (True, "deleted"))
        tt.run_teardown("wsmig-src-1", "", "admin@example.com", "super-secret-pw")
        assert "super-secret-pw" not in os.environ.values()
