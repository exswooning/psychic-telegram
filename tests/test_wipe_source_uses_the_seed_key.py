"""22 Nov 2026, a.: deleting a source's users asked one token for every read-only migrate scope plus
admin.directory.user -- no key holds both since separate_credentials, so it was refused on every source."""
import pytest

import wipe_target


def test_the_source_side_uses_the_seed_key_with_one_scope(monkeypatch):
    import reset_target, separate_credentials
    from google.oauth2 import service_account
    import googleapiclient.discovery as disc
    seen = {}
    monkeypatch.setattr(reset_target, "assert_sandbox", lambda *a: None)
    monkeypatch.setattr(separate_credentials, "seed_key_path", lambda s: "/keys/seed-sa.json")

    def creds(path, scopes, subject):
        seen.update(path=path, scopes=scopes, subject=subject)
        return object()
    monkeypatch.setattr(service_account.Credentials, "from_service_account_file", creds)
    monkeypatch.setattr(disc, "build", lambda *a, **k: "directory")

    def stop(directory, *a, **k):
        seen["directory"] = directory
        raise SystemExit(0)
    monkeypatch.setattr(wipe_target, "deletable_users", stop)
    monkeypatch.setenv("SOURCE_ADMIN", "info@a.example")
    with pytest.raises(SystemExit):
        wipe_target.main(["--side", "source", "--confirm-domain", "a.example"])
    assert seen["path"] == "/keys/seed-sa.json" and seen["directory"] == "directory"
    assert seen["scopes"] == ["https://www.googleapis.com/auth/admin.directory.user"]
