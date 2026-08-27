"""Quick Setup adopted the box's leftover service account and failed.

Pressed live, "Set up source" reported:

    ERROR: (gcloud.projects.create) PERMISSION_DENIED:
    Service accounts cannot create projects

full_setup asks provision_gcp.gcloud_ready() whether to run its own
browser sign-in, and gcloud_ready answers "is anybody signed in". On a box
that has already run a migration the answer is yes -- the previous
tenant's source-sa@... service account is still the active gcloud
identity. So the sign-in that was supposed to happen was skipped, the
service account was used instead, and Google refused on the first real
call. The operator saw a permission error naming their admin account,
which had nothing to do with it.
"""
import provision_gcp


class TestWhoCanCreateAProject:
    def test_a_service_account_cannot(self):
        assert not provision_gcp.can_create_projects(
            "source-sa@wsmig-src-96030.iam.gserviceaccount.com")

    def test_a_person_can(self):
        assert provision_gcp.can_create_projects("info@source.rohitrokaya.com.np")

    def test_nobody_signed_in_cannot(self):
        assert not provision_gcp.can_create_projects("")
        assert not provision_gcp.can_create_projects(None)

    def test_the_check_is_case_insensitive(self):
        # gcloud prints these lowercase, but nothing guarantees it.
        assert not provision_gcp.can_create_projects(
            "SA@PROJ.IAM.GSERVICEACCOUNT.COM")

    def test_a_lookalike_domain_is_not_treated_as_one(self):
        # Only the real suffix; a user account may legitimately contain it.
        assert provision_gcp.can_create_projects(
            "iam.gserviceaccount.com.attacker@example.com")


class TestTheSetupSignsInInstead:
    def test_full_setup_treats_a_service_account_as_not_ready(self):
        """The point of the fix: it is not a reason to skip the sign-in, it
        is the reason to do it."""
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "full_setup.py"), encoding="utf-8").read()
        block = src.split("gcloud_ready()")[1][:600]
        assert "can_create_projects" in block
        assert "ready = False" in block

    def test_provision_refuses_with_a_message_that_names_the_fix(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "provision_gcp.py"), encoding="utf-8").read()
        block = src.split("def provision(")[1].split(chr(10) + "def ")[0]
        assert "can_create_projects" in block

    def test_provision_returns_the_refusal_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(provision_gcp, "gcloud_ready",
                            lambda env=None: (True, "sa@p.iam.gserviceaccount.com"))
        out = provision_gcp.provision("s.example", "t.example")
        assert out["ok"] is False
        assert "service account" in out["error"]
        # names the identity AND what to do about it -- the live failure
        # said only "PERMISSION_DENIED", which reads as the admin's fault.
        assert "sa@p.iam.gserviceaccount.com" in out["error"]
        assert "gcloud auth login" in out["error"]
        assert out["sides"] == []

    def test_a_real_user_still_gets_through_the_check(self, monkeypatch):
        # Guard against the check refusing everyone: it must only stop the
        # identity Google itself stops.
        seen = {}
        monkeypatch.setattr(provision_gcp, "gcloud_ready",
                            lambda env=None: (True, "admin@example.com"))
        monkeypatch.setattr(provision_gcp, "detect_org",
                            lambda env=None: seen.setdefault("got_past", "1") or "")
        monkeypatch.setattr(provision_gcp, "provision_side",
                            lambda *a, **k: {"ok": True, "steps": []})
        provision_gcp.provision("s.example", "t.example", dry_run=True)
        assert seen.get("got_past") == "1"
