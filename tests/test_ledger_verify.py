"""A ledger that claims work the tenant no longer holds.

id_mapping is authoritative: preload_mappings pulls it into memory and
get_target_id consults it before every mutating call, so anything mapped is
skipped on a resume. Nothing ever checked that the target item still exists.

Live 2026-08-21: all 200 target accounts deleted at 17:13, fresh empty ones
created on the same addresses at 17:43. 210,456 files and 240,732 messages
went with the deleted accounts, and the ledger still recorded every one as
SUCCESS -- so the tenant read as migrated and empty simultaneously, and a
re-run would have skipped all of it and reported success in seconds.
"""
import ledger_verify


class FakeDirectory:
    def __init__(self, accounts):
        self.accounts = accounts
        self.calls = 0

    def users(self):
        return self

    def get(self, userKey, fields=None):
        self.calls += 1
        outer = self

        class _Req:
            def execute(self):
                if userKey not in outer.accounts:
                    raise RuntimeError(
                        "<HttpError 404 ... Resource Not Found: userKey>")
                return {"primaryEmail": userKey,
                        "creationTime": outer.accounts[userKey]}
        return _Req()


class FakeDB:
    def __init__(self, bounds, samples=None):
        self.bounds = bounds
        self.samples = samples or {}
        self.forgotten = []

    def mapping_bounds(self, user):
        return self.bounds.get(user)

    def sample_mapping(self, user, item_type="file"):
        return self.samples.get(user)

    def forget_mappings(self, user):
        self.forgotten.append(user)
        return self.bounds[user]["n"]

    def reopen_identity(self, user):
        self.reopened = getattr(self, "reopened", [])
        self.reopened.append(user)


def bounds(n, earliest):
    return {"n": n, "earliest": earliest}


class TestItCatchesTheRecreatedAccount:
    def test_a_mapping_older_than_its_account_is_stale(self):
        """The exact live shape: mappings at 11:17, account created 17:43."""
        db = FakeDB({"u@src": bounds(28545, "2026-08-21T11:17:15Z")})
        d = FakeDirectory({"u@tgt": "2026-08-21T17:43:46.000Z"})
        r = ledger_verify.verify(db, d, [("u@src", "u@tgt")])
        assert len(r.stale) == 1
        assert r.stale_mappings == 28545
        assert "predate the account" in r.stale[0].reason

    def test_a_mapping_newer_than_its_account_is_fine(self):
        """info@target survived precisely because it predated the ledger."""
        db = FakeDB({"u@src": bounds(4884, "2026-08-21T19:58:19Z")})
        d = FakeDirectory({"u@tgt": "2026-08-11T07:06:00.000Z"})
        assert not ledger_verify.verify(db, d, [("u@src", "u@tgt")]).stale

    def test_a_missing_account_is_a_finding_not_an_error(self):
        """Every mapping for an account that does not exist is unreachable by
        definition -- that is the answer, not a failure to get one."""
        db = FakeDB({"u@src": bounds(41, "2026-08-21T11:00:00Z")})
        r = ledger_verify.verify(db, FakeDirectory({}), [("u@src", "u@tgt")])
        assert len(r.stale) == 1
        assert r.unreadable == []
        assert "does not exist" in r.stale[0].reason

    def test_an_unreadable_account_is_not_reported_as_stale(self):
        """A network failure must never be turned into "your data is gone"."""
        class Boom(FakeDirectory):
            def get(self, userKey, fields=None):
                class _R:
                    def execute(self):
                        raise RuntimeError("503 backend error")
                return _R()

        db = FakeDB({"u@src": bounds(10, "2026-08-21T11:00:00Z")})
        r = ledger_verify.verify(db, Boom({}), [("u@src", "u@tgt")])
        assert r.stale == []
        assert len(r.unreadable) == 1


