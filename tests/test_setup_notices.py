import json
import os
from types import SimpleNamespace

import pytest

import setup_notices as notices

EMAIL = "nest@target.example"
URL = "https://accounts.google.com/v3/signin/speedbump/workspacetermsofservice?state=private"
BODY = f"Loading\nWelcome\nWelcome to your new account\n{EMAIL}. Your administrator manages account data.\nI understand\nHelp"


class Page:
    url = URL
    body = BODY
    clicked = 0
    def inner_text(self, selector): return self.body
    def get_by_role(self, role, *, name, exact):
        assert (role, name, exact) == ("button", "I understand", True)
        return self
    def count(self): return 1
    def is_visible(self): return True
    def is_enabled(self): return True
    def click(self): self.clicked += 1


def test_welcome_binds_the_google_origin_account_and_screen():
    value = notices.detect(URL, BODY, EMAIL)
    assert value["category"] == "workspace_welcome"
    assert "private" not in json.dumps(value)
    assert notices.detect(URL, BODY, "other@target.example") is None
    assert notices.detect(URL, BODY.replace(EMAIL, EMAIL + ".evil"), EMAIL) is None


@pytest.mark.parametrize("url", [
    URL.replace("https:", "http:"), URL.replace("accounts.google.com", "accounts.google.com.evil"),
    URL.replace("accounts.google.com", "accounts.google.com:444"), "https://example.test/notice",
])
def test_untrusted_locations_are_never_approval_candidates(url):
    assert notices.detect(url, BODY, EMAIL) is None


def test_cloud_sdk_scope_screen_is_its_own_distinct_review():
    body = f"Google Cloud SDK wants access to your Google Account\n{EMAIL}\nThis will allow Google Cloud SDK to:\nManage Compute resources\nMake sure you trust Google Cloud SDK"
    url = "https://accounts.google.com/signin/oauth/v3/consent?client_id=" + notices.SDK_CLIENT
    found = notices.detect(url, body, EMAIL)
    assert found["category"] == "cloud_sdk_access" and found["buttonLabel"] == "Allow"
    assert "Manage Compute resources" in found["text"]
    assert notices.detect(url.replace(notices.SDK_CLIENT, "other-app"), body, EMAIL) is None
    assert notices.detect(URL, "I agree to something unrelated " + EMAIL, EMAIL) is None


def test_requests_are_distinct_even_for_identical_notices():
    notice = notices.detect(URL, BODY, EMAIL)
    assert notices.request(notice)["requestId"] != notices.request(notice)["requestId"]


def test_a_notice_too_large_to_show_in_full_is_not_an_approval_candidate():
    assert notices.detect(URL, BODY.replace('I understand', 'new term ' * 2000 + 'I understand'), EMAIL) is None


def run_review(tmp_path, response=None, change=False):
    page = Page(); events = []; logs = []; clock = [0]
    path = tmp_path / "response"
    pending = {}
    def publish(value):
        events.append(value)
        if value: pending.update(value)
    def sleep(_):
        clock[0] += 1
        if response and clock[0] == 1:
            reply = {k: pending[k] for k in ("requestId", "noticeHash", "adminEmail")}
            reply["decision"] = response
            if response == "wrong": reply["requestId"] = "0" * 48
            path.write_text(json.dumps(reply))
            if change: page.body = BODY.replace("manages", "can delete")
    result = notices.await_review(page, EMAIL, SimpleNamespace(poll=lambda: None),
        publish, path, 3, logs.append, clock=lambda: clock[0], sleep=sleep)
    return page, result, events, path, logs


@pytest.mark.parametrize("response,expected,clicks", [
    (None, "expired", 0), ("approve", "approved", 1), ("decline", "declined", 0),
    ("wrong", "expired", 0),
])
def test_only_an_explicit_matching_single_use_reply_can_click(tmp_path, response, expected, clicks):
    page, result, events, path, _ = run_review(tmp_path, response)
    assert result == expected and page.clicked == clicks
    assert events[-1] is None and not path.exists()


def test_a_changed_notice_invalidates_approval_before_the_click(tmp_path):
    page, result, *_ = run_review(tmp_path, "approve", change=True)
    assert result == "changed" and page.clicked == 0


def test_stale_response_from_previous_run_is_removed(tmp_path):
    (tmp_path / "response").write_text('{"decision":"approve"}')
    page, result, *_ = run_review(tmp_path)
    assert result == "expired" and page.clicked == 0


def test_decision_writer_is_exclusive_private_and_tied_to_live_progress(tmp_path):
    request = notices.request(notices.detect(URL, BODY, EMAIL))
    progress = tmp_path / "progress"
    progress.write_text(json.dumps({"handoff": request}))
    notices.write_decision(str(progress), request["requestId"], request["noticeHash"], "approve")
    assert os.stat(str(progress) + ".notice-response").st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="already"):
        notices.write_decision(str(progress), request["requestId"], request["noticeHash"], "approve")


@pytest.mark.parametrize("change", ["requestId", "noticeHash", "expiresAt", "kind"])
def test_changed_or_expired_progress_cannot_accept_a_decision(tmp_path, change):
    original = notices.request(notices.detect(URL, BODY, EMAIL))
    current = dict(original)
    current[change] = 0 if change == "expiresAt" else "changed"
    progress = tmp_path / "progress"; progress.write_text(json.dumps({"handoff": current}))
    with pytest.raises(ValueError, match="changed or expired"):
        notices.write_decision(str(progress), original["requestId"], original["noticeHash"], "approve")
    assert not (tmp_path / "progress.notice-response").exists()
