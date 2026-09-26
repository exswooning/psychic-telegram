"""The tally compares two tenants directly. What matters is that it cannot
flatter a run: a user with no target account counts against parity, a check
that errored is unknown (never a pass), deliberate skips are not missing, and
the WORST service sets the headline.
"""
from __future__ import annotations

import pytest

import tally as T
from db import bulk_seed_identities


def side(counts=None, errors=None):
    return {"counts": counts or {}, "errors": errors or {}}


def row(user, src, tgt, skipped=None, src_err=None, tgt_err=None):
    return {"user": user, "target_user": user.replace("@a.", "@b."), "skipped": skipped or {},
            "source": side(src, src_err), "target": side(tgt, tgt_err)}


class TestAggregate:
    def test_matching_tenants_are_full_parity(self):
        agg = T.aggregate([row("u@a.com", {"mail": 10, "drive_files": 5}, {"mail": 10, "drive_files": 5})])
        assert agg["services"]["mail"]["parity"] == 1.0 and agg["countParity"] == 1.0

    def test_deliberate_skips_are_not_missing(self):
        agg = T.aggregate([row("u@a.com", {"drive_files": 10}, {"drive_files": 7}, skipped={"drive_files": 3})])
        s = agg["services"]["drive_files"]
        assert (s["expected"], s["parity"], agg["worst"]) == (7, 1.0, [])

    def test_the_worst_service_sets_the_headline(self):
        """A perfect Drive does not excuse missing mail."""
        agg = T.aggregate([row("u@a.com", {"drive_files": 100, "mail": 100}, {"drive_files": 100, "mail": 50})])
        assert agg["countParity"] == 0.5

    def test_a_user_with_no_target_account_counts_against_parity(self):
        """Dropping them would let a run that never moved 99 mailboxes score 100%."""
        rows = [row("a@a.com", {"mail": 50}, {"mail": 50}),
                row("b@a.com", {"mail": 50}, {}, tgt_err={"mail": "invalid_grant: Invalid email or User ID"})]
        agg = T.aggregate(rows)
        assert agg["services"]["mail"]["parity"] == 0.5
        assert agg["usersUnreachable"] == ["b@a.com"]

    def test_an_unclassified_error_is_unknown_not_zero_and_not_a_pass(self):
        rows = [row("a@a.com", {"mail": 50}, {"mail": 50}),
                row("b@a.com", {"mail": 50}, {}, tgt_err={"mail": "timed out"})]
        agg = T.aggregate(rows)
        s = agg["services"]["mail"]
        assert s["parity"] == 1.0 and s["usersCompared"] == 1 and s["usersUnknown"] == 1
        assert agg["usersUnknown"][0]["user"] == "b@a.com"

    def test_a_source_that_could_not_be_counted_is_excluded(self):
        agg = T.aggregate([row("a@a.com", {}, {"mail": 3}, src_err={"mail": "boom"})])
        assert agg["services"]["mail"]["parity"] is None and agg["countParity"] is None

    def test_a_surplus_is_reported_but_never_lifts_parity_above_one(self):
        agg = T.aggregate([row("a@a.com", {"mail": 10}, {"mail": 14})])
        assert agg["services"]["mail"]["parity"] == 1.0 and agg["services"]["mail"]["surplus"] == 4

    def test_the_users_furthest_from_parity_are_listed_worst_first(self):
        agg = T.aggregate([row("a@a.com", {"mail": 10}, {"mail": 9}), row("b@a.com", {"mail": 10}, {"mail": 2})])
        assert [w["user"] for w in agg["worst"]] == ["b@a.com", "a@a.com"] and agg["worst"][0]["missing"] == 8

    def test_nothing_measured_is_none_not_perfect(self):
        assert T.aggregate([])["countParity"] is None

    @pytest.mark.parametrize("err,kind", [("invalid_grant: Invalid email or User ID", "absent"),
                                          ("Mail service not enabled", "absent"),
                                          ("The read operation timed out", "unknown")])
    def test_classify(self, err, kind):
        assert T.classify(err) == kind


