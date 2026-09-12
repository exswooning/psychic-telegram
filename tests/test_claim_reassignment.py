"""When to stop waiting for an offline node and give its user to another.

Until now an expired lease was simply refused and a human had to force it.
That is safe and it stalls: a laptop that never comes back held its user
forever, and nothing else was allowed to finish it.

The trade-off is real, not a formality. The dead node's LOCAL ledger is
what makes resuming cheap -- gmail_engine skips a message when
db.get_target_id() finds a row, and those rows are on the machine that
died. Reassigning means re-delivering: slow but safe for Gmail, which asks
the target for the Message-ID, and DUPLICATED FILES for Drive, whose
duplicate check reads the local id_mapping. 004_user_claims.sql spells this
out at length.

The policy: wait up to the cost of redoing the work, then reassign. If the
node returns inside that, waiting won. If it has not, the redo cost has
already been spent waiting and still has to be paid -- so waiting longer
can only lose, whatever happens next.

The pleasing part is that one formula reproduces MULTINODE.md's per-service
advice without a second rule, because Drive's redo costs more.
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile

import pytest

import claim_policy
import control_plane_db as cpdb
import user_claims as uc
from db import MigrationDB


@pytest.fixture
def db(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _stale(user: str, services: str, minutes_ago: float,
           owner: str = "sleepy-laptop") -> None:
    """A claim whose lease lapsed `minutes_ago` minutes ago."""
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
    stamp = past.strftime("%Y-%m-%dT%H:%M:%SZ")
    with cpdb.rw() as conn:
        conn.execute(
            "INSERT INTO user_claims (account_id, source_user, node_id, status, "
            "services, claimed_at, renewed_at, lease_expires) "
            "VALUES (?,?,?,'CLAIMED',?,?,?,?)",
            (1, user, owner, services, stamp, stamp, stamp))


class TestThePolicyIsBoundedByTheCostOfRedoing:
    def test_it_waits_while_waiting_can_still_win(self):
        d = claim_policy.decide(5 * 60, "gmail", observed=20 * 60)
        assert d["reassign"] is False
        assert d["remainingS"] > 0

    def test_it_stops_once_waiting_can_only_lose(self):
        """Past the redo cost, the cost has been spent waiting AND still has
        to be paid. Every further second is unrecoverable."""
        d = claim_policy.decide(45 * 60, "gmail", observed=20 * 60)
        assert d["reassign"] is True

    def test_drive_waits_far_longer_than_gmail(self):
        """Not a separate rule: Drive's redo re-copies everything and leaves
        duplicates, so it costs more, so the bound is further out. One
        formula, and MULTINODE.md's advice falls out of it."""
        g = claim_policy.decide(0, "gmail", observed=20 * 60)["waitDeadlineS"]
        d = claim_policy.decide(0, "drive", observed=20 * 60)["waitDeadlineS"]
        assert d > g * 2

    def test_a_mixed_run_is_priced_as_its_worst_service(self):
        """Gmail-and-Drive together is as bad as the Drive half, because
        that is the half that duplicates. Averaging would under-price it."""
        mixed = claim_policy.decide(0, "gmail,drive", observed=20 * 60)
        only = claim_policy.decide(0, "drive", observed=20 * 60)
        assert mixed["waitDeadlineS"] == only["waitDeadlineS"]

    def test_all_is_priced_as_drive(self):
        """"all" means everything the tenant has, which includes Drive."""
        a = claim_policy.decide(0, "all", observed=20 * 60)
        d = claim_policy.decide(0, "drive", observed=20 * 60)
        assert a["waitDeadlineS"] == d["waitDeadlineS"]

    def test_there_is_always_a_floor(self):
        """A node restarting after a deploy, or a laptop waking, is back
        within a couple of minutes and resumes for free. Reassigning inside
        that window pays the whole redo cost to save nothing."""
        d = claim_policy.decide(10, "gmail", observed=1)
        assert d["reassign"] is False
        assert d["waitDeadlineS"] >= claim_policy.MIN_WAIT_S

    def test_the_decision_carries_its_arithmetic(self):
        """This duplicates somebody's files if it is wrong, so a takeover
        has to be able to say why in numbers an operator can check."""
        d = claim_policy.decide(45 * 60, "drive", observed=20 * 60)
        for key in ("reassign", "waitDeadlineS", "redoCostS", "remainingS", "reason"):
            assert key in d
        assert "redo" in d["reason"]


