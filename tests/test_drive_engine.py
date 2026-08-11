"""
tests/test_drive_engine.py
==========================
The bugs these tests exist to catch are the ones that are expensive in
production and invisible in a code review:

  * a resumed run duplicating every file,
  * a delta pass re-uploading a terabyte because a timestamp comparison is
    inverted,
  * ACLs leaking source-tenant addresses into the target,
  * permission creation firing notification emails,
  * a permanent 403 being retried seven times per item across 200k items.
"""

from __future__ import annotations

import pytest

from config import FOLDER_MIME
from resilience import PermanentAPIError, QuotaExhausted
from tests.conftest import SRC_USER, TGT_USER
from tests.fakes import http_error

NATIVE_DOC = "application/vnd.google-apps.document"


# ======================================================================
# Folder mirroring
# ======================================================================
def test_mirrors_nested_folder_tree(migrator, auth):
    src = auth.source_drive(SRC_USER)
    a = src.add_folder("Projects")
    b = src.add_folder("2024", parent=a)
    src.add_folder("Q1", parent=b)

    migrator.run()

    tgt = auth.target_drive(TGT_USER)
    assert tgt.count(mime=FOLDER_MIME) == 3
    proj = tgt.by_name("Projects")[0]
    y2024 = tgt.by_name("2024")[0]
    q1 = tgt.by_name("Q1")[0]
    # Hierarchy must be preserved, not flattened.
    assert proj["parents"] == [tgt.root_id]
    assert y2024["parents"] == [proj["id"]]
    assert q1["parents"] == [y2024["id"]]


def test_folder_modified_time_preserved(migrator, auth):
    src = auth.source_drive(SRC_USER)
    src.add_folder("Archive", mtime="2019-03-04T05:06:07Z")
    migrator.run()
    tgt = auth.target_drive(TGT_USER)
    assert tgt.by_name("Archive")[0]["modifiedTime"] == "2019-03-04T05:06:07Z"


def test_deep_tree_is_pruned_at_max_depth(migrator, auth, settings):
    settings.max_recursion_depth = 3
    src = auth.source_drive(SRC_USER)
    parent = None
    for i in range(8):
        parent = src.add_folder(f"level{i}", parent=parent)
    migrator.run()
    # Pruned rather than blowing the Python recursion limit.
    assert auth.target_drive(TGT_USER).count(mime=FOLDER_MIME) <= 5


# ======================================================================
# File transfer
# ======================================================================
def test_binary_file_copied_with_matching_checksum(migrator, auth):
    src = auth.source_drive(SRC_USER)
    src.add_binary("report.pdf", data=b"PDF-CONTENT-1234")
    migrator.run()

    tgt = auth.target_drive(TGT_USER)
    copied = tgt.by_name("report.pdf")[0]
    assert tgt.content[copied["id"]] == b"PDF-CONTENT-1234"
    assert copied["md5Checksum"] == src.store[src.by_name("report.pdf")[0]["id"]]["md5Checksum"]
    assert migrator.stats["files"] == 1
    assert migrator.stats["failed"] == 0


def test_native_doc_exported_and_converted_back(migrator, auth):
    src = auth.source_drive(SRC_USER)
    src.add_native("Strategy", kind="document", export_bytes=b"DOCX-BYTES")
    migrator.run()

    tgt = auth.target_drive(TGT_USER)
    doc = tgt.by_name("Strategy")[0]
    # Must land as a native Google Doc, not as a stranded .docx attachment.
    assert doc["mimeType"] == NATIVE_DOC
    assert tgt.exports[doc["id"]] == b"DOCX-BYTES"
    # And it must have gone out through export_media, never get_media.
    assert src.call_count("files.export_media") == 1
    assert src.call_count("files.get_media") == 0


def test_checksum_mismatch_marks_file_failed(migrator, auth, db, monkeypatch):
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("corrupt.bin", data=b"original")
    # Simulate silent corruption in flight: the target stores different bytes.
    tgt = auth.target_drive(TGT_USER)
    real_create = tgt.files()._create

    def corrupting_create(body, media_body=None, fields="", **kw):
        if media_body is not None:
            media_body._data = b"CORRUPTED"
        return real_create(body=body, media_body=media_body, fields=fields, **kw)

    monkeypatch.setattr("tests.fakes._DriveFiles._create",
                        lambda self, **kw: corrupting_create(**kw))
    migrator.run()

    row = db.get_audit(SRC_USER, fid, "file")
    assert row["status"] == "FAILED"
    assert "checksum" in (row["error_message"] or "").lower()


def test_oversized_native_export_is_skipped_not_failed(migrator, auth, db, settings):
    settings.export_size_limit = 50
    src = auth.source_drive(SRC_USER)
    fid = src.add_native("Huge Deck", kind="presentation", export_bytes=b"x" * 500)
    migrator.run()

    row = db.get_audit(SRC_USER, fid, "file")
    assert row["status"] == "SKIPPED_EXPORT_TOO_LARGE"
    assert auth.target_drive(TGT_USER).count() == 0


def test_undownloadable_file_is_skipped(migrator, auth, db):
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("dlp.pdf", can_download=False)
    migrator.run()
    assert db.get_audit(SRC_USER, fid, "file")["status"] == "SKIPPED_NO_DOWNLOAD"


def test_unexportable_native_types_are_skipped(migrator, auth, db):
    src = auth.source_drive(SRC_USER)
    fid = src.add_native("Signup Form", kind="form")
    migrator.run()
    assert db.get_audit(SRC_USER, fid, "file")["status"] == "SKIPPED_UNEXPORTABLE"


def test_shortcut_resolved_after_target_migrates(migrator, auth):
    src = auth.source_drive(SRC_USER)
    target_file = src.add_binary("real.pdf")
    src.add_shortcut("link-to-real", target_id=target_file)
    migrator.run()

    tgt = auth.target_drive(TGT_USER)
    sc = tgt.by_name("link-to-real")
    assert sc, "shortcut should be created by the fixup pass"
    mapped = sc[0]["shortcutDetails"]["targetId"]
    assert mapped == tgt.by_name("real.pdf")[0]["id"]


# ======================================================================
# Idempotency — the property the whole design rests on
# ======================================================================
def test_rerun_creates_nothing_new(migrator, auth, db, settings, quota):
    import drive_engine

    src = auth.source_drive(SRC_USER)
    f = src.add_folder("Docs")
    src.add_binary("a.pdf", parent=f)
    src.add_binary("b.pdf", parent=f)
    src.add_native("Notes", parent=f)

    migrator.run()
    tgt = auth.target_drive(TGT_USER)
    after_first = tgt.count()
    assert after_first == 4  # 1 folder + 3 files

    # Second pass with a fresh migrator, same DB — simulates a restart.
    second = drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota)
    second.run()

    assert tgt.count() == after_first, "resume must not duplicate anything"
    assert second.stats["files"] == 0
    assert second.stats["folders"] == 0
    assert second.stats["skipped"] == 3


def test_interrupted_run_resumes_from_id_mapping(migrator, auth, db, settings, quota):
    import drive_engine

    src = auth.source_drive(SRC_USER)
    for i in range(5):
        src.add_binary(f"f{i}.pdf", data=f"data{i}".encode())

    # Blow up partway through the first run.
    auth.target_drive(TGT_USER).fail_next("files.create", status=500,
                                          reason="backendError", times=99)
    migrator.run()
    assert migrator.stats["failed"] == 5
    assert auth.target_drive(TGT_USER).count() == 0

    # Clear the fault and resume.
    auth.target_drive(TGT_USER)._faults.clear()
    resumed = drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota)
    resumed.run(delta=True)
    assert auth.target_drive(TGT_USER).count() == 5
    assert resumed.stats["failed"] == 0


