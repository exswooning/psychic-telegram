"""A tenant admin's gcloud sign-in must not outlive its migration. Live (2026-10-04)
the box still held a service account of a project no account uses, and an admin of a
finished tenant left signed in under /tmp by a setup that died in August."""
import gcloud_signout
from tests.test_ui_for_ssh_ops import _signed_in
from tests.test_control_plane import cp  # noqa: F401 - the fixture


def test_every_leftover_is_revoked_and_deleted_and_the_default_revoked(tmp_path, monkeypatch):
    left = tmp_path / "cloudsdk-abc"
    left.mkdir()
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    monkeypatch.setattr(gcloud_signout, "AMBIENT", str(ambient))
    monkeypatch.setattr(gcloud_signout.tempfile, "gettempdir", lambda: str(tmp_path))
    signed_in = {str(left): ["old-admin@t.example"], str(ambient): ["stale-sa@p.iam"]}
    monkeypatch.setattr(gcloud_signout, "_accounts", lambda c: signed_in.get(c, []))
    revoked = []
    monkeypatch.setattr(gcloud_signout.subprocess, "run",
                        lambda argv, **k: revoked.append((argv[1:3], k["env"]["CLOUDSDK_CONFIG"])))
    assert gcloud_signout.sign_out_all() == ["old-admin@t.example", "stale-sa@p.iam"]
    assert not left.exists() and ambient.exists()
    assert (["auth", "revoke"], str(left)) in revoked and (["auth", "revoke"], str(ambient)) in revoked


class TestApprovingAMigrationSignsGcloudOut:
    def test_refused_while_a_setup_uses_a_sign_in(self, cp, monkeypatch):
        _signed_in(cp, "boss4@example.com", superadmin=True)
        monkeypatch.setattr(gcloud_signout, "busy", lambda: "python3 full_setup.py --side target")
        monkeypatch.setattr(gcloud_signout, "sign_out_all", lambda: (_ for _ in ()).throw(AssertionError("signed out")))
        r = cp.post("/api/v2/migrations/approve", json={"reason": "client done"})
        assert r.json()["ok"] is False and "full_setup" in r.json()["detail"]

    def test_approved_signs_out_and_says_whom(self, cp, monkeypatch):
        _signed_in(cp, "boss5@example.com", superadmin=True)
        monkeypatch.setattr(gcloud_signout, "busy", lambda: "")
        monkeypatch.setattr(gcloud_signout, "sign_out_all", lambda: ["old-admin@t.example"])
        r = cp.post("/api/v2/migrations/approve", json={"reason": "client done"})
        assert r.json()["ok"] is True and "old-admin@t.example" in r.json()["detail"]

    def test_only_a_superadmin(self, cp):
        _signed_in(cp, "plain2@example.com")
        assert cp.post("/api/v2/migrations/approve", json={"reason": "x done"}).status_code == 403
