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
    ])
    def test_it_is_named(self, service, item, kind):
        assert known_issues.classify(service, item) == kind

    def test_a_kind_marked_automatic_says_what_fixes_it(self):
        for issue_id, _svc, _m, _title, _cause, fix, auto in known_issues.ISSUES:
            if auto:
                assert fix and fix.strip(), issue_id