# ======================================================================
# Module 6: delta pass
# ======================================================================
def test_delta_skips_unchanged_files(migrator, auth, db, settings, quota):
    import drive_engine

    src = auth.source_drive(SRC_USER)
    src.add_binary("stable.pdf", mtime="2024-01-01T00:00:00Z")
    src.add_binary("also-stable.pdf", mtime="2024-01-01T00:00:00Z")
    migrator.run()

    tgt = auth.target_drive(TGT_USER)
    tgt.reset_calls()
    delta = drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota)
    delta.run(delta=True)

    assert delta.stats["files"] == 0
    assert delta.stats["skipped"] == 2
    assert tgt.call_count("files.create") == 0
    assert tgt.call_count("files.update") == 0


def test_delta_updates_only_the_changed_file_in_place(migrator, auth, db,
                                                     settings, quota):
    import drive_engine

    src = auth.source_drive(SRC_USER)
    changed = src.add_binary("edited.pdf", data=b"v1", mtime="2024-01-01T00:00:00Z")
    src.add_binary("untouched.pdf", data=b"same", mtime="2024-01-01T00:00:00Z")
    migrator.run()

    tgt = auth.target_drive(TGT_USER)
    original_target_id = db.get_target_id(SRC_USER, changed, "file")
    tgt.reset_calls()

    src.touch(changed, mtime="2025-06-01T00:00:00Z", data=b"v2-longer-content")
    delta = drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota)
    delta.run(delta=True)

    assert delta.stats["files"] == 1
    assert delta.stats["skipped"] == 1
    # Updated in place: the target ID is stable, so shared links and ACLs
    # that users already have keep working.
    assert tgt.call_count("files.update") == 1
    assert tgt.call_count("files.create") == 0
    assert db.get_target_id(SRC_USER, changed, "file") == original_target_id
    assert tgt.content[original_target_id] == b"v2-longer-content"


def test_delta_comparison_is_not_inverted(migrator, auth, db, settings, quota):
    """A backwards `<`/`>` would re-copy the entire corpus every night."""
    import drive_engine

    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("x.pdf", mtime="2024-05-05T00:00:00Z")
    migrator.run()
    # Move the source timestamp *backwards* — must still be treated as unchanged.
    src.touch(fid, mtime="2020-01-01T00:00:00Z")
    delta = drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota)
    delta.run(delta=True)
    assert delta.stats["files"] == 0


def test_full_rerun_without_delta_flag_skips_everything(migrator, auth, db,
                                                       settings, quota):
    import drive_engine

    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("a.pdf", mtime="2024-01-01T00:00:00Z")
    migrator.run()
    src.touch(fid, mtime="2030-01-01T00:00:00Z")

    second = drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota)
    second.run(delta=False)
    assert second.stats["files"] == 0, "non-delta rerun must not re-copy"


# ======================================================================
# ACL translation
# ======================================================================
def _target_perms(auth, name: str):
    tgt = auth.target_drive(TGT_USER)
    fid = tgt.by_name(name)[0]["id"]
    return tgt.perms[fid]


def test_mapped_user_permission_is_translated(migrator, auth, db):
    from db import bulk_seed_identities

    bulk_seed_identities(db, [("bob@tenanta.com", "robert.jones@tenantb.com")])
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("shared.pdf")
    src.add_permission(fid, "user", "writer", email="bob@tenanta.com")

    migrator.run()
    perms = _target_perms(auth, "shared.pdf")
    assert any(p["emailAddress"] == "robert.jones@tenantb.com"
               and p["role"] == "writer" for p in perms)
    assert not any("tenanta.com" in (p.get("emailAddress") or "") for p in perms)


def test_unmapped_internal_identity_is_dropped_and_logged(migrator, auth, db):
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("orphan-acl.pdf")
    src.add_permission(fid, "user", "reader", email="ghost@tenanta.com")

    migrator.run()
    assert _target_perms(auth, "orphan-acl.pdf") == []
    row = db.get_audit(SRC_USER, f"{fid}:ghost@tenanta.com", "acl")
    assert row is not None and row["status"] == "SKIPPED_UNMAPPED_IDENTITY"


def test_permission_with_no_email_is_skipped_not_sent_empty(migrator, auth, db):
    """A dangling grant left behind after the grantee's account was deleted
    surfaces as a 'user' permission with no emailAddress at all. Sending that
    straight to the API produces a confusing 400; it should be skipped."""
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("stale-grant.pdf")
    src.add_permission(fid, "user", "reader")  # no email= at all

    migrator.run()
    assert _target_perms(auth, "stale-grant.pdf") == []
    row = db.get_audit(SRC_USER, f"{fid}:(no-email)", "acl")
    assert row is not None and row["status"] == "SKIPPED_UNMAPPED_IDENTITY"
    # The file itself must still migrate -- a stale ACL is not the file's fault.
    assert auth.target_drive(TGT_USER).by_name("stale-grant.pdf")


def test_external_collaborator_is_preserved_verbatim(migrator, auth):
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("vendor.pdf")
    src.add_permission(fid, "user", "commenter", email="jo@partner.com")

    migrator.run()
    perms = _target_perms(auth, "vendor.pdf")
    assert any(p["emailAddress"] == "jo@partner.com"
               and p["role"] == "commenter" for p in perms)


def test_domain_permission_is_rewritten_to_target_domain(migrator, auth):
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("internal.pdf")
    src.add_permission(fid, "domain", "reader", domain="tenanta.com")
    src.add_permission(fid, "domain", "reader", domain="partner.com")

    migrator.run()
    domains = {p.get("domain") for p in _target_perms(auth, "internal.pdf")}
    assert "tenantb.com" in domains
    assert "partner.com" in domains
    assert "tenanta.com" not in domains


def test_anyone_with_link_is_preserved(migrator, auth):
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("public.pdf")
    src.add_permission(fid, "anyone", "reader", allow_discovery=True)
    migrator.run()
    perms = _target_perms(auth, "public.pdf")
    assert any(p["type"] == "anyone" and p["allowFileDiscovery"] for p in perms)


def test_owner_permission_is_never_reapplied(migrator, auth):
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("mine.pdf")
    src.add_permission(fid, "user", "owner", email=SRC_USER)
    migrator.run()
    assert all(p["role"] != "owner" for p in _target_perms(auth, "mine.pdf"))


def test_inherited_permissions_are_recreated_per_file(migrator, auth, db):
    """A grant a document gets through its parent folder is made explicit on
    the migrated document itself, so the share access does not depend on the
    file staying put in the tree."""
    from db import bulk_seed_identities

    bulk_seed_identities(db, [("bob@tenanta.com", "bob@tenantb.com")])
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("child.pdf")
    src.add_permission(fid, "user", "reader", email="bob@tenanta.com",
                       inherited=True)
    migrator.run()
    perms = _target_perms(auth, "child.pdf")
    assert any(p["emailAddress"] == "bob@tenantb.com"
               and p["role"] == "reader" for p in perms)


def test_inherited_permission_default_is_on():
    from config import Settings

    s = Settings()
    assert s.recreate_inherited_acls is True


def test_inherited_permissions_can_stay_folder_derived(
        migrator, auth, db, settings):
    """MIGRATE_INHERITED_ACLS=false returns to folder-derived sharing: no
    per-file grant is recreated, only the parent folder's."""
    from db import bulk_seed_identities

    settings.recreate_inherited_acls = False
    bulk_seed_identities(db, [("bob@tenanta.com", "bob@tenantb.com")])
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("child.pdf")
    src.add_permission(fid, "user", "reader", email="bob@tenanta.com",
                       inherited=True)
    migrator.run()
    assert _target_perms(auth, "child.pdf") == []


