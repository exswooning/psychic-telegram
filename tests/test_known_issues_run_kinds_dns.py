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
