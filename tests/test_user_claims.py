"""
tests/test_user_claims.py
=========================
Who migrates which user, when more than one machine is doing the work.

The failure this prevents is silent and expensive: two nodes both start user
U, and every one of U's messages is inserted twice. Nothing downstream
notices, because the per-item ledger that makes a re-run idempotent is local
to each node -- neither can see the other's work, so neither skips anything.

The second half of these tests pins something less obvious and more
important: an *expired* lease must not be handed to a different node. Resume
reads the dead node's local ledger, so another machine picking the user up
starts from empty and re-delivers everything. That is the same duplication,
arrived at by a route that looks like sensible failover.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import control_plane_db as cpdb  # noqa: E402
import user_claims as uc  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "cp.db")
    monkeypatch.setattr(cpdb, "_db_path", lambda: path)
    cpdb.apply_migrations(path)
    return path


class TestOneUserOneNode:
    def test_a_free_user_is_claimable(self, db):
        ok, why = uc.acquire(7, "a@x.com", node="alpha")
        assert ok and why == ""

    def test_a_second_node_is_refused_and_told_who_holds_it(self, db):
        uc.acquire(7, "a@x.com", node="alpha")
        ok, why = uc.acquire(7, "a@x.com", node="beta")
        assert not ok
        assert "alpha" in why

    def test_the_owner_may_reclaim_its_own_live_claim(self, db):
        """Idempotent for the owner: a restarted run re-claiming its own
        users must not deadlock itself out of its own work."""
        assert uc.acquire(7, "a@x.com", node="alpha")[0]
        assert uc.acquire(7, "a@x.com", node="alpha")[0]

    def test_different_accounts_do_not_collide_on_the_same_address(self, db):
        """Two tenants can legitimately both have info@ -- the claim is per
        (account, user), not per address."""
        assert uc.acquire(7, "info@x.com", node="alpha")[0]
        assert uc.acquire(9, "info@x.com", node="beta")[0]

    def test_a_finished_user_is_not_handed_out_again(self, db):
        uc.acquire(7, "a@x.com", node="alpha")
        uc.finish(7, "a@x.com", node="alpha")
        ok, why = uc.acquire(7, "a@x.com", node="beta")
        assert not ok
        assert "already migrated" in why

    def test_a_released_claim_becomes_free_again(self, db):
        """A node that claims then decides not to work must not strand the
        user until its lease lapses."""
        uc.acquire(7, "a@x.com", node="alpha")
        uc.release(7, "a@x.com", node="alpha")
        assert uc.acquire(7, "a@x.com", node="beta")[0]


class TestTheRaceIsActuallyClosed:
    def test_only_one_of_many_concurrent_claimers_wins(self, db):
        """The whole point, exercised concurrently rather than argued for.

        Without BEGIN IMMEDIATE both threads read "unclaimed" and both
        insert; this is the test that would catch a well-meaning change to a
        plain deferred transaction.
        """
        winners: list[str] = []
        lock = threading.Lock()
        start = threading.Barrier(8)

        def run(name: str) -> None:
            start.wait()
            ok, _ = uc.acquire(7, "hot@x.com", node=name)
            if ok:
                with lock:
                    winners.append(name)

        threads = [threading.Thread(target=run, args=(f"n{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(winners) == 1, f"{len(winners)} nodes all claimed one user"

    def test_a_whole_user_list_partitions_with_no_overlap(self, db):
        """Two nodes sweeping the same list must between them cover every
        user exactly once -- no double-claims, and nothing dropped."""
        users = [f"u{i}@x.com" for i in range(40)]
        got: dict[str, list[str]] = {"alpha": [], "beta": []}
        lock = threading.Lock()

        def sweep(name: str) -> None:
            for u in users:
                ok, _ = uc.acquire(7, u, node=name)
                if ok:
                    with lock:
                        got[name].append(u)

        ts = [threading.Thread(target=sweep, args=(n,)) for n in ("alpha", "beta")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        assert not set(got["alpha"]) & set(got["beta"]), "a user was claimed twice"
        assert sorted(got["alpha"] + got["beta"]) == sorted(users)


class TestLeases:
    def _expire(self, db, user="a@x.com", account=7):
        past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=60))
        with cpdb.rw() as conn:
            conn.execute("UPDATE user_claims SET lease_expires=? "
                         "WHERE account_id IS ? AND source_user=?",
                         (uc._stamp(past), account, user))

    def test_renewing_keeps_a_claim_alive(self, db):
        uc.acquire(7, "a@x.com", node="alpha", lease_seconds=1)
        assert uc.renew(7, "a@x.com", node="alpha", lease_seconds=300)
        rows = uc.claims(7)
        assert rows[0]["live"] is True

    def test_renewing_a_claim_that_is_no_longer_ours_fails(self, db):
        """Meaningful, not cosmetic: it means an operator forced the user
        elsewhere and this node must stop rather than race the new owner."""
        uc.acquire(7, "a@x.com", node="alpha")
        assert uc.renew(7, "a@x.com", node="beta") is False

    def test_the_same_node_may_resume_its_own_expired_claim(self, db):
        """A restart. Its local item ledger is intact, so resuming is safe
        and skips everything already migrated."""
        uc.acquire(7, "a@x.com", node="alpha")
        self._expire(db)
        ok, _ = uc.acquire(7, "a@x.com", node="alpha")
        assert ok

    def test_a_different_node_may_NOT_take_over_an_expired_claim(self, db):
        """The subtle one. This looks exactly like reasonable failover and
        is the same silent duplication the module exists to prevent: resume
        reads the DEAD node's local ledger, so a fresh node re-inserts
        everything already delivered."""
        uc.acquire(7, "a@x.com", node="alpha")
        self._expire(db)
        ok, why = uc.acquire(7, "a@x.com", node="beta")
        assert not ok
        assert "expired" in why
        assert "alpha" in why
        assert "force" in why.lower()

    def test_force_takes_it_and_records_where_it_came_from(self, db):
        """Forcing is a real decision with a real cost (duplicates unless
        the target is cleaned first), so it is recorded, not silent."""
        uc.acquire(7, "a@x.com", node="alpha")
        self._expire(db)
        ok, _ = uc.acquire(7, "a@x.com", node="beta", force=True)
        assert ok
        row = uc.claims(7)[0]
        assert row["node_id"] == "beta"
        assert row["forced_from"] == "alpha"

    def test_a_stale_claim_is_reported_as_stale_not_live(self, db):
        uc.acquire(7, "a@x.com", node="alpha")
        self._expire(db)
        row = uc.claims(7)[0]
        assert row["stale"] is True and row["live"] is False


class TestReporting:
    def test_finishing_keeps_the_row_so_the_ledger_can_be_found(self, db):
        """The row records WHICH node holds that user's item ledger. Delete
        it and a later verification pass cannot tell which machine to read
        the per-item history from."""
        uc.acquire(7, "a@x.com", node="alpha")
        uc.finish(7, "a@x.com", node="alpha")
        rows = uc.claims(7)
        assert len(rows) == 1
        assert rows[0]["status"] == "DONE"
        assert rows[0]["node_id"] == "alpha"

    def test_summary_counts_by_node_and_state(self, db):
        uc.acquire(7, "a@x.com", node="alpha")
        uc.finish(7, "a@x.com", node="alpha")
        uc.acquire(7, "b@x.com", node="alpha")
        uc.acquire(7, "c@x.com", node="beta")
        uc.finish(7, "c@x.com", node="beta", status="FAILED", detail="boom")

        s = uc.summary(7)
        assert s["total"] == 3
        assert s["done"] == 1
        assert s["failed"] == 1
        nodes = {n["node"]: n for n in s["nodes"]}
        assert nodes["alpha"]["done"] == 1 and nodes["alpha"]["claimed"] == 1
        assert nodes["beta"]["failed"] == 1

    def test_claims_are_scoped_to_one_account_by_default(self, db):
        uc.acquire(7, "a@x.com", node="alpha")
        uc.acquire(9, "b@x.com", node="alpha")
        assert [r["source_user"] for r in uc.claims(7)] == ["a@x.com"]
        assert len(uc.claims(all_accounts=True)) == 2


class TestNodeIdentity:
    def test_the_hostname_is_the_default_identity(self, db, monkeypatch):
        monkeypatch.delenv("BITPORT_NODE_ID", raising=False)
        import socket
        assert uc.node_id() == socket.gethostname()

    def test_an_explicit_node_id_overrides_it(self, db, monkeypatch):
        """Two nodes sharing a hostname (cloned cloud images, containers)
        would each silently satisfy the other's lease renewals -- exactly
        the collision this module exists to prevent."""
        monkeypatch.setenv("BITPORT_NODE_ID", "worker-2")
        assert uc.node_id() == "worker-2"


class TestCoordinatedDispatch:
    """`run_batch` under coordination.

    Off unless a coordinator is configured, so every existing single-box
    install behaves exactly as before. On, every user is claimed before any
    of its data moves, and the claim is held for as long as the work runs.
    """

    def _batch(self, monkeypatch, pairs, coordinated=True, migrate=None):
        import main

        monkeypatch.setattr(main, "_coordination_enabled", lambda: coordinated)

        class FakeDB:
            def all_identities(self):
                return [{"entity_type": "user", "source_email": s,
                         "target_email": t, "status": "PENDING"}
                        for s, t in pairs]
            def services_done(self, u):
                return set()

        calls: list[str] = []

        def fake_migrate(auth, db, settings, s, t, services, delta, days):
            calls.append(s)
            if migrate:
                migrate(s)
            return {"source": s, "status": "DONE"}

        monkeypatch.setattr(main, "migrate_user", fake_migrate)

        class S:
            user_workers = 4
            account_id = 7

        out = main.run_batch(None, FakeDB(), S(), {"gmail"},
                             delta=False, delta_days=0)
        return out, calls

    def test_uncoordinated_runs_are_completely_unchanged(self, db, monkeypatch):
        """The single-box case must not acquire anything, so an existing
        install gains no new failure mode and no new dependency."""
        acquired: list[str] = []
        monkeypatch.setattr(uc, "acquire",
                            lambda *a, **k: acquired.append(a) or (True, ""))
        out, calls = self._batch(monkeypatch, [("a@x.com", "a@y.com")],
                                 coordinated=False)
        assert calls == ["a@x.com"]
        assert acquired == []
        assert len(out) == 1

    def test_every_user_is_claimed_before_its_data_moves(self, db, monkeypatch):
        order: list[str] = []
        monkeypatch.setattr(uc, "acquire",
                            lambda acct, u, **k: order.append(f"claim:{u}") or (True, ""))
        monkeypatch.setattr(uc, "finish", lambda *a, **k: None)
        self._batch(monkeypatch, [("a@x.com", "a@y.com")],
                    migrate=lambda u: order.append(f"migrate:{u}"))
        assert order.index("claim:a@x.com") < order.index("migrate:a@x.com")

    def test_a_user_another_node_owns_is_skipped_not_migrated(self, db, monkeypatch):
        """The core property. If this ever regresses, both nodes migrate the
        same mailbox and every message in it is delivered twice."""
        monkeypatch.setattr(uc, "acquire",
                            lambda acct, u, **k: (u != "taken@x.com",
                                                  "held by beta"))
        monkeypatch.setattr(uc, "finish", lambda *a, **k: None)
        out, calls = self._batch(monkeypatch, [("taken@x.com", "t@y.com"),
                                               ("free@x.com", "f@y.com")])
        assert calls == ["free@x.com"]
        assert [r["source"] for r in out] == ["free@x.com"]

    def test_a_skipped_user_is_not_reported_as_this_nodes_work(self, db, monkeypatch):
        """A batch summary that lists a user this node never touched would
        credit it with another machine's migration."""
        monkeypatch.setattr(uc, "acquire", lambda *a, **k: (False, "held"))
        monkeypatch.setattr(uc, "finish", lambda *a, **k: None)
        out, calls = self._batch(monkeypatch, [("a@x.com", "a@y.com")])
        assert calls == []
        assert out == []

    def test_a_finished_user_is_marked_done(self, db, monkeypatch):
        seen: list[tuple] = []
        monkeypatch.setattr(uc, "acquire", lambda *a, **k: (True, ""))
        monkeypatch.setattr(uc, "finish",
                            lambda acct, u, **k: seen.append((u, k.get("status"))))
        self._batch(monkeypatch, [("a@x.com", "a@y.com")])
        assert seen == [("a@x.com", "DONE")]

    def test_a_crashing_user_is_marked_failed_not_left_claimed(self, db, monkeypatch):
        """Otherwise it sits CLAIMED until the lease lapses and reads as a
        live node still working on it -- so nobody retries it and nobody
        knows why."""
        seen: list[tuple] = []
        monkeypatch.setattr(uc, "acquire", lambda *a, **k: (True, ""))
        monkeypatch.setattr(uc, "finish",
                            lambda acct, u, **k: seen.append((u, k.get("status"))))

        def boom(u):
            raise RuntimeError("drive exploded")

        self._batch(monkeypatch, [("a@x.com", "a@y.com")], migrate=boom)
        assert seen == [("a@x.com", "FAILED")]


