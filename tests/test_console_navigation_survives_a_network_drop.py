"""A browser navigation that fails for the network is tried again; anything else is raised as before.

13 Nov 2026 (account 3): one net::ERR_NAME_NOT_RESOLVED during a Wi-Fi drop ended the DMS import job."""
import pytest

import dwd_helper


class Page:
    def __init__(self, failures):
        self.failures, self.calls = list(failures), 0

    def goto(self, url, **kw):
        self.calls += 1
        if self.failures:
            raise RuntimeError(self.failures.pop(0))
        return "loaded"


def test_a_dns_failure_is_retried_until_the_page_loads():
    waits = []
    page = dwd_helper.retry_navigation(Page(["Page.goto: net::ERR_NAME_NOT_RESOLVED at https://admin.google.com/",
                                             "net::ERR_INTERNET_DISCONNECTED"]), sleep=waits.append)
    assert page.goto("https://admin.google.com/", timeout=30000) == "loaded"
    assert page.calls == 3 and waits == [5, 15]


def test_any_other_failure_is_raised_at_once():
    waits = []
    page = dwd_helper.retry_navigation(Page(["Timeout 30000ms exceeded"]), sleep=waits.append)
    with pytest.raises(RuntimeError, match="Timeout"):
        page.goto("https://admin.google.com/")
    assert page.calls == 1 and not waits


def test_a_network_that_stays_down_is_raised_after_the_last_wait():
    waits = []
    page = dwd_helper.retry_navigation(Page(["net::ERR_NAME_NOT_RESOLVED"] * 10), sleep=waits.append)
    with pytest.raises(RuntimeError, match="ERR_NAME_NOT_RESOLVED"):
        page.goto("https://admin.google.com/")
    assert waits == list(dwd_helper.NETWORK_BACKOFF) and page.calls == len(dwd_helper.NETWORK_BACKOFF) + 1
