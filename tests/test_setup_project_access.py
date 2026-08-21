"""Setup must check that the admin can administer the key's project.

An uploaded service-account key carries no relationship to who owns the
project it came from. Live: a key for wsmig-src-96030 whose admin held no
IAM role on it at all. Every console-driven step then ran as an account that
could not open the page, and Chat could never be configured -- surfacing
three layers away as "could not find the app name field", a selector
complaint about a page that had never rendered.
"""
import json

import provision_gcp


class TestProjectOf:
    def test_it_reads_the_project_out_of_a_key(self, tmp_path):
        k = tmp_path / "sa.json"
        k.write_text(json.dumps({"project_id": "wsmig-src-96030",
                                 "client_id": "1234"}))
        assert provision_gcp.project_of(str(k)) == "wsmig-src-96030"

    def test_a_missing_file_is_empty_not_an_exception(self, tmp_path):
        """Setup asks this before it knows the key is usable, so it must not
        be able to raise the whole run down."""
        assert provision_gcp.project_of(str(tmp_path / "nope.json")) == ""

    def test_unparseable_json_is_empty(self, tmp_path):
        k = tmp_path / "sa.json"
        k.write_text("{not json")
        assert provision_gcp.project_of(str(k)) == ""

    def test_a_key_without_the_field_is_empty(self, tmp_path):
        k = tmp_path / "sa.json"
        k.write_text(json.dumps({"client_id": "1234"}))
        assert provision_gcp.project_of(str(k)) == ""


class TestTheProbeFailsClosed:
    """An unreachable project and an unanswerable check lead to the same
    place. Guessing "probably fine" is how the unusable key went unnoticed."""

    def test_a_login_failure_reads_as_no_access(self, monkeypatch):
        import full_setup
        monkeypatch.setattr(full_setup.gcloud_browser_auth, "login",
                            lambda *a, **k: (False, "bad password", ""))
        assert full_setup._admin_can_reach_project("p", "a@b.c", "pw") is False

    def test_an_exception_reads_as_no_access(self, monkeypatch):
        import full_setup
        def boom(*a, **k):
            raise RuntimeError("gcloud is not installed")
        monkeypatch.setattr(full_setup.gcloud_browser_auth, "login", boom)
        assert full_setup._admin_can_reach_project("p", "a@b.c", "pw") is False


class TestItWillNotReplaceWorkingCredentials:
    """Trading a live migration for one unlocked service is not a call this
    should make on anybody's behalf."""

    def test_a_key_with_no_client_id_is_not_treated_as_live(self):
        import full_setup
        assert full_setup._delegation_already_live(None, "source", "") is False

    def test_an_unanswerable_scope_check_is_not_treated_as_live(self,
                                                               monkeypatch):
        """Failing closed here means "repair it", which is the recoverable
        direction -- re-provisioning a broken setup, not replacing a working
        one on a bad reading."""
        import scope_guard
        def boom(*a, **k):
            raise RuntimeError("network down")
        monkeypatch.setattr(scope_guard, "is_complete", boom)
        import full_setup
        assert full_setup._delegation_already_live(
            object(), "source", "12345") is False
