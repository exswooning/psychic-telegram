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


def test_inherited_permissions_are_not_duplicated_per_file(migrator, auth):
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