class TestDeep:
    def _check(self, name, ok=True, detail="", s=None, t=None):
        return {"name": name, "ok": ok, "detail": detail, "source_value": s, "target_value": t}

    def test_nothing_sampled_leaves_everything_unknown(self):
        out = T.aggregate_deep([{"user": "u", "acl": None, "checks": []}])
        assert out["aclFidelity"] is None and out["checksumFailures"] is None and out["timestampsPreserved"] is None

    def test_checksums_and_timestamps(self):
        r = {"user": "u", "acl": None, "checks": [
            self._check("drive.checksum_sample", False, "25 sampled, 2 mismatched", 25, 2),
            self._check("drive.modified_time", False, "4 file(s) lost their timestamp")]}
        out = T.aggregate_deep([r])
        assert out["checksumFailures"] == 2 and out["checksumSampled"] == 25
        assert out["timestampsPreserved"] == pytest.approx(1 - 4 / 20)

    def test_a_clean_sample_reports_zero_failures_not_unknown(self):
        r = {"user": "u", "acl": None, "checks": [self._check("drive.checksum_sample", True, "", 25, 0)]}
        assert T.aggregate_deep([r])["checksumFailures"] == 0

    def test_acl_fidelity_is_matched_over_expected_and_extras_are_summed(self):
        acl = lambda src, ok, extra: {"grants_source": src, "grants_matched": ok, "extra_grants": extra,  # noqa: E731
                                      "missing_files": 0}
        out = T.aggregate_deep([{"user": "a", "acl": acl(10, 9, 0), "checks": []},
                                {"user": "b", "acl": acl(10, 10, 2), "checks": []}])
        assert out["aclFidelity"] == pytest.approx(19 / 20) and out["extraGrants"] == 2

    def test_no_grants_to_compare_is_unknown_not_perfect(self):
        acl = {"grants_source": 0, "grants_matched": 0, "extra_grants": 0, "missing_files": 0}
        assert T.aggregate_deep([{"user": "a", "acl": acl, "checks": []}])["aclFidelity"] is None


class TestCounting:
    class _Pages:
        """A paged list endpoint: .list(...).execute() yields each page in turn."""
        def __init__(self, pages, key):
            self.pages, self.key, self.calls = list(pages), key, []

        def list(self, **kw):
            self.calls.append(kw)
            page = self.pages[len(self.calls) - 1]
            body = {self.key: [{"id": i} for i in range(page[0])]}
            if page[1]:
                body["nextPageToken"] = "next"
            return type("R", (), {"execute": staticmethod(lambda: body)})()

    def test_mail_pages_and_includes_spam_and_trash(self):
        msgs = self._Pages([(500, True), (120, False)], "messages")
        gm = type("G", (), {"users": lambda self: type("U", (), {"messages": lambda s: msgs})()})()
        assert T.count_mail(gm) == 620
        assert all(c["includeSpamTrash"] is True for c in msgs.calls)

    def test_calendar_counts_series_once(self):
        ev = self._Pages([(3, False)], "items")
        cal = type("C", (), {"events": lambda self: ev})()
        assert T.count_calendar(cal) == 3 and ev.calls[0]["singleEvents"] is False


class TestSkips:
    def test_only_deliberate_skips_are_subtracted_and_mapped_to_a_service(self, settings, db):
        for i, (typ, st) in enumerate([("file", "SKIPPED_TOO_LARGE"), ("file", "SKIPPED_X"), ("file", "FAILED"),
                                       ("message", "SKIPPED_Y"), ("acl", "SKIPPED_Z")]):
            db.conn.execute("INSERT INTO audit_log(source_user,item_id,item_type,status) VALUES('u',?,?,?)",
                            (f"i{i}", typ, st))
        db.conn.commit()
        assert T.skipped_by_user(db.conn) == {"u": {"drive_files": 2, "mail": 1}}


