"""A UI that shows progress must not be a meaningful cost to that progress.

Measured against a live 200k-row audit_log, one full dashboard refresh:

    spa_stages_payload        2018 ms
    spa_users_payload         1263 ms
    spa_metrics_payload        805 ms
    snapshot_payload           788 ms
    spa_verification_payload   783 ms
                               ------
                               5.7 s of CPU

useMigration.ts polls every 4000 ms, so one open tab asked for more CPU
than a 2-core box has -- and took it from the migration it was displaying.
webui.py held 43% of that box during a live run; closing the browser
returned ~13% throughput to the migration and dropped webui to 0%.

Only /api/status had a cache. These five had none.
"""
import time

import pytest

import webui


@pytest.fixture(autouse=True)
def _clear():
    webui.invalidate_spa_cache()
    webui._spa_busy.clear()
    yield
    webui.invalidate_spa_cache()
    webui._spa_busy.clear()


class TestItComputesOncePerWindow:
    def test_repeated_polls_hit_the_ledger_once(self):
        calls = []
        fn = lambda a: calls.append(a) or {"v": len(calls)}
        for _ in range(10):
            webui._cached_payload("x", fn, 66)
        assert len(calls) == 1, f"scanned the ledger {len(calls)} times for 10 polls"

    def test_the_first_call_is_served_honestly(self):
        # Nothing to show yet, so that one pays for itself rather than
        # returning an empty shape the page would render as "no data".
        out = webui._cached_payload("x", lambda a: {"v": 7}, 66)
        assert out == {"v": 7}

    def test_two_accounts_do_not_share_an_entry(self):
        fn = lambda a: {"acct": a}
        assert webui._cached_payload("x", fn, 7)["acct"] == 7
        assert webui._cached_payload("x", fn, 66)["acct"] == 66

    def test_two_readers_do_not_share_an_entry(self):
        assert webui._cached_payload("users", lambda a: "U", 7) == "U"
        assert webui._cached_payload("stages", lambda a: "S", 7) == "S"


class TestItNeverBlocksAPoll:
    def test_an_expired_entry_is_served_while_it_refreshes(self, monkeypatch):
        monkeypatch.setattr(webui, "SPA_TTL", 0.0)
        webui._cached_payload("x", lambda a: {"v": 1}, 66)

        def slow(a):
            time.sleep(0.4)
            return {"v": 2}

        t = time.time()
        out = webui._cached_payload("x", slow, 66)
        assert time.time() - t < 0.2, "a poll waited on the scan"
        assert out == {"v": 1}, "should serve the stale entry, not block"

        for _ in range(40):
            if webui._cached_payload("x", slow, 66) == {"v": 2}:
                break
            time.sleep(0.05)
        assert webui._cached_payload("x", slow, 66) == {"v": 2}

    def test_only_one_refresh_runs_at_a_time(self, monkeypatch):
        monkeypatch.setattr(webui, "SPA_TTL", 0.0)
        webui._cached_payload("x", lambda a: 0, 66)
        running = []

        def slow(a):
            running.append(1)
            time.sleep(0.3)
            return len(running)

        for _ in range(6):
            webui._cached_payload("x", slow, 66)
        time.sleep(0.6)
        assert len(running) == 1, f"{len(running)} concurrent ledger scans"

    def test_a_failed_refresh_keeps_serving_the_last_good_answer(self, monkeypatch):
        monkeypatch.setattr(webui, "SPA_TTL", 0.0)
        webui._cached_payload("x", lambda a: {"v": "good"}, 66)

        def boom(a):
            raise RuntimeError("ledger locked")

        assert webui._cached_payload("x", boom, 66) == {"v": "good"}
        time.sleep(0.2)
        assert webui._cached_payload("x", boom, 66) == {"v": "good"}, (
            "a failed refresh blanked a dashboard that was working")


class TestAButtonPressIsVisibleImmediately:
    def test_invalidation_drops_the_entries(self):
        webui._cached_payload("x", lambda a: 1, 66)
        webui.invalidate_spa_cache()
        assert webui._spa_cache == {}

    def test_writes_clear_both_caches(self):
        # Clearing only one leaves half the dashboard describing the world
        # as it was before the press.
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "webui.py"), encoding="utf-8").read()
        body = src.split("def _invalidate_all")[1][:300]
        assert "invalidate_status()" in body
        assert "invalidate_spa_cache()" in body
        assert src.count("_invalidate_all()") >= 6


class TestTheCheapReaderIsLeftAlone:
    def test_activity_is_not_cached(self):
        """64 ms, and it is the one people watch move -- caching it would
        buy nothing and make the live feed feel stuck."""
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "webui.py"), encoding="utf-8").read()
        # Just the handler line -- a wider window spills into the next
        # route, which IS cached, and the test then fails on its neighbour.
        route = src.split('elif path == "/api/spa/activity":')[1].splitlines()[1]
        assert "_cached_payload" not in route, route
