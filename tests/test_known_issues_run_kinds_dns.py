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
                 "external fallback has unreviewed sharing",
                 "sharing of X: original access inspection failed: Visible source parent has no mapped target folder"):
        assert known_issues.classify("drive", text) == "drive.external_copy", text
    assert "drive.external_copy" in parity_gate.ACCEPTED_KINDS
