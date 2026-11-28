"""Every kind of one-to-one finding Bitport has met is named, with its cause and its fix."""
import pytest

import known_issues


class TestEveryFindingSeenOnTheSandboxHasAKind:
    """s.sarafgloabalexim.com -> t. (2026-11-01): each of these read "unclassified" until it was
    traced to its cause. A finding Bitport cannot name is one it cannot fix or explain."""

    @pytest.mark.parametrize("service, item, kind", [
        ("gmail", {"notCopied": {"error": "Pre-delivery link scan: HTTP 404 (notFound): <HttpError 404"}}, "gmail.link_scan_fixed"),
        ("gmail", {"notCopied": {"error": "Pre-delivery link scan: Multipart body has no readable parts"}}, "gmail.link_scan_fixed"),
        ("gmail", {"notCopied": {"error": "Pre-delivery link scan: Unresolved Drive link mapping: 11q"}}, "gmail.link_owner_not_migrated"),
        ("gmail", {"notCopied": {"error": "Pre-delivery link scan: Encrypted MIME content cannot be classified"}}, "gmail.link_scan_encrypted"),
        ("gmail", {"target": "19ea", "messageId": "<x@mail.gmail.com>"}, "gmail.duplicate"),
        ("gmail", "could not read labels: exhausted 9 retries on HTTP 429 (rateLimitExceeded)", "gmail.rate_limited"),
        ("drive", "file 1: content could not be compared: exhausted 6 retries on HTTP 503 (UNAVAILABLE)", "drive.read_refused"),
        ("drive", "file 1: content could not be compared: exhausted 6 retries on HTTP 429 (RESOURCE_EXHAUSTED)", "drive.read_refused"),
        ("drive", "Comment threads: HTTP 404 (notFound): <HttpError 404", "drive.comments_unreadable"),
        ("drive", {"source": "a", "target": "b", "why": "not on the target (<HttpError 404"}, "drive.missing_on_target"),
        ("contacts", "2 source contact(s) have no ledger mapping; the target cannot be matched safely", "contacts.unmapped_source"),
        ("contacts", {"source": "people/c1", "why": "not on the target (Requested entity was not found.)"}, "contacts.missing_on_target"),
        ("chat", {"notCopied": {"error": "Source Chat space has no migration mapping"}}, "chat.not_migrated"),
        ("tasks", "task list 'My Tasks': HTTP 404 (notFound): Task list not found.", "tasks.list_missing"),
        ("drive", {"notCopied": {"type": "file", "id": "s1",
                                    "copyResolution": {"contentScope": "Size and MD5 matched; the file was not downloaded"}}},
         "drive.copy_unrecorded"),
        ("drive", {"notCopied": {"type": "external_access", "id": "s1",
                                    "copyResolution": {"contentScope": "Size and MD5 matched; the file was not downloaded"}}},
         "drive.external_copy"),
        ("drive", "Repair remains incomplete: file-1: external original shortcut missing or placement differs",
         "drive.external_copy"),
        ("drive", "Repair remains incomplete: file-1: Retained fallback has other grants; review is required before accepting original-reference mode",
         "drive.external_copy_review"),
        ("drive", "native Sheet structures differ; sheets[0].protectedRanges[0].editors: extra on target",
         "drive.native_structure"),
        ("drive", "Repair remains incomplete: file-1: Non-cell native Sheet features differ; a lossless structure repair is required",
         "drive.native_structure"),
        ("drive", "Form title differs", "drive.form_title"),
        ("gmail", {"notCopied": {"error": "Existing destination Message-ID has different content; manual review required",
                                   "id": "<hex>", "type": "message"}}, "gmail.reset_leftover"),
        ("drive", "script project differs: 2 file(s) -> 3, or their code", "drive.script_project"),
        ("drive", "2 file-upload question(s) not carried: no API can create one", "drive.form_upload_question"),
        ("drive", "Repair remains incomplete: <id>: Sheet tabs differ; lossless tab repair must preserve existing gid references",
         "drive.sheet_tabs"),
        ("drive", "Repair remains incomplete: <id>: Visible source parent has no mapped target folder",
         "drive.external_copy"),
    ])
    def test_it_is_named(self, service, item, kind):
        assert known_issues.classify(service, item) == kind

    def test_a_kind_marked_automatic_says_what_fixes_it(self):
        for issue_id, _svc, _m, _title, _cause, fix, auto in known_issues.ISSUES:
            if auto:
                assert fix and fix.strip(), issue_id


def test_not_copied_run_failure_kind_is_visible_in_finding_summary(db, identity):
    import verification_repairs
    item = {"error": "Existing destination Message-ID has different content; manual review required",
            "id": "<hex>", "type": "message"}
    verification_repairs.enqueue_findings(db, "alice@tenanta.com", {"gmail": {"notCopied": [item]}})

    row = db.conn.execute("SELECT issue,state FROM verification_repair_queue WHERE item_id='<hex>'").fetchone()
    kinds = {kind["id"]: kind for kind in known_issues.summary(db.conn)["kinds"]}

    assert row["issue"] == "gmail.reset_leftover" and row["state"] == "QUEUED"
    assert kinds["gmail.reset_leftover"]["openByCategory"] == {"notCopied": 1}
    assert "other content" in known_issues.user_reason(db.conn, "alice@tenanta.com")


def test_not_copied_failure_reclassification_version_covers_existing_rows(db, identity):
    import verification_repairs
    item = {"error": "Existing destination Message-ID has different content; manual review required",
            "id": "<hex>", "type": "message"}
    verification_repairs.enqueue_findings(db, "alice@tenanta.com", {"gmail": {"notCopied": [item]}})
    db.conn.execute("UPDATE verification_repair_queue SET issue='unclassified',issue_version=2 WHERE item_id='<hex>'")

    known_issues.ensure(db.conn)

    row = db.conn.execute("SELECT issue,issue_version FROM verification_repair_queue WHERE item_id='<hex>'").fetchone()
    assert row["issue"] == "gmail.reset_leftover" and row["issue_version"] == known_issues.CLASSIFIER_VERSION


def test_a_finding_with_both_a_missing_grant_and_a_moved_time_is_the_grant():
    import known_issues as K
    t = '{"diffs": ["modified time not preserved: a -> b", "grant missing on the target: [\'user\', \'x@y\', \'reader\']"]}'
    assert K.classify("drive", t) == "drive.grant_missing"
    t2 = '{"diffs": ["MD5 differs: a -> b", "modified time not preserved: a -> b"]}'
    assert K.classify("drive", t2) == "drive.content_changed"