def test_permissions_never_send_notification_email(migrator, auth, db):
    """A regression here mails every collaborator once per migrated file."""
    from db import bulk_seed_identities

    bulk_seed_identities(db, [("bob@tenanta.com", "bob@tenantb.com")])
    src = auth.source_drive(SRC_USER)
    for i in range(4):
        fid = src.add_binary(f"doc{i}.pdf")
        src.add_permission(fid, "user", "reader", email="bob@tenanta.com")

    migrator.run()
    creates = auth.target_drive(TGT_USER).calls_to("permissions.create")
    assert len(creates) == 4
    assert all(c["sendNotificationEmail"] is False for c in creates)


def test_modified_time_survives_acl_application(migrator, auth, db):
    """
    Granting a permission bumps modifiedTime to now (real Drive behaviour,
    verified live). ACLs are applied after the copy, so without a re-assert
    every *shared* file would show the migration date -- silently breaking
    "sort by last modified" for exactly the files people collaborate on.
    """
    from db import bulk_seed_identities

    bulk_seed_identities(db, [("bob@tenanta.com", "bob@tenantb.com")])
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("shared.pdf", mtime="2019-03-04T05:06:07Z")
    src.add_permission(fid, "user", "reader", email="bob@tenanta.com")

    migrator.run()

    tgt = auth.target_drive(TGT_USER)
    copied = tgt.by_name("shared.pdf")[0]
    assert tgt.perms[copied["id"]], "precondition: a grant was actually applied"
    assert copied["modifiedTime"] == "2019-03-04T05:06:07Z"


def test_modified_time_restore_runs_after_the_grants(migrator, auth, db, settings):
    """Order, not just outcome: the restore must be the last write.

    It is tempting to fold the restore into the staging->My Drive move (the
    move already accepts modifiedTime) and save a write per file -- ~1.5x on
    a path where target-account writes are the binding constraint.

    Measured against the live tenant on 2026-08-10, 15 trials: grant-then-move
    silently lost modifiedTime 3 times out of 15, stamping the file with the
    migration date. The current order held 14/14. A grant's modifiedTime bump
    can land *after* a parent-changing update; the current order is immune
    because its last write is a standalone metadata update that changes no
    parents, so it deterministically wins.

    The ~20% rate is what makes it worth a test rather than a comment: it
    passes casual manual checking, and the damage -- a wrong date on a fifth
    of all shared files -- is silent, unretried and absent from the audit log.

    See contract_probe.probe_staging_acl_order.
    """
    from db import bulk_seed_identities

    _server_side(settings)
    bulk_seed_identities(db, [("bob@tenanta.com", "bob@tenantb.com")])
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("shared.pdf", mtime="2019-03-04T05:06:07Z")
    src.add_permission(fid, "user", "reader", email="bob@tenanta.com")

    migrator.run()

    names = [n for n, _ in auth.target_drive(TGT_USER).calls]
    assert "permissions.create" in names, "precondition: a grant was applied"
    mtime_updates = [i for i, (n, kw) in
                     enumerate(auth.target_drive(TGT_USER).calls)
                     if n == "files.update" and "modifiedTime" in str(kw.get("body"))
                     and "addParents" not in kw]
    assert mtime_updates, "no standalone modifiedTime restore was issued"
    assert max(mtime_updates) > names.index("permissions.create"), \
        "the restore ran before the grants, so the grant's bump would win"


def test_modified_time_survives_acls_on_folders_too(migrator, auth, db):
    from db import bulk_seed_identities

    bulk_seed_identities(db, [("bob@tenanta.com", "bob@tenantb.com")])
    src = auth.source_drive(SRC_USER)
    folder = src.add_folder("Shared Reports", mtime="2020-07-08T09:10:11Z")
    src.add_permission(folder, "user", "writer", email="bob@tenanta.com")

    migrator.run()

    tgt = auth.target_drive(TGT_USER)
    copied = tgt.by_name("Shared Reports")[0]
    assert copied["modifiedTime"] == "2020-07-08T09:10:11Z"


def test_unshared_file_needs_no_extra_update_call(migrator, auth):
    """The re-assert only fires when a grant was actually applied -- an
    unshared file must not cost an extra API round trip per item."""
    src = auth.source_drive(SRC_USER)
    src.add_binary("private.pdf", mtime="2021-01-02T03:04:05Z")

    migrator.run()

    tgt = auth.target_drive(TGT_USER)
    assert tgt.by_name("private.pdf")[0]["modifiedTime"] == "2021-01-02T03:04:05Z"
    assert tgt.call_count("files.update") == 0


def test_acl_failure_does_not_lose_the_file(migrator, auth, db):
    from db import bulk_seed_identities

    bulk_seed_identities(db, [("bob@tenanta.com", "bob@tenantb.com")])
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("policy-blocked.pdf")
    src.add_permission(fid, "user", "writer", email="bob@tenanta.com")
    auth.target_drive(TGT_USER).fail_next(
        "permissions.create", status=403, reason="domainPolicy", times=5
    )

    migrator.run()
    # The bytes landed; only the ACL failed.
    assert db.get_audit(SRC_USER, fid, "file")["status"] == "SUCCESS"
    assert auth.target_drive(TGT_USER).by_name("policy-blocked.pdf")


def test_acl_batch_applies_every_grant_via_one_round_trip(
        migrator, auth, db, settings, monkeypatch):
    """
    _sync_acls routes multiple grants through a BatchHttpRequest when the
    client is the real API. The test fakes have no _http, so this forces the
    batch path by faking a client and a BatchHttpRequest that just executes
    each request object it is given -- the point is to prove the batching
    bookkeeping (applied count, per-grant failure audit) matches the
    per-call loop it replaced.
    """
    from db import bulk_seed_identities

    bulk_seed_identities(db, [("bob@tenanta.com", "bob@tenantb.com"),
                              ("carol@tenanta.com", "carol@tenantb.com")])
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("shared-with-team.pdf")
    src.add_permission(fid, "user", "reader", email="bob@tenanta.com")
    src.add_permission(fid, "user", "writer", email="carol@tenanta.com")

    # Make the target client look like the real API so the batch path runs.
    auth.target_drive(TGT_USER)._http = object()

    executed: list = []

    class FakeBatch:
        def __init__(self, callback=None, batch_uri=None, http=None):
            self._requests = []

        def add(self, request, request_id=None, callback=None):
            self._requests.append((request, request_id, callback))

        def execute(self, **kw):
            for request, request_id, callback in self._requests:
                try:
                    resp = request.execute()
                except Exception as exc:
                    callback(request_id, None, exc)
                else:
                    callback(request_id, resp, None)

    monkeypatch.setattr("googleapiclient.http.BatchHttpRequest", FakeBatch)
    settings.acl_batch_size = 20

    migrator.run()

    tgt = auth.target_drive(TGT_USER)
    copied = tgt.by_name("shared-with-team.pdf")[0]
    emails = {p["emailAddress"] for p in tgt.perms[copied["id"]]}
    assert emails == {"bob@tenantb.com", "carol@tenantb.com"}
    assert db.get_audit(SRC_USER, fid, "file")["status"] == "SUCCESS"