class TestRun:
    def _wire(self, db, n=4, done=True):
        bulk_seed_identities(db, [(f"u{i}@tenanta.com", f"u{i}@tenantb.com") for i in range(n)])
        if done:
            db.conn.execute("UPDATE identity_map SET status='DONE'")
            db.conn.commit()

    def test_stores_a_payload_the_report_reads_back(self, settings, db):
        self._wire(db)

        def count(auth, s, sidename, user, retry):
            return side({"mail": 10, "drive_files": 4})
        out = T.run(settings, db, None, deep=False, count_fn=count, progress=lambda *_: None)
        assert out["countParity"] == 1.0 and out["users"]["tallied"] == 4
        assert out["aclFidelity"] is None            # not sampled -> unknown, never perfect
        assert db.latest_fidelity()["countParity"] == 1.0

    def test_users_filter_and_sample_are_honoured(self, settings, db):
        self._wire(db, 5)
        seen = []

        def count(auth, s, sidename, user, retry):
            seen.append((sidename, user))
            return side({"mail": 1, "drive_files": 1})

        class Rep:
            def __init__(s, checks): s.checks = checks

        deep_users = []

        def verify(a, d, s, su, tu, n):
            deep_users.append(su)
            return Rep([])

        out = T.run(settings, db, None, users=["u1@tenanta.com", "u2@tenanta.com"], sample_users=1, deep=True,
                    count_fn=count, verify_fn=verify,
                    audit_fn=lambda *a: {"grants_source": 4, "grants_matched": 4, "extra_grants": 0, "missing_files": 0},
                    progress=lambda *_: None)
        assert {u for sd, u in seen if sd == "source"} == {"u1@tenanta.com", "u2@tenanta.com"}
        assert len(deep_users) == 1 and out["sample"]["users"] == 1 and out["aclFidelity"] == 1.0

    def test_users_that_could_not_be_reached_are_never_sampled(self, settings, db):
        self._wire(db, 2)

        def count(auth, s, sidename, user, retry):
            return side({"mail": 3}) if sidename == "source" else side({}, {"mail": "invalid_grant"})
        picked = []
        out = T.run(settings, db, None, sample_users=2, count_fn=count,
                    verify_fn=lambda *a: picked.append(a) or None, audit_fn=lambda *a: picked.append(a),
                    progress=lambda *_: None)
        assert picked == [] and out["sample"]["users"] == 0
        assert out["countParity"] == 0.0             # 0 of 6 expected made it

    def test_one_users_failed_check_leaves_only_that_check_unknown(self, settings, db):
        self._wire(db, 1)

        def boom(*a):
            raise RuntimeError("quota")
        out = T.run(settings, db, None, count_fn=lambda *a: side({"mail": 1, "drive_files": 1}), verify_fn=boom,
                    audit_fn=lambda *a: {"grants_source": 2, "grants_matched": 2, "extra_grants": 0, "missing_files": 0},
                    progress=lambda *_: None)
        assert out["checksumFailures"] is None and out["aclFidelity"] == 1.0

    def test_a_missing_tasks_scope_does_not_disqualify_a_user_from_the_sample(self, settings, db):
        self._wire(db, 1)
        picked = []

        def count(auth, s, sidename, user, retry):
            return side({"drive_files": 2}, {"tasks": "insufficient scope"})
        T.run(settings, db, None, count_fn=count, verify_fn=lambda *a: picked.append(a) or None,
              audit_fn=lambda *a: {}, progress=lambda *_: None)
        assert len(picked) == 1

    def test_users_still_migrating_are_never_sampled(self, settings, db):
        self._wire(db, 3, done=False)
        picked = []
        out = T.run(settings, db, None, sample_users=3, count_fn=lambda *a: side({"mail": 1, "drive_files": 1}),
                    verify_fn=lambda *a: picked.append(a) or None, audit_fn=lambda *a: picked.append(a),
                    progress=lambda *_: None)
        assert picked == [] and out["sample"]["users"] == 0 and out["aclFidelity"] is None
