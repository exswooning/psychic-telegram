import known_issues


def test_a_dns_failure_is_named():
    """P3.7 run 11: a link repoint failed 'Unable to find the server at docs.googleapis.com'."""
    assert known_issues.classify_failure("link_rewrite", "Unable to find the server at docs.googleapis.com") == "run.dns"


def test_a_google_5xx_through_every_retry_is_named():
    """P3.7 run 13: one Sheet's link check, Sheets API 503 six times."""
    t = "exhausted 6 retries on HTTP 503 (UNAVAILABLE): <HttpError 503 when requesting https://sheets.googleapis.com/v4/"
    assert known_issues.classify_failure("link_rewrite", t) == "run.google_unavailable"
    assert known_issues.classify_failure("file", "exhausted 5 retries on HTTP 500 internalError") == "run.backend_500"


def test_a_rendered_page_or_slide_difference_is_the_native_round_trip():
    """fp2-live, 24 Nov 2026: a download_upload twin's Doc read 'page 1 renders differently' and was filed unclassified."""
    import known_issues
    assert known_issues.classify("drive", "page 1 renders differently") == "drive.native_structure"
    assert known_issues.classify("drive", "slide 3 renders differently") == "drive.native_structure"


def test_an_outsiders_file_delivered_as_a_copy_is_one_accepted_kind():
    """dharan.gov.np, 23 Nov 2026: 1,132 findings on 42 users; the operator accepted them on 24 Nov."""
    import known_issues, parity_gate
    for text in ('{"type": "external_access", "id": "X"}', "external original shortcut missing or placement differs",
                 "sharing of X: original access inspection failed: Visible source parent has no mapped target folder"):
        assert known_issues.classify("drive", text) == "drive.external_copy", text
    for text in ("external fallback has unreviewed sharing",
                 "Repair remains incomplete: X: Retained fallback has other grants; review is required"):
        assert known_issues.classify("drive", text) == "drive.external_copy_review", text
    combined = '{"diffs": ["external original shortcut missing or placement differs", "external fallback has unreviewed sharing"]}'
    assert known_issues.classify("drive", combined) == "drive.external_copy"
    assert "drive.external_copy" in parity_gate.ACCEPTED_KINDS


def test_the_gate_can_judge_one_service():
    """A Drive-only migration (dharan.gov.np) is judged on Drive; its other services were never part of it."""
    import sqlite3, parity_gate
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE identity_map (source_email TEXT, entity_type TEXT)")
    c.execute("INSERT INTO identity_map VALUES ('u@a.example','user')")
    c.execute("CREATE TABLE user_verification (source_user TEXT, service TEXT, verdict TEXT, verified_at TEXT, payload TEXT)")
    c.execute("INSERT INTO user_verification VALUES ('u@a.example','drive','IDENTICAL','2026-11-24T00:00:00Z','{}')")
    assert parity_gate.assess(c, services=("drive",))["verdict"] == "PARITY"
    assert parity_gate.assess(c)["verdict"] != "PARITY"


def test_a_lost_reply_is_named():
    """fp2-live, 24 Nov 2026: the comment failpoint's FAILED row read unclassified (incident #299)."""
    import known_issues
    for t in ("failpoint drive.comment.created@1: reply lost", "[Errno 104] Connection reset by peer",
              "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))"):
        assert known_issues.classify_failure("comment", t) == "run.reply_lost", t


def test_gmail_source_gone_and_manual_recovery_conflicts_are_named_with_correct_automation():
    gone = "Gmail raw read <id>: HTTP 404 notFound: message not found"
    ambiguous = "Ambiguous unclaimed destination Message-ID; recovery blocked"
    leftover = "Existing destination Message-ID has different content; manual review required"
    assert known_issues.classify_failure("message", gone) == "gmail.source_gone"
    assert known_issues.classify_failure("message", ambiguous) == "gmail.destination_ambiguous"
    assert known_issues.classify_failure("message", leftover) == "gmail.reset_leftover"
    by_id = {row[0]: row for row in known_issues.RUN_KINDS}
    assert by_id["gmail.source_gone"][6] is True
    assert by_id["gmail.destination_ambiguous"][6] is False
    assert by_id["gmail.reset_leftover"][6] is False
    assert "inspect the target messages" in by_id["gmail.destination_ambiguous"][5]
    assert "compare the source and destination raw messages" in by_id["gmail.reset_leftover"][5]