def test_acl_batch_chunks_respect_batch_size(migrator, auth, db, settings,
                                             monkeypatch):
    """Grants beyond acl_batch_size spill into a second batch request."""
    from db import bulk_seed_identities

    seeds = [(f"u{i}@tenanta.com", f"u{i}@tenantb.com") for i in range(5)]
    bulk_seed_identities(db, seeds)
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("many-grantees.pdf")
    for i in range(5):
        src.add_permission(fid, "user", "reader", email=f"u{i}@tenanta.com")

    auth.target_drive(TGT_USER)._http = object()
    batches: list[list] = []

    class FakeBatch:
        def __init__(self, callback=None, batch_uri=None, http=None):
            self._requests = []

        def add(self, request, request_id=None, callback=None):
            self._requests.append((request, request_id, callback))

        def execute(self, **kw):
            batches.append(self._requests)
            for request, request_id, callback in self._requests:
                try:
                    resp = request.execute()
                except Exception as exc:
                    callback(request_id, None, exc)
                else:
                    callback(request_id, resp, None)

    monkeypatch.setattr("googleapiclient.http.BatchHttpRequest", FakeBatch)
    settings.acl_batch_size = 2

    migrator.run()

    # 5 grants at 2 per batch = 2 batch requests (2+2), with the trailing
    # single grant going through the per-call path rather than a one-item
    # batch. Every grant must still land regardless of which path took it.
    assert len(batches) == 2
    assert [len(b) for b in batches] == [2, 2]
    tgt = auth.target_drive(TGT_USER)
    copied = tgt.by_name("many-grantees.pdf")[0]
    assert len(tgt.perms[copied["id"]]) == 5


def test_acl_batch_uses_the_discovery_endpoint_not_the_legacy_one(
        migrator, auth, db, settings, monkeypatch):
    """The B4 bug, as a regression test.

    `BatchHttpRequest()` without a batch_uri falls back to the legacy
    `https://www.googleapis.com/batch`, which Google turned down -- the live
    B4 Trial A ran every grant create against a 404 and silently lost
    20,714/20,714 grants while the files themselves copied fine. The engine
    must build its batch from the discovery document (drive v3 batchPath),
    i.e. via the client's `new_batch_http_request()`, never the bare class.
    """
    from db import bulk_seed_identities

    bulk_seed_identities(db, [("bob@tenanta.com", "bob@tenantb.com"),
                              ("carol@tenanta.com", "carol@tenantb.com")])
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("shared-to-team.pdf")
    src.add_permission(fid, "user", "reader", email="bob@tenanta.com")
    src.add_permission(fid, "user", "writer", email="carol@tenanta.com")

    auth.target_drive(TGT_USER)._http = object()

    uris: list[str] = []

    class FakeBatch:
        def __init__(self, callback=None, batch_uri=None, http=None):
            self._requests = []
            self._callback = callback
            uris.append(batch_uri)

        def add(self, request, request_id=None, callback=None):
            self._requests.append((request, request_id, callback))

        def execute(self, **kw):
            for request, request_id, callback in self._requests:
                try:
                    resp = request.execute()
                except Exception as exc:
                    callback(request_id, None, exc)
                else:
                    callback(request_id, resp, None)

    monkeypatch.setattr("googleapiclient.http.BatchHttpRequest", FakeBatch)
    settings.acl_batch_size = 20

    migrator.run()

    assert uris, "expected the batch path to run"
    assert all(u != "https://www.googleapis.com/batch" for u in uris), \
        "legacy batch endpoint must never be used"
    assert all("/batch/" in u for u in uris), \
        f"expected an API-specific batchPath, got {uris}"
    tgt = auth.target_drive(TGT_USER)
    copied = tgt.by_name("shared-to-team.pdf")[0]
    assert {p["emailAddress"] for p in tgt.perms[copied["id"]]} == \
        {"bob@tenantb.com", "carol@tenantb.com"}
    assert db.get_audit(SRC_USER, fid, "file")["status"] == "SUCCESS"


# ======================================================================
# Drive comments (MIGRATE_COMMENTS)
# ======================================================================
def test_comments_are_migrated_when_enabled(auth, db, settings, identity, quota):
    import drive_engine

    settings.migrate_comments = True
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("reviewed.pdf")
    src.add_comment(fid, "Needs a second look", author="Bob Johnson",
                    created="2023-05-06T00:00:00Z")

    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()

    tgt = auth.target_drive(TGT_USER)
    copied = tgt.by_name("reviewed.pdf")[0]
    comments = tgt.comment_store[copied["id"]]
    assert len(comments) == 1
    # The API cannot author a comment as someone else, so attribution is
    # carried in the text rather than silently reassigned.
    assert "Bob Johnson" in comments[0]["content"]
    assert "Needs a second look" in comments[0]["content"]


def test_comment_replies_are_migrated(auth, db, settings, identity, quota):
    import drive_engine

    settings.migrate_comments = True
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("thread.pdf")
    src.add_comment(fid, "Top level", author="Alice",
                    replies=[{"id": "r1", "content": "Agreed",
                             "author": {"displayName": "Carol"},
                             "createdTime": "2023-05-07T00:00:00Z"}])

    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()

    tgt = auth.target_drive(TGT_USER)
    copied = tgt.by_name("thread.pdf")[0]
    replies = tgt.comment_store[copied["id"]][0]["replies"]
    assert len(replies) == 1
    assert "Carol" in replies[0]["content"]


def test_comments_are_skipped_unless_enabled(auth, db, settings, identity, quota):
    import drive_engine

    settings.migrate_comments = False
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("quiet.pdf")
    src.add_comment(fid, "should not travel")

    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
    assert src.call_count("comments.list") == 0


def test_comments_are_not_duplicated_on_rerun(auth, db, settings, identity, quota):
    import drive_engine

    settings.migrate_comments = True
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("once.pdf")
    src.add_comment(fid, "only once")

    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
    tgt = auth.target_drive(TGT_USER)
    copied = tgt.by_name("once.pdf")[0]
    assert len(tgt.comment_store[copied["id"]]) == 1

    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
    assert len(tgt.comment_store[copied["id"]]) == 1


# ======================================================================
# Server-side copy mode (TRANSFER_MODE=server_side)
# ======================================================================
def _server_side(settings):
    settings.transfer_mode = "server_side"
    return settings


def test_server_side_copies_without_streaming_bytes(auth, db, settings, identity, quota):
    """The whole point: no download/upload through this host at all."""
    import drive_engine

    _server_side(settings)
    src = auth.source_drive(SRC_USER)
    src.add_binary("big.bin", data=b"x" * 4096)

    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()

    assert src.call_count("files.copy") == 1
    assert src.call_count("files.get_media") == 0
    assert src.call_count("files.export_media") == 0
    assert auth.target_drive(TGT_USER).by_name("big.bin")


def test_server_side_keeps_native_docs_native(auth, db, settings, identity, quota):
    """No OOXML round trip means no fidelity loss and no 10 MB export ceiling."""
    import drive_engine

    _server_side(settings)
    settings.export_size_limit = 10          # would reject any export
    src = auth.source_drive(SRC_USER)
    src.add_native("Huge Strategy", kind="document", export_bytes=b"y" * 5000)

    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()

    tgt = auth.target_drive(TGT_USER)
    doc = tgt.by_name("Huge Strategy")[0]
    assert doc["mimeType"] == NATIVE_DOC
    # Export never happened, so the size ceiling never applied.
    assert src.call_count("files.export_media") == 0
    assert db.get_audit(SRC_USER, src.by_name("Huge Strategy")[0]["id"],
                        "file")["status"] == "SUCCESS"