class TestLeaseRenewal:
    def test_the_lease_is_renewed_while_the_user_is_in_flight(self, db, monkeypatch):
        """A user can take an hour against a five-minute lease."""
        import main

        monkeypatch.setattr(uc, "RENEW_EVERY", 0.01)
        monkeypatch.setattr(main.user_claims, "RENEW_EVERY", 0.01)
        renewed: list[str] = []
        monkeypatch.setattr(main.user_claims, "renew",
                            lambda acct, u, **k: renewed.append(u) or True)

        stop = threading.Event()
        t = threading.Thread(target=main._renew_until, args=(stop, 7, "a@x.com"))
        t.start()
        import time
        time.sleep(0.1)
        stop.set()
        t.join(timeout=2)
        assert renewed, "the lease was never renewed"

    def test_renewal_stops_when_the_claim_is_taken_away(self, db, monkeypatch):
        """An operator forced the user elsewhere; this thread has nothing
        useful left to do and must not keep hammering the coordinator."""
        import main

        monkeypatch.setattr(main.user_claims, "RENEW_EVERY", 0.01)
        calls = {"n": 0}

        def once(acct, u, **k):
            calls["n"] += 1
            return False

        monkeypatch.setattr(main.user_claims, "renew", once)
        stop = threading.Event()
        t = threading.Thread(target=main._renew_until, args=(stop, 7, "a@x.com"))
        t.start()
        import time
        time.sleep(0.1)
        stop.set()
        t.join(timeout=2)
        assert calls["n"] == 1, "kept renewing a claim it no longer holds"

    def test_a_coordinator_blip_does_not_kill_the_migration(self, db, monkeypatch):
        """The lease may lapse -- which shows up as a stale claim an operator
        can see -- but an unreachable coordinator must not end a run that is
        otherwise working."""
        import main

        monkeypatch.setattr(main.user_claims, "RENEW_EVERY", 0.01)

        def boom(*a, **k):
            raise uc.CoordinatorError("connection refused")

        monkeypatch.setattr(main.user_claims, "renew", boom)
        stop = threading.Event()
        t = threading.Thread(target=main._renew_until, args=(stop, 7, "a@x.com"))
        t.start()
        import time
        time.sleep(0.05)
        stop.set()
        t.join(timeout=2)
        assert not t.is_alive()


