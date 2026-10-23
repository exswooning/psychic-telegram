"""Every kind of finding is tagged; when a fresh, complete check no longer finds it,
one line records that it was fixed and how -- the durable answer, per kind."""
import known_issues
import verification_repairs as R
from tests.conftest import SRC_USER


def _gmail(diffs, checked=3):
    return {"gmail": {"checked": checked, "identical": checked - len(diffs),
                      "differences": [{"source": f"s{i}", "target": f"t{i}", "diffs": [d]} for i, d in enumerate(diffs)],
                      "missing": [], "duplicates": [], "notCopied": [], "errors": [], "extras": [], "notes": []}}


def test_each_live_finding_is_given_its_kind():
    assert known_issues.classify("gmail", {"diffs": ["labels added on the target: ['INBOX']"]}) == "gmail.labels_added"
    assert known_issues.classify("tasks", {"diffs": ["title: None -> '(untitled)'"]}) == "tasks.untitled"
    assert known_issues.classify("drive", {"diffs": ["grant missing on the target: ['group', 'x@a', 'writer']"]}) == "drive.group_grant"
    assert known_issues.classify("drive", {"diffs": ["grant missing on the target: ['user', 'x@a', 'reader']"]}) == "drive.grant_missing"
    assert known_issues.classify("drive", "file 1: HTTP 403 ACCESS_TOKEN_SCOPE_INSUFFICIENT") == "drive.sheets_scope"
    assert known_issues.classify("drive", {"diffs": ["something new"]}) == "unclassified"


def test_a_finding_a_later_check_no_longer_finds_is_logged_as_fixed_with_how(db):
    R.enqueue_findings(db, SRC_USER, _gmail(["labels added on the target: ['INBOX']"]))
    row = db.conn.execute("SELECT issue, state FROM verification_repair_queue").fetchone()
    assert (row["issue"], row["state"]) == ("gmail.labels_added", "QUEUED")
    R.enqueue_findings(db, SRC_USER, _gmail([]))                 # fixed: a clean, complete check
    log = db.conn.execute("SELECT issue, source_user, how FROM issue_fix_log").fetchall()
    assert [(r["issue"], r["source_user"]) for r in log] == [("gmail.labels_added", SRC_USER)]
    assert "Remove the extra labels" in log[0]["how"]
    kinds = {k["id"]: k for k in known_issues.summary(db.conn)["kinds"]}
    assert (kinds["gmail.labels_added"]["open"], kinds["gmail.labels_added"]["fixed"]) == (0, 1)
    assert kinds["gmail.labels_added"]["auto"] is True


def test_a_sample_never_marks_anything_fixed(db):
    R.enqueue_findings(db, SRC_USER, _gmail(["labels added on the target: ['INBOX']"]))
    sampled = _gmail([])
    sampled["gmail"]["sampled"] = {"checked": 3, "of": 300}
    R.enqueue_findings(db, SRC_USER, sampled)
    assert db.conn.execute("SELECT COUNT(*) FROM issue_fix_log").fetchone()[0] == 0