def test_server_side_file_lands_in_target_my_drive_not_staging(auth, db, settings,
                                                               identity, quota):
    import drive_engine

    _server_side(settings)
    src = auth.source_drive(SRC_USER)
    folder = src.add_folder("Reports")
    src.add_binary("q1.pdf", parent=folder)

    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()

    tgt = auth.target_drive(TGT_USER)
    copied = tgt.by_name("q1.pdf")[0]
    tgt_folder = tgt.by_name("Reports")[0]
    # Reparented out of staging and into the mirrored folder.
    assert copied["parents"] == [tgt_folder["id"]]
    assert copied.get("driveId") is None
    assert copied["owners"] == [{"emailAddress": TGT_USER}]


def test_server_side_staging_drive_is_cleaned_up(auth, db, settings, identity, quota):
    import drive_engine

    _server_side(settings)
    auth.source_drive(SRC_USER).add_binary("a.pdf")
    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
    # Everything moved out, so the empty staging drive is removed.
    assert auth.target_drive(TGT_USER).shared_drives == {}


def test_server_side_keeps_staging_drive_when_a_file_is_stranded(auth, db, settings,
                                                                 identity, quota):
    """A copy that lands but fails to move must not be deleted along with the
    staging drive -- losing bytes is worse than leaving a drive behind."""
    import drive_engine

    _server_side(settings)
    auth.source_drive(SRC_USER).add_binary("stranded.pdf")
    tgt = auth.target_drive(TGT_USER)
    tgt.fail_next("files.update", status=403, reason="insufficientPermissions",
                  times=9)

    m = drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota)
    m.run()

    assert m.stats["failed"] == 1
    assert tgt.shared_drives, "staging drive must survive so the copy isn't lost"


def test_server_side_reuses_existing_staging_drive_on_resume(auth, db, settings,
                                                            identity, quota):
    import drive_engine

    _server_side(settings)
    src = auth.source_drive(SRC_USER)
    src.add_binary("one.pdf")
    tgt = auth.target_drive(TGT_USER)
    tgt.fail_next("files.update", status=403, reason="insufficientPermissions",
                  times=9)
    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
    drives_after_first = dict(tgt.shared_drives)
    assert len(drives_after_first) == 1

    tgt._faults.clear()
    tgt.reset_calls()
    src.add_binary("two.pdf")
    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()

    # No second staging drive was created for the same user pair.
    assert tgt.call_count("drives.create") == 0


def test_server_side_rerun_is_still_idempotent(auth, db, settings, identity, quota):
    import drive_engine

    _server_side(settings)
    src = auth.source_drive(SRC_USER)
    f = src.add_folder("Docs")
    src.add_binary("a.pdf", parent=f)
    src.add_native("Notes", parent=f)

    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
    tgt = auth.target_drive(TGT_USER)
    after_first = tgt.count()

    second = drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota)
    second.run()
    assert tgt.count() == after_first
    assert second.stats["files"] == 0
    assert second.stats["skipped"] == 2


def test_server_side_dry_run_writes_nothing(auth, db, settings, identity, quota):
    import drive_engine

    _server_side(settings)
    settings.dry_run = True
    src = auth.source_drive(SRC_USER)
    src.add_binary("a.pdf")

    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
    tgt = auth.target_drive(TGT_USER)
    assert tgt.count() == 0
    assert src.call_count("files.copy") == 0
    assert tgt.shared_drives == {}, "dry run must not even create a staging drive"


def test_server_side_still_resolves_shortcuts(auth, db, settings, identity, quota):
    import drive_engine

    _server_side(settings)
    src = auth.source_drive(SRC_USER)
    target_file = src.add_binary("real.pdf")
    src.add_shortcut("link-to-real", target_id=target_file)

    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()

    tgt = auth.target_drive(TGT_USER)
    sc = tgt.by_name("link-to-real")
    assert sc, "shortcut fixup must run in server_side mode too"
    assert sc[0]["shortcutDetails"]["targetId"] == tgt.by_name("real.pdf")[0]["id"]


def test_server_side_delta_updates_in_place_via_upload(auth, db, settings,
                                                       identity, quota):
    """
    A changed file is updated in place rather than re-copied, so the target
    file ID and the ACLs already hanging off it survive. That path streams
    bytes even in server_side mode -- deliberate: delete-and-recopy would
    change the ID and drop every existing share.
    """
    import drive_engine

    _server_side(settings)
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("edited.pdf", data=b"v1", mtime="2024-01-01T00:00:00Z")
    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()

    tgt = auth.target_drive(TGT_USER)
    original_target_id = db.get_target_id(SRC_USER, fid, "file")
    tgt.reset_calls()

    src.touch(fid, mtime="2025-06-01T00:00:00Z", data=b"v2-longer")
    delta = drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota)
    delta.run(delta=True)

    assert delta.stats["files"] == 1
    assert db.get_target_id(SRC_USER, fid, "file") == original_target_id
    assert tgt.content[original_target_id] == b"v2-longer"


def test_server_side_acls_are_still_translated(auth, db, settings, identity, quota):
    import drive_engine
    from db import bulk_seed_identities

    _server_side(settings)
    bulk_seed_identities(db, [("bob@tenanta.com", "robert@tenantb.com")])
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("shared.pdf")
    src.add_permission(fid, "user", "writer", email="bob@tenanta.com")

    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()

    tgt = auth.target_drive(TGT_USER)
    copied = tgt.by_name("shared.pdf")[0]
    emails = {p.get("emailAddress") for p in tgt.perms[copied["id"]]}
    assert "robert@tenantb.com" in emails
    assert not any((e or "").endswith("tenanta.com") for e in emails)


# ======================================================================
# Resilience wiring
# ======================================================================
def test_transient_rate_limit_is_retried_then_succeeds(migrator, auth, db):
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("flaky.pdf")
    auth.target_drive(TGT_USER).fail_next(
        "files.create", status=403, reason="rateLimitExceeded", times=2
    )
    migrator.run()
    assert db.get_audit(SRC_USER, fid, "file")["status"] == "SUCCESS"
    assert auth.target_drive(TGT_USER).call_count("files.create") == 3


def test_permanent_403_is_not_retried(migrator, auth, db, settings):
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("nospace.pdf")
    auth.target_drive(TGT_USER).fail_next(
        "files.create", status=403, reason="storageQuotaExceeded", times=99
    )
    migrator.run()
    assert db.get_audit(SRC_USER, fid, "file")["status"] == "FAILED"
    # Exactly one attempt: retrying a full Drive burns quota for nothing.
    assert auth.target_drive(TGT_USER).call_count("files.create") == 1


def test_503_is_retried(migrator, auth, db):
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("unavailable.pdf")
    auth.target_drive(TGT_USER).fail_next("files.create", status=503,
                                          reason="backendError", times=3)
    migrator.run()
    assert db.get_audit(SRC_USER, fid, "file")["status"] == "SUCCESS"


# ======================================================================
# Quota governance
# ======================================================================
def test_quota_exhaustion_halts_the_user(auth, db, settings, identity):
    import drive_engine
    from resilience import DailyQuotaGuard

    src = auth.source_drive(SRC_USER)
    for i in range(5):
        src.add_binary(f"big{i}.bin", data=b"x" * 1000)

    tiny = DailyQuotaGuard(db, TGT_USER, 2500)  # room for two files
    m = drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, tiny)
    with pytest.raises(QuotaExhausted):
        m.run()
    assert auth.target_drive(TGT_USER).count() == 2


def test_quota_consumption_survives_restart(db, settings):
    from resilience import DailyQuotaGuard

    g1 = DailyQuotaGuard(db, TGT_USER, 1000)
    g1.reserve(700)
    # New guard object, same DB — a process restart must not reset the ledger.
    g2 = DailyQuotaGuard(db, TGT_USER, 1000)
    assert g2.remaining() == 300
    with pytest.raises(QuotaExhausted):
        g2.reserve(400)