class TestItLearnsHowLongAUserTakes:
    def test_it_uses_observed_durations(self, db):
        with cpdb.rw() as conn:
            for i in range(5):
                conn.execute(
                    "INSERT INTO user_claims (account_id, source_user, node_id, "
                    "status, claimed_at, renewed_at, lease_expires) "
                    "VALUES (?,?,?,'DONE',?,?,?)",
                    (1, f"u{i}@x.test", "n",
                     "2026-09-01T10:00:00Z", "2026-09-01T10:30:00Z",
                     "2026-09-01T10:30:00Z"))
        assert abs(claim_policy.observed_user_seconds(1) - 30 * 60) < 60

    def test_it_uses_the_median_not_the_mean(self, db):
        """One 40 GB mailbox must not set the estimate for the other 199,
        and a stuck run that was eventually killed must not either."""
        spans = [10, 10, 10, 10, 600]      # minutes
        with cpdb.rw() as conn:
            for i, m in enumerate(spans):
                end = dt.datetime(2026, 9, 1, 10, 0) + dt.timedelta(minutes=m)
                conn.execute(
                    "INSERT INTO user_claims (account_id, source_user, node_id, "
                    "status, claimed_at, renewed_at, lease_expires) "
                    "VALUES (?,?,?,'DONE',?,?,?)",
                    (1, f"v{i}@x.test", "n", "2026-09-01T10:00:00Z",
                     end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     end.strftime("%Y-%m-%dT%H:%M:%SZ")))
        assert abs(claim_policy.observed_user_seconds(1) - 10 * 60) < 60

    def test_a_first_run_still_decides(self, db):
        """No history to learn from is not a reason to refuse to decide."""
        assert claim_policy.observed_user_seconds(1) == claim_policy.DEFAULT_USER_SECONDS


class TestTheClaimPathAppliesIt:
    def test_a_recently_offline_node_keeps_its_user(self, db):
        _stale("a@x.test", "gmail", 5)
        ok, why = uc._local_acquire(1, "a@x.test", node="vps-garud", services="gmail")
        assert ok is False
        assert "waiting up to" in why

    def test_a_long_gone_node_loses_it(self, db):
        _stale("b@x.test", "gmail", 400)
        ok, _ = uc._local_acquire(1, "b@x.test", node="vps-garud", services="gmail")
        assert ok is True

    def test_drive_is_still_waited_for_when_gmail_would_not_be(self, db):
        _stale("c@x.test", "drive", 40)
        assert uc._local_acquire(1, "c@x.test", node="vps-garud",
                                 services="drive")[0] is False
        _stale("c2@x.test", "gmail", 40)
        assert uc._local_acquire(1, "c2@x.test", node="vps-garud",
                                 services="gmail")[0] is True

    def test_the_same_node_returning_always_resumes(self, db):
        """Its ledger is intact, so this is a resume and not a takeover --
        no waiting, no cost, no record."""
        _stale("e@x.test", "drive", 2)
        ok, _ = uc._local_acquire(1, "e@x.test", node="sleepy-laptop",
                                  services="drive")
        assert ok is True

    def test_a_takeover_is_recorded_with_its_reasoning(self, db):
        """The one place this tool decides by itself to re-deliver work.
        Somebody finding duplicate Drive files later must be able to find
        the decision and the numbers behind it."""
        _stale("f@x.test", "drive", 600)
        assert uc._local_acquire(1, "f@x.test", node="vps-garud",
                                 services="drive")[0] is True
        takeovers = [a for a in cpdb.recent_actions(50)
                     if "take over" in (a.get("action") or "")]
        assert takeovers, "a takeover left no record"
        assert "sleepy-laptop" in takeovers[0]["target"]
        assert "redo" in takeovers[0]["reason"]

    def test_the_takeover_is_marked_on_the_claim_too(self, db):
        """forced_from, exactly as a manual force sets it -- the Nodes page
        already renders it."""
        _stale("g@x.test", "gmail", 400)
        uc._local_acquire(1, "g@x.test", node="vps-garud", services="gmail")
        with cpdb.ro() as conn:
            row = conn.execute("SELECT forced_from FROM user_claims "
                               "WHERE source_user='g@x.test'").fetchone()
        assert row["forced_from"] == "sleepy-laptop"

    def test_a_fresh_claim_is_untouched_and_unaudited(self, db):
        """The common path. It was briefly orphaned behind a return while
        this was being written, which the live check caught."""
        assert uc._local_acquire(1, "new@x.test", node="vps-garud",
                                 services="gmail") == (True, "")
        with cpdb.ro() as conn:
            row = conn.execute("SELECT forced_from FROM user_claims "
                               "WHERE source_user='new@x.test'").fetchone()
        assert row["forced_from"] == ""
        assert not [a for a in cpdb.recent_actions(50)
                    if "take over" in (a.get("action") or "")]

    def test_the_audit_happens_outside_the_transaction(self):
        """Written inside, it deadlocks against this call's own BEGIN
        IMMEDIATE -- and because _audit_takeover swallows its errors so it
        can never block a claim, the record simply never appeared. Silently,
        which is the worst outcome for the one thing that explains a
        duplicated Drive."""
        import inspect
        src = inspect.getsource(uc._local_acquire)
        assert "pending_audit" in src
        assert src.index("with cpdb.rw()") < src.index("took = pending_audit")
        # The call itself is dedented back out of the `with`.
        for line in src.splitlines():
            if "_audit_takeover(account_id" in line:
                assert line.startswith("        _audit_takeover"), line
