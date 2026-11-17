"""An operator-commanded watcher answers one run's Google notices, and only the ones it is told to."""
import json
import time

import approve_setup_notices as A
import setup_notices


def _write(tmp_path, **handoff):
    p = tmp_path / "full-setup-source.json.progress"
    base = {"kind": "review", "requestId": "r1", "noticeHash": "h1", "adminEmail": "info@a.example",
            "category": "cloud_console_terms", "expiresAt": time.time() + 600}
    p.write_text(json.dumps({"pct": 5, "handoff": {**base, **handoff}}))
    return str(p)


def test_a_named_category_for_the_named_domain_is_approved_once(tmp_path):
    path, seen, log = _write(tmp_path), set(), []
    assert A.step(path, "a.example", {"cloud_console_terms"}, seen, log.append) == "approved"
    resp = json.loads(open(path + ".notice-response").read())
    assert resp["decision"] == "approve" and resp["requestId"] == "r1" and resp["noticeHash"] == "h1"
    assert A.step(path, "a.example", {"cloud_console_terms"}, seen, log.append) is None      # same notice: once


def test_another_category_or_another_domain_is_never_approved(tmp_path):
    path = _write(tmp_path, category="billing_setup")
    assert A.step(path, "a.example", {"cloud_console_terms"}, set(), lambda m: None) == "refused"
    path = _write(tmp_path, adminEmail="info@elsewhere.example", requestId="r2")
    assert A.step(path, "a.example", {"cloud_console_terms"}, set(), lambda m: None) == "refused"
    assert not (tmp_path / "full-setup-source.json.progress.notice-response").exists()


def test_a_government_domain_is_refused_outright():
    assert A.main(["--account-id", "1", "--side", "source", "--domain", "dharan.gov.np"]) == 2


def test_an_expired_notice_is_not_answered(tmp_path):
    path = _write(tmp_path, expiresAt=time.time() - 5)
    assert A.step(path, "a.example", {"cloud_console_terms"}, set(), lambda m: None) is None