def test_failed_upload_refunds_the_quota(migrator, auth, db, quota):
    src = auth.source_drive(SRC_USER)
    src.add_binary("doomed.bin", data=b"y" * 5000)
    before = quota.remaining()
    auth.target_drive(TGT_USER).fail_next("files.create", status=403,
                                          reason="insufficientPermissions")
    migrator.run()
    assert quota.remaining() == before, "a failed upload must not eat the cap"


# ======================================================================
# Dry run
# ======================================================================
def test_dry_run_writes_nothing(auth, db, settings, identity, quota):
    import drive_engine

    settings.dry_run = True
    src = auth.source_drive(SRC_USER)
    f = src.add_folder("Docs")
    src.add_binary("a.pdf", parent=f)

    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER,
                               quota).run()
    tgt = auth.target_drive(TGT_USER)
    assert tgt.count() == 0
    assert tgt.call_count("files.create") == 0


# ----------------------------------------------------------------------
# Failures that used to leave no trace.
#
# Both of these logged nothing at all: a `log.warning` at most, which scrolls
# past and gives `report` and resolve_failures nothing to act on. A file whose
# sharing never transferred looked identical to one that had no sharing.
# ----------------------------------------------------------------------
def test_unreadable_source_acl_is_recorded_not_just_warned(migrator, auth, db):
    """The root cause behind resolve_failures erasing ACL failures: _sync_acls
    returned 0 on error without writing an audit row, so the retry tool
    deleted the old FAILED row and found nothing to replace it with."""
    class Exploding:
        def permissions(self):
            return self

        def list(self, **kw):
            raise RuntimeError("permission denied listing permissions")

    # Replace the migrator's own client: auth.source_drive() hands back a fresh
    # fake each call, so patching that would not touch the captured one.
    migrator.src = Exploding()

    applied = migrator._sync_acls("src-file", "tgt-file")

    assert applied == 0
    rows = db.conn.execute(
        "SELECT item_id, item_type, status FROM audit_log "
        "WHERE source_user=? AND item_type='acl'",
        ("alice@tenanta.com",)).fetchall()
    assert rows, "an unreadable source ACL left no record at all"
    assert rows[0]["status"].startswith("FAILED")
    assert migrator.stats["acl_failed"] >= 1


def test_unreadable_acls_are_skipped_not_failed_when_merely_denied(migrator, db):
    """403 insufficientFilePermissions is Google working correctly, not an error.

    A user who is only a *reader* on someone else's file cannot enumerate its
    permissions. That happens on every externally-owned shared-with-me file,
    which MIGRATE_EXTERNAL_SHARES copies precisely because nobody inside the
    org owns them -- so B6 logged 18 of these against an otherwise perfect
    run. A clean migration reporting 18 failures is how operators learn to
    ignore the failure count, which is the exact desensitising that let B4's
    20,714 silently-404ing grants go unnoticed.

    Still recorded, so nothing vanishes: SKIPPED_NO_PERMISSION, not silence.
    """
    class Denied:
        def permissions(self):
            return self

        def list(self, **kw):
            raise RuntimeError(
                "HTTP 403 (insufficientFilePermissions): The user does not "
                "have sufficient permissions for this file")

    migrator.src = Denied()
    assert migrator._sync_acls("src-file", "tgt-file") == 0

    row = db.conn.execute(
        "SELECT status FROM audit_log WHERE source_user=? AND item_type='acl'",
        ("alice@tenanta.com",)).fetchone()
    assert row["status"] == "SKIPPED_NO_PERMISSION"
    assert migrator.stats["acl_failed"] == 0, (
        "a permission we were never allowed to read is not a lost permission")


def test_a_dropped_comment_reply_is_recorded(migrator):
    """A reply that could not be recreated simply vanished, so the thread came
    out shorter on the target with nothing anywhere saying so."""
    import inspect

    src = inspect.getsource(migrator.__class__)
    # the reply handler must record, not `pass`
    reply_bit = src[src.index("replies().create"):]
    handler = reply_bit[:reply_bit.index("# -- ACL translation")]
    assert "log_audit" in handler
    assert "reply not recreated" in handler


# ----------------------------------------------------------------------
# modifiedTime survives *every* post-create write, not just the first kind.
#
# Found by measurement, not by review: an A/B across 1,342 real files showed
# 97 with a drifted modifiedTime in both transfer modes, and every one of the
# 97 was native and carried comments. The engine restored the timestamp after
# ACLs and then wrote comments, undoing the restore it had just made. The
# suite could not see it because the fake modelled the permission bump but
# not the comment bump -- so the fix is in two places, and this test fails
# against the old ordering.
# ----------------------------------------------------------------------
def test_modified_time_survives_comments_not_just_acls(auth, db, settings,
                                                       identity, quota):
    """A commented, shared file must still read back with its own timestamp."""
    import drive_engine

    settings.migrate_comments = True
    src = auth.source_drive(SRC_USER)
    doc = src.add_native("Design — Project note 006", mtime="2024-03-01T09:00:00Z")
    src.add_comment(doc, "does this still apply?", author="Bob")
    # Shared, so the ACL path runs too and the two restores cannot be confused.
    src.perms[doc].append({"id": "p1", "type": "user", "role": "writer",
                           "emailAddress": "bob@tenanta.com"})

    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER,
                               quota).run()

    tgt = auth.target_drive(TGT_USER)
    got = tgt.by_name("Design — Project note 006")
    assert got, "the document did not arrive at all"
    assert got[0]["modifiedTime"] == "2024-03-01T09:00:00Z", (
        "modifiedTime was left at the value the last write stamped on it "
        f"({got[0]['modifiedTime']}) instead of the source's")


# ----------------------------------------------------------------------
# owned_only: the invariant that stops a shared file being copied once per
# recipient. Default True, applied in both discovery and the engine, and
# until now untested.
#
# The seeder builds a cross-user sharing graph precisely so this can be
# checked: five users collectively *see* far more than they collectively
# *own*, and a correct migration reproduces the union of what they own
# exactly once. Without this filter, a deck shared with four colleagues is
# stored five times in the target -- paid for five times, and nobody can say
# which copy is authoritative.
# ----------------------------------------------------------------------
def test_a_file_shared_in_from_a_colleague_is_not_migrated(migrator, auth, db):
    """It belongs to its owner's migration, not to everyone who can see it."""
    src = auth.source_drive(SRC_USER)
    mine = src.add_binary("my-deck.pptx")
    theirs = src.add_binary("their-deck.pptx")
    # reassign ownership: this one is shared in, not owned
    src.store[theirs]["owners"] = [{"emailAddress": "colleague@tenanta.com"}]

    migrator.run()

    assert db.get_target_id(SRC_USER, mine, "file"), "own file must migrate"
    assert not db.get_target_id(SRC_USER, theirs, "file"), \
        "a shared-in file was copied, so every recipient stores their own copy"


def test_owned_only_is_on_by_default(monkeypatch):
    monkeypatch.delenv("OWNED_ONLY", raising=False)
    from config import Settings

    assert Settings().owned_only is True


def test_turning_it_off_migrates_shared_in_files_too(migrator, auth, db, settings):
    """The escape hatch exists for a single-user rescue, where 'everything
    this account can see' is the point."""
    settings.owned_only = False
    src = auth.source_drive(SRC_USER)
    theirs = src.add_binary("their-deck.pptx")
    src.store[theirs]["owners"] = [{"emailAddress": "colleague@tenanta.com"}]

    migrator.run()

    assert db.get_target_id(SRC_USER, theirs, "file")


