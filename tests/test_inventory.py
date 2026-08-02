"""
tests/test_inventory.py
=======================
The pre-migration index: what is in this tenant, and who can see it.

Sharing classification is the part that has to be right. "142 documents" and
"142 documents, 38 shared outside the company" are different facts, and only
the second tells you whether the migration is safe to run — so a
misclassification here is worse than a missing count.
"""

from __future__ import annotations

import inventory
from config import Settings


def settings(domain="c.example.com"):
    s = Settings()
    s.source_domain = domain
    return s


class FakeDrive:
    def __init__(self, pages):
        self.pages, self.i = pages, 0

    def files(self):
        return self

    def list(self, **kw):
        return self

    def execute(self):
        page = self.pages[min(self.i, len(self.pages) - 1)]
        self.i += 1
        return page


class TestShareClassification:
    def test_an_internal_grant_is_not_external(self):
        r = inventory._classify_share(
            [{"type": "user", "emailAddress": "bob@c.example.com"}],
            "c.example.com")
        assert (r["internal"], r["external"]) == (1, 0)

    def test_an_outside_grant_is_external(self):
        r = inventory._classify_share(
            [{"type": "user", "emailAddress": "x@gmail.com"}], "c.example.com")
        assert (r["internal"], r["external"]) == (0, 1)

    def test_a_lookalike_domain_does_not_count_as_internal(self):
        """`notc.example.com` ends with `c.example.com`; only an `@` boundary
        match may count as internal, or an outside collaborator is reported as
        a colleague."""
        r = inventory._classify_share(
            [{"type": "user", "emailAddress": "x@notc.example.com"}],
            "c.example.com")
        assert r["external"] == 1 and r["internal"] == 0

    def test_case_is_ignored_on_both_sides(self):
        r = inventory._classify_share(
            [{"type": "user", "emailAddress": "BOB@C.EXAMPLE.COM"}],
            "c.example.com")
        assert r["internal"] == 1

    def test_groups_count_like_users(self):
        r = inventory._classify_share(
            [{"type": "group", "emailAddress": "team@c.example.com"}],
            "c.example.com")
        assert r["internal"] == 1

    def test_link_sharing_is_flagged_separately(self):
        """It survives into the target, so it needs its own number rather than
        being folded into a share count."""
        r = inventory._classify_share([{"type": "anyone"}], "c.example.com")
        assert r["anyone"] is True

    def test_deleted_grants_are_ignored(self):
        r = inventory._classify_share(
            [{"type": "user", "emailAddress": "x@c.example.com", "deleted": True}],
            "c.example.com")
        assert r["internal"] == 0

    def test_a_grant_with_no_address_is_skipped(self):
        """A grantee whose account was deleted comes back with no address."""
        r = inventory._classify_share([{"type": "user", "role": "reader"}],
                                      "c.example.com")
        assert r["internal"] == r["external"] == 0

    def test_no_permissions_at_all(self):
        for perms in ([], None):
            r = inventory._classify_share(perms, "c.example.com")
            assert not any((r["internal"], r["external"], r["domain"], r["anyone"]))


class TestScanDrive:
    def test_pagination_is_followed(self):
        pages = [
            {"files": [{"id": "1", "mimeType": "application/vnd.google-apps.document"}],
             "nextPageToken": "t"},
            {"files": [{"id": "2", "mimeType": "application/vnd.google-apps.document"}]},
        ]
        r = inventory.scan_drive(FakeDrive(pages), settings(), "a@c.example.com")
        assert r["kinds"]["documents"] == 2

    def test_a_negative_size_cannot_shrink_the_total(self):
        """Drive should never report one, but an unguarded int() would sum it
        straight into the total and understate the corpus."""
        pages = [{"files": [
            {"id": "1", "mimeType": "image/png", "size": "100"},
            {"id": "2", "mimeType": "image/png", "size": "-500"},
        ]}]
        r = inventory.scan_drive(FakeDrive(pages), settings(), "a@c.example.com")
        assert r["total_bytes"] == 100

    def test_a_missing_or_null_size_is_zero_not_a_crash(self):
        pages = [{"files": [
            {"id": "1", "mimeType": "image/png"},
            {"id": "2", "mimeType": "image/png", "size": None},
        ]}]
        r = inventory.scan_drive(FakeDrive(pages), settings(), "a@c.example.com")
        assert r["total_bytes"] == 0

    def test_folders_are_excluded_from_the_share_count(self):
        """A folder's grant reappears on every child as an inherited one, so
        counting both would double every shared tree."""
        pages = [{"files": [
            {"id": "F", "mimeType": inventory.FOLDER_MIME,
             "permissions": [{"type": "anyone"}]},
        ]}]
        r = inventory.scan_drive(FakeDrive(pages), settings(), "a@c.example.com")
        assert r["shared_file_count"] == 0

    def test_unexportable_types_are_counted_up_front(self):
        """Forms and Sites have no export format; saying so before the run
        beats discovering it afterwards."""
        pages = [{"files": [
            {"id": "1", "mimeType": "application/vnd.google-apps.form"},
            {"id": "2", "mimeType": "application/vnd.google-apps.site"},
        ]}]
        r = inventory.scan_drive(FakeDrive(pages), settings(), "a@c.example.com")
        assert r["unexportable"] == 2

    def test_an_unknown_native_type_is_not_silently_a_binary(self):
        pages = [{"files": [
            {"id": "1", "mimeType": "application/vnd.google-apps.something-new"}]}]
        r = inventory.scan_drive(FakeDrive(pages), settings(), "a@c.example.com")
        assert r["kinds"].get("other_native") == 1
        assert "binaries" not in r["kinds"]

    def test_the_grantee_list_is_capped(self):
        """One file can carry a thousand permissions; the report must stay
        readable."""
        perms = [{"type": "user", "emailAddress": f"u{i}@c.example.com"}
                 for i in range(1000)]
        pages = [{"files": [{"id": "1", "name": "x",
                             "mimeType": "application/vnd.google-apps.document",
                             "permissions": perms}]}]
        r = inventory.scan_drive(FakeDrive(pages), settings(), "a@c.example.com")
        assert len(r["top_grantees"]) == 10
        assert r["shared_files"][0]["internal"] == 1000


class TestRender:
    def test_an_empty_tenant_renders(self):
        assert inventory.render([], settings())

    def test_link_sharing_gets_a_warning(self):
        out = inventory.render([{
            "user": "a@c.example.com",
            "drive": {"kinds": {}, "total_bytes": 0, "unexportable": 0,
                      "shared_file_count": 1, "shared_externally": 0,
                      "shared_with_anyone": 1, "shared_files": [],
                      "top_grantees": []},
            "gmail": {"messages": 0, "threads": 0, "drafts": 0, "labels": 0},
            "calendar": {"events": 0, "calendars": 0},
        }], settings())
        assert "link-shared" in out and "fix it before" in out
