import known_issues


def test_a_dns_failure_is_named():
    """P3.7 run 11: a link repoint failed 'Unable to find the server at docs.googleapis.com'."""
    assert known_issues.classify_failure("link_rewrite", "Unable to find the server at docs.googleapis.com") == "run.dns"