def test_the_filter_reaches_the_query_not_just_the_results(migrator):
    """Filtering after listing would still pay to enumerate every shared-in
    file on every user -- on a real tenant that is the bulk of the listing."""
    import inspect

    # It belongs in the listing query, not in _walk: filtering after the fact
    # would still pay to enumerate every shared-in file on every user, which
    # on a real tenant is the bulk of the listing.
    src = inspect.getsource(migrator.__class__._list_children)
    assert "'me' in owners" in src
    assert "owned_only" in src


# ======================================================================
# migrate_external_shares: rescue files shared in from owners OUTSIDE the
# source org. A colleague-owned file is migrated by that colleague, so a
# recipient must not copy it; a file owned by an external domain is carried
# by nobody and is lost unless the recipient's run picks it up.
# ======================================================================
def test_external_share_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("MIGRATE_EXTERNAL_SHARES", raising=False)
    from config import Settings

    assert Settings().migrate_external_shares is False


def test_external_owned_shared_file_is_migrated_when_enabled(
        migrator, auth, db, settings):
    """A file owned by an external domain and shared in is carried by the
    recipient's run when the flag is on."""
    settings.migrate_external_shares = True
    src = auth.source_drive(SRC_USER)
    owned = src.add_binary("my-deck.pptx")
    theirs = src.add_binary("their-deck.pptx")
    # reassign ownership: this one is shared in, not owned
    src.store[theirs]["owners"] = [{"emailAddress": "external@partner.com"}]
    src.perms[theirs].append({"id": "p1", "type": "user", "role": "reader",
                              "emailAddress": SRC_USER})

    migrator.run()

    assert db.get_target_id(SRC_USER, owned, "file"), "own file must migrate"
    assert db.get_target_id(SRC_USER, theirs, "file"), \
        "external-org-owned shared file must be rescued"


def test_external_owned_shared_file_is_not_migrated_by_default(
        migrator, auth, db):
    """Flag off: identical to today's behaviour -- external shares are left
    alone, matching the owned_only invariant for same-org files."""
    src = auth.source_drive(SRC_USER)
    theirs = src.add_binary("their-deck.pptx")
    src.store[theirs]["owners"] = [{"emailAddress": "external@partner.com"}]
    src.perms[theirs].append({"id": "p1", "type": "user", "role": "reader",
                              "emailAddress": SRC_USER})

    migrator.run()

    assert not db.get_target_id(SRC_USER, theirs, "file"), \
        "external share must not migrate while the flag is off"


def test_colleague_owned_shared_file_still_not_migrated_even_when_enabled(
        migrator, auth, db, settings):
    """The rescue must not turn into a copy-once-per-recipient for files a
    colleague (source org) owns -- their run carries those."""
    settings.migrate_external_shares = True
    src = auth.source_drive(SRC_USER)
    theirs = src.add_binary("their-deck.pptx")
    src.store[theirs]["owners"] = [{"emailAddress": "colleague@tenanta.com"}]
    src.perms[theirs].append({"id": "p1", "type": "user", "role": "reader",
                              "emailAddress": SRC_USER})

    migrator.run()

    assert not db.get_target_id(SRC_USER, theirs, "file"), \
        "same-org owner migrates it; a recipient must not copy it"


def test_external_shared_folder_tree_is_migrated_when_enabled(
        migrator, auth, db, settings):
    """Folders shared in from an external org are mirrored, recursively."""
    settings.migrate_external_shares = True
    src = auth.source_drive(SRC_USER)
    folder = src.add_folder("ext-proj")
    src.store[folder]["owners"] = [{"emailAddress": "external@partner.com"}]
    src.perms[folder].append({"id": "p1", "type": "user", "role": "reader",
                              "emailAddress": SRC_USER})
    child = src.add_binary("child.pdf", parent=folder)
    src.store[child]["owners"] = [{"emailAddress": "external@partner.com"}]
    src.perms[child].append({"id": "p1", "type": "user", "role": "reader",
                             "emailAddress": SRC_USER})

    migrator.run()

    assert db.get_target_id(SRC_USER, folder, "folder")
    assert db.get_target_id(SRC_USER, child, "file")


def test_external_share_is_idempotent_across_runs(migrator, auth, db, settings):
    """Re-running must not duplicate an external-shared file (id_mapping
    dedupes by source id, exactly like owned files)."""
    settings.migrate_external_shares = True
    src = auth.source_drive(SRC_USER)
    theirs = src.add_binary("their-deck.pptx")
    src.store[theirs]["owners"] = [{"emailAddress": "external@partner.com"}]
    src.perms[theirs].append({"id": "p1", "type": "user", "role": "reader",
                              "emailAddress": SRC_USER})

    migrator.run()
    first = db.get_target_id(SRC_USER, theirs, "file")
    files_before = migrator.stats["files"]
    migrator.run()

    assert first
    assert db.get_target_id(SRC_USER, theirs, "file") == first
    assert migrator.stats["files"] == files_before, \
        "resume must not copy it again"


class TestIntraUserFileConcurrency:
    """
    `drive_file_workers` parallelises the files inside one folder.

    It exists because the batch cannot finish before its slowest single
    user, and that user is one thread: measured on the live tenant, a user
    thread runs at 0.66 req/s against Google's 3 sustained writes/sec
    *per account* ceiling, so ~4.6x of that account's budget goes unused
    and no amount of extra `user_workers` can reach it.
    """

    def _tree(self, auth, n_files: int) -> None:
        src = auth.source_drive(SRC_USER)
        for i in range(n_files):
            src.add_binary(f"f{i}.bin")

    def test_default_is_parallel(self, migrator, auth, settings):
        """The default is parallel, not 1.

        It shipped as 1 so that deploying the parallel path mid-benchmark
        was a no-op, and then stayed 1 -- every run since went at ~0.66
        req/s per user against a ceiling of 3. Asserting `> 1` rather than a
        literal because the value is machine-derived (halved under memory
        pressure); what must not regress is that it is concurrent at all.
        """
        assert settings.drive_file_workers > 1
        self._tree(auth, 5)
        migrator.run()
        assert migrator.stats["files"] == 5
        assert migrator.stats["failed"] == 0

    def test_default_is_four_on_a_healthy_host(self):
        """4 is where the write ceiling binds; under memory pressure it
        halves, because download_upload's peak buffer is
        user_workers x drive_file_workers x download_chunk_bytes."""
        import resources
        rec = resources.recommend()
        healthy = not rec["resources"].under_memory_pressure
        assert rec["drive_file_workers"] == (4 if healthy else 2)

    def test_one_worker_is_still_a_clean_serial_path(self, migrator, auth, settings):
        """DRIVE_FILE_WORKERS=1 is the documented escape hatch, so it has to
        keep working: no pool, no semaphore, same result."""
        settings.drive_file_workers = 1
        self._tree(auth, 5)
        migrator.run()
        assert migrator.stats["files"] == 5
        assert migrator.stats["failed"] == 0

    def test_parallel_copies_every_file_exactly_once(self, migrator, auth, settings):
        settings.drive_file_workers = 4
        self._tree(auth, 12)
        migrator.run()
        assert migrator.stats["files"] == 12
        assert migrator.stats["failed"] == 0
        # Exactly-once is the property that matters: a double-copy would
        # duplicate real user data on the target.
        tgt = auth.target_drive(TGT_USER)
        names = [f["name"] for f in tgt.store.values()
                 if f.get("mimeType") != FOLDER_MIME]
        assert sorted(names) == sorted(f"f{i}.bin" for i in range(12))

    def test_parallel_result_matches_serial_result(self, migrator, auth, settings):
        """The whole point: same outcome, less wall clock."""
        settings.drive_file_workers = 8
        self._tree(auth, 20)
        migrator.run()
        assert migrator.stats == {"folders": 0, "files": 20, "skipped": 0,
                                  "failed": 0, "acl_failed": 0}

    def test_stats_are_not_lost_under_concurrent_increment(self, migrator, auth,
                                                           settings):
        """`d[k] += 1` is not atomic. Without the lock this undercounts, and
        it would undercount the failure counters the run is judged on."""
        settings.drive_file_workers = 8
        self._tree(auth, 40)
        migrator.run()
        assert migrator.stats["files"] == 40

    def test_files_from_different_folders_run_concurrently(self, migrator, auth,
                                                           settings):
        """The pool spans the whole walk, not one folder.

        A per-folder pool blocked until that folder drained and skipped
        parallelism entirely for a folder holding fewer files than there are
        workers -- so a tree of single-file folders, which is an ordinary
        shape, ran fully serially no matter how many workers were configured.

        Deterministic rather than timing-based: the barrier can only trip if
        two tasks from two different folders are genuinely in flight at once,
        and times out into BrokenBarrierError if they are serialised.
        """
        import threading as _t

        settings.drive_file_workers = 4
        src = auth.source_drive(SRC_USER)
        for i in range(4):
            folder = src.add_folder(f"d{i}")
            src.add_binary(f"f{i}.bin", parent=folder)

        barrier = _t.Barrier(2, timeout=10)
        original = migrator._sync_file

        def _rendezvous(item, tgt_parent):
            barrier.wait()          # raises BrokenBarrierError if never paired
            return original(item, tgt_parent)

        migrator._sync_file = _rendezvous
        migrator.run()
        assert migrator.stats["files"] == 4
        assert migrator.stats["failed"] == 0

    def test_quota_exhaustion_still_aborts_the_user(self, migrator, auth,
                                                    settings, quota):
        """On the serial path QuotaExhausted propagates and halts the user.
        Parallelism must not downgrade it to a per-file failure."""
        settings.drive_file_workers = 4
        self._tree(auth, 6)
        quota.cap_bytes = 0
        with pytest.raises(QuotaExhausted):
            migrator.run()


