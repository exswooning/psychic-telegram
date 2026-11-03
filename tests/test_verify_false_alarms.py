"""Live on gorkhatweed.com: three one-to-one 'differences' that were the checker's own."""
import verify_sample as V


def test_the_same_moment_in_two_time_zones_is_equal():
    a = {"start": {"dateTime": "2026-03-06T14:00:00+05:45"}, "end": {"dateTime": "2026-03-06T14:30:00+05:45"}}
    b = {"start": {"dateTime": "2026-03-06T08:15:00Z"}, "end": {"dateTime": "2026-03-06T08:45:00Z"}}
    assert V.compare_event(a, b, lambda e: e) == []
    b["start"]["dateTime"] = "2026-03-06T08:16:00Z"
    assert V.compare_event(a, b, lambda e: e)


def test_gmail_superstars_are_not_a_difference():
    assert V.compare_labels({"INBOX", "YELLOW_STAR"}, {"INBOX"}) == []
    assert V.compare_labels({"INBOX", "Work"}, {"INBOX"})


def test_an_empty_source_reads_identical_and_an_unchecked_one_does_not():
    class DB:
        rows = []
        def save_user_verification(self, *a): self.rows.append(a)
    base = {"checked": 0, "identical": 0, "differences": [], "missing": [], "duplicates": [],
            "extras": [], "notCopied": [], "errors": [], "notes": []}
    db = DB()
    V.save_user_results(db, {"users": {"u": {"tasks": dict(base, emptySource=True)}}})
    V.save_user_results(db, {"users": {"u": {"tasks": dict(base)}}})
    assert [r[2] for r in db.rows] == ["IDENTICAL", "INCOMPLETE"]


def test_zero_item_service_reads_empty_not_identical(tmp_path):
    import db as D
    rows = [{"source_user": "u", "service": s, "verified_at": "t", "verdict": "IDENTICAL", "checked": c,
             "identical": c, "sampled_of": None, "payload": '{"scopeVersion": 2}'}
            for s, c in (("tasks", 0), ("drive", 5))]
    parsed = D.parse_user_verification_rows(rows)
    assert {r["service"]: r["verdict"] for r in parsed} == {"tasks": "EMPTY", "drive": "IDENTICAL"}
    users = [{"source_email": "u", "target_email": "t", "status": "DONE", "services_done": ""}]
    assert D.verification_rollup(users, parsed)["users"][0]["verdict"] == "IDENTICAL"
    assert D.verification_rollup(users, parsed[:1])["users"][0]["verdict"] == "EMPTY"