class TestAutoMapCanBootstrapAFreshTarget:
    """auto-map and provision-users could not start each other.

    auto-map pairs only accounts that already exist on BOTH tenants;
    provision-users only creates accounts already in identity_map. On a
    fresh target that is a closed loop: a tenant holding one account mapped
    1 of the source's 201 users, then correctly reported nothing to create.
    Live, that read as a button doing nothing, repeatedly.
    """

    def _run(self, monkeypatch, include_missing):
        import main

        src = ["a@src.com", "b@src.com", "c@src.com"]
        tgt = ["a@tgt.com"]                     # only one exists yet
        monkeypatch.setattr(main, "list_domain_users",
                            lambda auth, side, dom: src if side == "source" else tgt)
        seeded: list = []
        import db as db_mod
        monkeypatch.setattr(db_mod, "bulk_seed_identities",
                            lambda d, pairs: seeded.extend(pairs))

        class S:
            source_domain = "src.com"
            target_domain = "tgt.com"
            db_path = ":memory:"

        args = type("A", (), {"identities": None, "auto_map": True,
                              "include_missing": include_missing})()
        main.cmd_init_db(args, S(), None, None)
        return seeded

    def test_without_the_flag_only_existing_accounts_are_mapped(self, monkeypatch):
        """The old behaviour, kept as the default: a lift-and-shift merge
        where nobody is being renamed should not invent target accounts."""
        assert self._run(monkeypatch, False) == [("a@src.com", "a@tgt.com")]

    def test_with_the_flag_every_source_user_is_mapped(self, monkeypatch):
        """So provision-users has something to create."""
        seeded = self._run(monkeypatch, True)
        assert sorted(seeded) == [("a@src.com", "a@tgt.com"),
                                  ("b@src.com", "b@tgt.com"),
                                  ("c@src.com", "c@tgt.com")]

    def test_the_mapping_keeps_the_localpart(self, monkeypatch):
        """b@src.com must become b@tgt.com, not a generated name -- the
        address is what an operator will recognise in the target."""
        seeded = dict(self._run(monkeypatch, True))
        assert seeded["b@src.com"] == "b@tgt.com"
