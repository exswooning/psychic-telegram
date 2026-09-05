"""The executor _off_loop uses must not be asyncio's default.

Pages rendered nothing but the nav, intermittently, with one pending
request and nothing failing. The cause was thread starvation:
run_in_executor(None, ...) uses a pool sized min(32, cpu_count + 4) -- six
threads on a 2-core VPS -- and _SingleFlightCache parks a thread for the
whole of someone else's slow query. A couple of slow reads took the pool
and /api/v2/auth/me queued behind them.
"""
import inspect

import api_server


def _asyncio_default_for(cores: int) -> int:
    """What asyncio would have given us. Kept as the comparison so this test
    states the thing it is defending against, not a bare number."""
    return min(32, cores + 4)


class TestTheOffloadPoolIsSizedForIO:
    def test_it_is_bigger_than_the_default_on_a_small_box(self):
        size = (api_server.BROWSER_CONNS_PER_HOST
                * api_server.CONCURRENT_WATCHERS
                + api_server.BACKGROUND_THREADS)
        assert size > _asyncio_default_for(2), (
            f"{size} threads is no better than asyncio's default on the "
            f"2-core VPS this actually runs on")

    def test_lifespan_installs_it(self):
        src = inspect.getsource(api_server.lifespan)
        assert "set_default_executor" in src, (
            "the constants exist but nothing installs the pool, so "
            "_off_loop is still using asyncio's default")

    def test_the_size_is_derived_and_not_a_literal(self):
        src = inspect.getsource(api_server.lifespan)
        assert "BROWSER_CONNS_PER_HOST" in src and "BACKGROUND_THREADS" in src, (
            "a literal here goes stale beside the comment deriving it")

    def test_a_parked_singleflight_waiter_holds_a_thread(self):
        """The premise of the whole fix. If this ever stops being true the
        pool can be smaller -- but it is true today, so the pool cannot."""
        src = inspect.getsource(api_server._SingleFlightCache.get)
        assert "with lock:" in src, (
            "waiters no longer block a thread; re-derive the pool size")
