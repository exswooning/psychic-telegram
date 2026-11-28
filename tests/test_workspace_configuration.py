"""Configuration evidence and target-only reconciliation, with no live API calls."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

import workspace_configuration as wc


class DB:
    def __init__(self):
        self.mapping = {"info@source.test": "nest@target.test"}
        self.saved = []

    def resolve_identity(self, email):
        return self.mapping.get(email)

    def identity_pairs(self):
        return [{"source_email": s, "target_email": t} for s, t in self.mapping.items()]

    def record_mapping(self, *args):
        self.saved.append(args)


class Fake(wc.ConfigurationCheck):
    def __init__(self, tmp_path):
        self.settings = SimpleNamespace(source_domain="source.test", target_domain="target.test",
                                        db_path=str(tmp_path / "migration.db"))
        self.db, self.group_map, self._clients = DB(), {}, {}
        self.data = {"source": [{"email": "team@source.test", "name": "Team", "description": "D",
                                 "aliases": ["all@source.test"]}],
                     "target": [{"email": "team@target.test", "name": "Wrong", "description": ""}]}
        self.member_data = {"source": [{"email": "info@source.test", "role": "OWNER"}],
                            "target": [{"email": "nest@target.test", "role": "MEMBER"}]}
        self.settings_data = {"source": {"whoCanPostMessage": "ALL_MEMBERS_CAN_POST", "archiveOnly": "false"},
                              "target": {"whoCanPostMessage": "ALL_MANAGERS_CAN_POST", "archiveOnly": "false"}}
        self.calls = []
        self.fail = set()

    def groups(self, side):
        if (side, "groups") in self.fail:
            raise ValueError("not authorized")
        return deepcopy(self.data[side])

    def members(self, side, email):
        if (side, "members") in self.fail:
            raise ValueError("membership denied")
        return deepcopy(self.member_data[side])

    def group_settings(self, side, email):
        if (side, "settings") in self.fail:
            raise ValueError("settings scope missing")
        return deepcopy(self.settings_data[side])

    def quota(self, side, email):
        if (side, "quota") in self.fail:
            raise ValueError("quota denied")
        return {"limit": None, "usage": "90071992547409930",
                "usageInDrive": "90071992547409931" if side == "source" else "90071992547409930",
                "usageInDriveTrash": "0"}

    def directory(self, side, write=False):
        assert side == "target" and write
        fake = self

        class Service:
            def groups(self): return self
            def members(self): return self
            def aliases(self): return self
            def insert(self, **kwargs):
                fake.calls.append(("insert", kwargs))
                return SimpleNamespace(execute=lambda: {})
            def patch(self, **kwargs):
                fake.calls.append(("patch", kwargs))
                return SimpleNamespace(execute=lambda: {})
        return Service()

    def client(self, side, api, version, scope, user=None, write=False):
        assert side == "target" and write
        fake = self

        class Service:
            def groups(self): return self
            def patch(self, **kwargs):
                fake.calls.append(("settings", kwargs))
                return SimpleNamespace(execute=lambda: {})
        return Service()


@pytest.fixture
def check(tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "report_path", lambda s: tmp_path / (s.target_domain + ".json"))
    monkeypatch.setattr(wc.domain_guard, "require_not_source", lambda *args: None)
    return Fake(tmp_path)


def test_plan_covers_metadata_alias_role_settings(check):
    g = check.snapshot()["groups"][0]
    assert g["status"] == "different" and not g["blocked"]
    assert {o["kind"] for o in g["operations"]} == {"metadata", "member", "alias", "settings"}
    assert next(o for o in g["operations"] if o["kind"] == "member")["email"] == "nest@target.test"
    patch = next(o for o in g["operations"] if o["kind"] == "settings")["body"]
    assert patch == check.settings_data["source"]  # coupled archive/posting fields
    assert not check.calls


def test_timestamps_do_not_make_every_review_stale(check):
    a, b = check.snapshot(), check.snapshot()
    assert a["checkedAt"] != b["checkedAt"]
    assert a["planHash"] == b["planHash"]


def test_exact_int64_strings_and_unknown_limit(check):
    row = check.snapshot()["storage"][0]
    assert row["sourceQuota"]["value"]["usageInDrive"] == "90071992547409931"
    assert row["sourceQuota"]["value"]["limit"] is None
    assert row["targetMinusSourceBytes"]["usageInDrive"] == "-1"


@pytest.mark.parametrize("part", ["groups", "members", "settings"])
def test_missing_access_never_passes(check, part):
    check.fail.add(("source", part))
    r = check.snapshot()
    if part == "groups":
        assert r["groupInventory"]["source"]["status"] == "unreadable"
    else:
        assert r["groups"][0]["status"] == "not_checked"
    wc.save_report(check.settings, r)
    with pytest.raises(ValueError):
        check.apply(r["planHash"])
    assert not check.calls


def test_unreadable_target_membership_does_not_plan_duplicate_inserts(check):
    check.fail.add(("target", "members"))
    row = check.snapshot()["groups"][0]
    assert row["blocked"]
    assert not any(o["kind"] == "member" for o in row["operations"])


def test_unmapped_external_group_does_not_get_invented(check):
    check.member_data["source"].append({"email": "outside@external.test", "type": "GROUP", "role": "OWNER"})
    row = check.snapshot()["groups"][0]
    assert any("outside@external.test" in b for b in row["blocked"])
    assert all(o.get("email") != "outside@target.test" for o in row["operations"])


def test_target_only_members_are_retained_and_reported(check):
    check.member_data["target"].append({"email": "existing@target.test", "role": "MEMBER"})
    row = check.snapshot()["groups"][0]
    assert any("Target-only" in b for b in row["blocked"])
    assert not any(o["kind"] in ("delete", "remove") for o in row["operations"])


def test_no_guessing_secondary_domain_alias_mapping(check):
    check.data["source"][0]["aliases"] = ["team@other.test"]
    row = check.snapshot()["groups"][0]
    assert any("secondary-domain" in b for b in row["blocked"])
    assert not any(o["kind"] == "alias" for o in row["operations"])


def test_colliding_localparts_block_apply(check):
    check.data["source"].append({"email": "team@secondary.test"})
    assert all(any("Multiple source" in b for b in g["blocked"]) for g in check.snapshot()["groups"])


def test_custom_reply_address_uses_identity_mapping(check):
    check.settings_data["source"]["customReplyTo"] = "info@source.test"
    ops = check.snapshot()["groups"][0]["operations"]
    assert next(o for o in ops if o["kind"] == "settings")["body"]["customReplyTo"] == "nest@target.test"


def test_stale_review_refuses_all_writes(check):
    r = check.snapshot()
    wc.save_report(check.settings, r)
    check.data["source"][0]["name"] = "Changed since review"
    with pytest.raises(ValueError, match="changed since review"):
        check.apply(r["planHash"])
    assert not check.calls


def test_apply_requires_saved_review_and_reads_back(check):
    with pytest.raises(ValueError, match="missing or has changed"):
        check.apply("a" * 64)
    r = check.snapshot()
    wc.save_report(check.settings, r)
    after = check.apply(r["planHash"])
    assert len(check.calls) == 4
    assert after["appliedPlanHash"] == r["planHash"]
    # Fake API doesn't change data: successful HTTP receipts must NOT clear drift.
    assert after["groups"][0]["status"] == "different"


def test_new_groups_created_before_nested_memberships(check, monkeypatch):
    check.data["target"] = []
    check.member_data["source"] = []
    check.data["source"].append({"email": "nested@source.test", "name": "Nested"})
    monkeypatch.setattr(wc.time, "sleep", lambda n: None)
    r = check.snapshot()
    wc.save_report(check.settings, r)
    check.apply(r["planHash"])
    assert "groupKey" not in check.calls[0][1]
    assert "groupKey" not in check.calls[1][1]


def test_quota_denied_is_unknown_not_zero(check):
    check.fail.add(("target", "quota"))
    row = check.snapshot()["storage"][0]
    assert row["targetQuota"]["status"] == "unreadable"
    assert row["targetMinusSourceBytes"] == {}


def test_storage_policy_never_claimed_automatically_copied(check):
    r = check.snapshot()
    assert r["storagePolicy"]["status"] == "manual_required"
    assert "cannot write storage policies" in r["storagePolicy"]["reason"]
    assert not check.calls


def test_read_only_credentials_cannot_be_used_for_source_writes(check):
    with pytest.raises(ValueError, match="target-only"):
        wc.ConfigurationCheck.client(check, "source", "groupssettings", "v1", wc.GROUP_SCOPE, write=True)


def test_report_for_old_domain_pair_is_not_reused(check):
    wc.save_report(check.settings, check.snapshot())
    check.settings.source_domain = "another.test"
    assert wc.load_report(check.settings) is None


def test_optional_scope_is_not_added_to_migration_baseline(settings):
    from config import source_scopes, target_scopes
    assert wc.GROUP_SCOPE not in source_scopes(settings)
    assert wc.GROUP_SCOPE not in target_scopes(settings)


def test_source_groups_settings_client_exposes_get_only():
    service = SimpleNamespace(groups=lambda: SimpleNamespace(get=lambda **kw: kw))
    protected = wc.ReadOnlyGroupSettings(service)
    assert protected.groups().get(groupUniqueId="x@source.test") == {"groupUniqueId": "x@source.test"}
    with pytest.raises(ValueError, match="read-only"):
        protected.groups().patch(groupUniqueId="x@source.test", body={})


def test_legacy_deprecated_group_fields_are_not_replayed():
    assert not set(wc.GROUP_FIELDS) & {"whoCanInvite", "whoCanAdd", "showInGroupDirectory", "allowGoogleCommunication", "maxMessageBytes"}


def test_target_full_scope_read_fallback_does_not_change_delegation(check, monkeypatch):
    from google.auth.exceptions import RefreshError
    scopes_seen = []
    def client(side, api, version, scope, **kwargs):
        scopes_seen.append(scope)
        if scope == wc.DIRECTORY_READ:
            raise RefreshError("unauthorized_client")
        return "read-only-request"
    monkeypatch.setattr(check, "client", client)
    assert check.read_with_scopes("target", "admin", "directory_v1",
        [wc.DIRECTORY_READ, wc.DIRECTORY_WRITE], lambda c: c) == "read-only-request"
    assert scopes_seen == [wc.DIRECTORY_READ, wc.DIRECTORY_WRITE]


def test_source_group_read_never_asks_for_directory_write(check, monkeypatch):
    from google.auth.exceptions import RefreshError
    seen = []
    def client(side, api, version, scope, **kwargs):
        seen.append(scope)
        raise RefreshError("unauthorized_client")
    monkeypatch.setattr(check, "client", client)
    with pytest.raises(RefreshError):
        wc.ConfigurationCheck.directory_list(check, "source", "groups", customer="my_customer")
    assert seen == [wc.DIRECTORY_READ]