class TestItCostsOneCallPerUser:
    def test_it_does_not_scale_with_mapping_count(self):
        """216,615 mappings across 201 users is 201 requests, not 216,615.
        A per-file check would be correct and unusable."""
        db = FakeDB({f"u{i}@src": bounds(50_000, "2026-08-21T11:00:00Z")
                     for i in range(30)})
        d = FakeDirectory({f"u{i}@tgt": "2026-08-21T17:43:00.000Z"
                           for i in range(30)})
        ledger_verify.verify(db, d, [(f"u{i}@src", f"u{i}@tgt")
                                     for i in range(30)])
        assert d.calls == 30

    def test_a_user_with_no_mappings_costs_nothing(self):
        db = FakeDB({})
        d = FakeDirectory({"u@tgt": "2026-01-01T00:00:00.000Z"})
        ledger_verify.verify(db, d, [("u@src", "u@tgt")])
        assert d.calls == 0


class TestSpotCheck:
    def test_it_catches_contents_removed_from_a_surviving_account(self):
        """The case creationTime cannot see: the account is old, but its
        Drive was emptied some other way."""
        db = FakeDB({"u@src": bounds(500, "2026-08-21T11:00:00Z")},
                    samples={"u@src": "file123"})
        d = FakeDirectory({"u@tgt": "2026-01-01T00:00:00.000Z"})
        r = ledger_verify.verify(db, d, [("u@src", "u@tgt")],
                                 spot_check=lambda u, i: False)
        assert len(r.stale) == 1
        assert "sampled item" in r.stale[0].reason

    def test_it_is_not_consulted_when_the_account_already_failed(self):
        """No point sampling files in an account known to postdate them."""
        asked = []
        db = FakeDB({"u@src": bounds(5, "2026-08-21T11:00:00Z")},
                    samples={"u@src": "f1"})
        d = FakeDirectory({"u@tgt": "2026-08-21T17:43:00.000Z"})
        ledger_verify.verify(db, d, [("u@src", "u@tgt")],
                             spot_check=lambda u, i: asked.append(i) or True)
        assert asked == []


class TestReopen:
    def test_dry_run_changes_nothing(self):
        db = FakeDB({"u@src": bounds(99, "2026-08-21T11:00:00Z")})
        d = FakeDirectory({"u@tgt": "2026-08-21T17:43:00.000Z"})
        r = ledger_verify.verify(db, d, [("u@src", "u@tgt")])
        assert ledger_verify.reopen(db, r, dry_run=True) == 99
        assert db.forgotten == []

    def test_applying_forgets_only_the_stale_users(self):
        db = FakeDB({"bad@src": bounds(10, "2026-08-21T11:00:00Z"),
                     "good@src": bounds(10, "2026-08-21T19:00:00Z")})
        d = FakeDirectory({"bad@tgt": "2026-08-21T17:43:00.000Z",
                           "good@tgt": "2026-01-01T00:00:00.000Z"})
        r = ledger_verify.verify(db, d, [("bad@src", "bad@tgt"),
                                         ("good@src", "good@tgt")])
        ledger_verify.reopen(db, r, dry_run=False)
        assert db.forgotten == ["bad@src"]


class TestTimestampsFromTwoSources:
    def test_directory_and_audit_formats_compare_correctly(self):
        """Directory returns .000Z, audit_log does not, and one of them used
        to sort above the other purely on punctuation."""
        assert ledger_verify._iso("2026-08-21T17:43:46.000Z") == "2026-08-21T17:43:46"
        assert ledger_verify._iso("2026-08-21 17:43:46") == "2026-08-21T17:43:46"
        assert (ledger_verify._iso("2026-08-21T17:43:46.000Z")
                > ledger_verify._iso("2026-08-21T11:17:15Z"))


