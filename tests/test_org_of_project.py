"""When `organizations list` gives no single org, the org a project sits in comes from
its ancestry, so the orgpolicy self-grant runs (live: skipped as "no organization id
known", the key-policy override then denied, key creation blocked)."""
import provision_gcp


def test_the_org_comes_from_the_project_ancestry(monkeypatch):
    monkeypatch.setattr(provision_gcp, "run", lambda argv, **k:
                        (0, "wsmig-src-87384 project\n1234567890 organization\n"))
    assert provision_gcp._org_of_project("wsmig-src-87384") == "1234567890"


def test_no_org_in_the_ancestry_is_empty(monkeypatch):
    monkeypatch.setattr(provision_gcp, "run", lambda argv, **k: (0, "p project\n"))
    assert provision_gcp._org_of_project("p") == ""


def test_relax_key_policy_self_grants_on_the_ancestry_org(monkeypatch):
    seen = {}
    monkeypatch.setattr(provision_gcp, "_org_of_project", lambda p, env=None: "999")
    monkeypatch.setattr(provision_gcp, "_self_grant_orgpolicy_admin",
                        lambda org, steps, dry_run, env=None: seen.setdefault("org", org))
    monkeypatch.setattr(provision_gcp, "run", lambda argv, **k: (0, ""))
    provision_gcp.relax_key_policy("wsmig-src-1", [], dry_run=False, org="")
    assert seen["org"] == "999"
