import known_issues


def test_a_dns_failure_is_named():
    """P3.7 run 11: a link repoint failed 'Unable to find the server at docs.googleapis.com'."""
    assert known_issues.classify_failure("link_rewrite", "Unable to find the server at docs.googleapis.com") == "run.dns"


def test_a_google_5xx_through_every_retry_is_named():
    """P3.7 run 13: one Sheet's link check, Sheets API 503 six times."""
    t = "exhausted 6 retries on HTTP 503 (UNAVAILABLE): <HttpError 503 when requesting https://sheets.googleapis.com/v4/"
    assert known_issues.classify_failure("link_rewrite", t) == "run.google_unavailable"
    assert known_issues.classify_failure("file", "exhausted 5 retries on HTTP 500 internalError") == "run.backend_500"