class TestAMigrationRefusesToRunAgainstAStaleLedger:
    """The silent-success case is the whole point. id_mapping is
    authoritative, so a run against recreated accounts skips every mapped
    item and reports done in seconds -- which is what would have happened
    on 2026-08-22 if nothing checked."""

    def _pairs(self):
        return [("u@src", "u@tgt")]

    def _auth(self, accounts):
        class FakeAuth:
            def directory(self, tenant):
                return FakeDirectory(accounts)
        return FakeAuth()

    def test_it_stops_the_run_when_mappings_predate_the_account(self):
        import pytest

        import main
        db = FakeDB({"u@src": bounds(35_490, "2026-08-20T08:53:39Z")})
        with pytest.raises(SystemExit) as e:
            main._warn_if_ledger_is_stale(
                db, self._auth({"u@tgt": "2026-08-21T17:43:46.000Z"}),
                self._pairs())
        assert "verify-ledger" in str(e.value)

    def test_it_does_not_stop_a_healthy_run(self):
        import main
        db = FakeDB({"u@src": bounds(10, "2026-08-21T19:00:00Z")})
        main._warn_if_ledger_is_stale(
            db, self._auth({"u@tgt": "2026-01-01T00:00:00.000Z"}),
            self._pairs())

    def test_a_failing_check_does_not_block_the_run(self):
        """An unreachable Directory must not be able to stop a migration that
        would otherwise be fine -- the check is a guard, not a dependency."""
        import main

        class Broken:
            def directory(self, tenant):
                raise RuntimeError("network down")

        db = FakeDB({"u@src": bounds(10, "2026-08-21T19:00:00Z")})
        main._warn_if_ledger_is_stale(db, Broken(), self._pairs())

    def test_it_does_not_reopen_anything_by_itself(self):
        """Forgetting 462,048 mappings means re-copying them -- hours of
        someone's quota, and not a decision to take on their behalf."""
        import pytest

        import main
        db = FakeDB({"u@src": bounds(500, "2026-08-20T08:00:00Z")})
        with pytest.raises(SystemExit):
            main._warn_if_ledger_is_stale(
                db, self._auth({"u@tgt": "2026-08-21T17:43:46.000Z"}),
                self._pairs())
        assert db.forgotten == []


class TestReopeningAUserNotJustTheirMappings:
    """Forgetting the mappings was half the job. identity_map.status stayed
    DONE and main._already_done() drops DONE users before dispatch, so the
    next run said "dispatching 177 users" instead of 200 and silently
    skipped 24 -- the largest holding 35,490 items. Two records claimed the
    work was finished; correcting one of them fixed nothing visible."""

    def _stale(self):
        db = FakeDB({"u@src": bounds(35_490, "2026-08-20T08:53:39Z")})
        d = FakeDirectory({"u@tgt": "2026-08-21T17:43:46.000Z"})
        return db, ledger_verify.verify(db, d, [("u@src", "u@tgt")])

    def test_the_user_is_reopened_as_well(self):
        db, report = self._stale()
        ledger_verify.reopen(db, report, dry_run=False)
        assert db.forgotten == ["u@src"]
        assert getattr(db, "reopened", []) == ["u@src"]

    def test_a_dry_run_reopens_nothing(self):
        db, report = self._stale()
        ledger_verify.reopen(db, report, dry_run=True)
        assert getattr(db, "reopened", []) == []

    def test_the_status_and_services_are_both_cleared(self, tmp_path):
        """_already_done() consults services_done per service, so a user
        reset to PENDING while still claiming every service was finished is
        the same skip in a narrower place."""
        import db as dbmod
        d = dbmod.MigrationDB(str(tmp_path / "m.db"))
        d.conn.execute(
            "INSERT INTO identity_map(source_email,target_email,status,"
            "services_done) VALUES(?,?,?,?)",
            ("u@src", "u@tgt", "DONE", "drive,gmail"))
        d.conn.commit()
        d.reopen_identity("u@src")
        row = d.conn.execute(
            "SELECT status, services_done FROM identity_map "
            "WHERE source_email='u@src'").fetchone()
        assert row["status"] == "PENDING"
        assert not row["services_done"]
        d.close()