class TestClientsAreResolvedPerThread:
    """
    `httplib2.Http` is not thread-safe. AuthManager._service caches per
    thread for exactly that reason, and the engine used to defeat it by
    capturing `self.src`/`self.tgt` in __init__ -- so every file-pool thread
    drove the walk thread's socket.

    It was invisible while drive_file_workers defaulted to 1. The first real
    run at 4 died in 17s with `free(): invalid next size (normal)`: glibc
    heap corruption, SIGABRT, no Python traceback, 0 files migrated, and a
    benchmark that still printed PASS.
    """

    def test_clients_are_not_captured_on_the_instance(self, migrator):
        """Each access must go back through auth, because that is where the
        per-thread cache lives."""
        seen: dict[str, int] = {}

        class CountingAuth:
            def source_drive(self, user):
                seen["src"] = seen.get("src", 0) + 1
                return object()

            def target_drive(self, user):
                seen["tgt"] = seen.get("tgt", 0) + 1
                return object()

        migrator.auth = CountingAuth()
        migrator.src, migrator.tgt = None, None   # clear any override
        for _ in range(3):
            _, _ = migrator.src, migrator.tgt
        assert seen == {"src": 3, "tgt": 3}, \
            "a captured client would have consulted auth once, not per access"

    def test_two_threads_get_two_different_clients(self, migrator):
        """The property is only useful if a per-thread auth actually yields
        a different object per thread -- that is the whole defect."""
        import threading as _t

        local = _t.local()

        class PerThreadAuth:
            def _svc(self):
                if not hasattr(local, "svc"):
                    local.svc = object()
                return local.svc

            source_drive = lambda self, user: self._svc()   # noqa: E731
            target_drive = lambda self, user: self._svc()   # noqa: E731

        migrator.auth = PerThreadAuth()
        migrator.src = None
        # Hold the objects, not their id()s: a dead object's address gets
        # recycled, so comparing ids here reports "same client" for two
        # genuinely different ones.
        got: list = []
        threads = [_t.Thread(target=lambda: got.append(migrator.src))
                   for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert got[0] is not got[1], \
            "both threads shared one client -- that is the heap-corruption bug"


class TestWritePacing:
    """
    The write path was completely unthrottled: `_retry` never touched a
    limiter, and the only `limiter.acquire()` calls sat in `files.list` and
    the download helper. Serial, at ~0.66 req/s per user, nothing noticed.
    With `drive_file_workers > 1` it becomes a 429 generator, so the pacing
    has to be real before the concurrency is usable.
    """

    class _Counting:
        def __init__(self):
            self.n = 0

        def acquire(self):
            self.n += 1

    def test_writes_are_charged_to_the_write_bucket(self, migrator, auth):
        w, r = self._Counting(), self._Counting()
        migrator._write_limiter, migrator._read_limiter = w, r
        auth.source_drive(SRC_USER).add_binary("a.bin")
        migrator.run()
        assert w.n > 0, "the copy/move/create path paid nothing to the write bucket"

    def test_listing_is_not_charged_to_the_write_bucket(self, migrator, auth):
        """Reads come out of the 20,000/100s pool. Charging them against the
        3/sec write ceiling would spend ~22% of it on calls Google prices
        two orders of magnitude cheaper."""
        w, r = self._Counting(), self._Counting()
        migrator._write_limiter, migrator._read_limiter = w, r
        src = auth.source_drive(SRC_USER)
        src.add_folder("empty")
        migrator.run()
        assert r.n > 0, "the tree walk paid nothing to the read bucket"

    def test_write_bucket_defaults_to_googles_ceiling(self):
        """3/sec sustained per account, explicitly not raiseable on request.
        A default above it would just buy 429s and retry backoff."""
        from config import Settings
        assert Settings().drive_write_qps == 3.0

    def test_source_and_target_writes_use_separate_buckets(self, migrator, auth,
                                                           settings):
        """The ceiling is per *account*, and the copy is issued as the source
        user while the move/grant/mtime writes are issued as the target user.

        Charging both to one bucket -- which is what a single `_write_limiter`
        did -- capped the pair at one account's allowance and left the other
        account's identical allowance entirely unspent.

        server_side specifically: it is the only mode with a source-side
        write at all (files.copy). download_upload streams bytes through this
        host and writes solely to the target, which is why the split buys
        nothing there.
        """
        _server_side(settings)
        src_b, tgt_b, r = self._Counting(), self._Counting(), self._Counting()
        migrator._src_write_limiter = src_b
        migrator._write_limiter = migrator._tgt_write_limiter = tgt_b
        migrator._read_limiter = r
        auth.source_drive(SRC_USER).add_binary("a.bin")
        migrator.run()
        assert src_b.n > 0, "files.copy was not charged to the source account"
        assert tgt_b.n > 0, "the move/mtime writes were not charged to the target"

    def test_the_two_write_buckets_are_independent(self, migrator):
        """Aliasing them would silently reintroduce the shared ceiling."""
        assert migrator._src_write_limiter is not migrator._tgt_write_limiter

    def test_reads_are_not_paced_at_the_write_rate(self):
        """Reads come from the 20,000-per-100s pool (~200/sec), not the 3/sec
        write ceiling. They used to inherit `per_user_qps` -- 4/sec, auto-tuned
        down to 3 on a small host -- throttling them ~60x below what Google
        allows while every comment here called them effectively free."""
        from config import Settings
        s = Settings()
        assert s.drive_read_qps > s.drive_write_qps
        assert s.drive_read_qps == 12.0
