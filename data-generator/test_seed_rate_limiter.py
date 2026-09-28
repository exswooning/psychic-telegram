"""
data-generator/test_seed_rate_limiter.py
=========================================
The seeder had no proactive pacing at all: resources.DRIVE_WRITES_PER_SEC
sized how many leaf threads to run, and retry_on_google_error only reacted
to a 429 after Google had already sent one. seed_sandbox._seed_drive_limiter
gives it the same treatment drive_engine._project_limiter already has on
the migration side -- an AdaptiveRateLimiter, acquired before every Drive
call, that discovers the real per-account ceiling and remembers it in the
account's own ledger (rate_limiter_ceiling, tenant="seed") across runs.
"""
from __future__ import annotations

import pytest

from db import MigrationDB
from resilience import AdaptiveRateLimiter

import seed_sandbox as s


@pytest.fixture(autouse=True)
def _reset_global_limiter():
    """Module-global, same shape as drive_engine._PROJECT_LIMITERS -- one
    seed run is one tenant's Drive quota, so it is cached for the whole
    process. Tests must not leak it into each other."""
    s._SEED_DRIVE_LIMITER = None
    yield
    s._SEED_DRIVE_LIMITER = None


class TestSeedDriveLimiter:
    def test_returns_an_adaptive_rate_limiter(self, settings):
        lim = s._seed_drive_limiter(settings)
        assert isinstance(lim, AdaptiveRateLimiter)

    def test_starting_rate_is_the_measured_constant(self, settings):
        import resources
        lim = s._seed_drive_limiter(settings)
        assert lim.rate == resources.DRIVE_WRITES_PER_SEC

    def test_cached_across_calls_in_one_process(self, settings):
        assert s._seed_drive_limiter(settings) is s._seed_drive_limiter(settings)

    def test_a_previous_runs_learned_ceiling_starts_the_next_one(self, settings):
        d = MigrationDB(settings.db_path)
        d.save_rate_ceiling("seed", 2.5)
        lim = s._seed_drive_limiter(settings)
        assert lim.ceiling == 2.5

    def test_never_starts_above_the_configured_guard(self, settings, monkeypatch):
        """A stale or bogus learned value must not start a run hotter than
        the deliberately conservative default would -- min(), not a
        straight use, mirrors drive_engine._project_limiter exactly."""
        monkeypatch.setenv("SEED_DRIVE_CEILING", "5.0")
        d = MigrationDB(settings.db_path)
        d.save_rate_ceiling("seed", 999.0)
        lim = s._seed_drive_limiter(settings)
        assert lim.ceiling == 5.0

    def test_a_real_rejection_persists_the_tightened_ceiling(self, settings):
        lim = s._seed_drive_limiter(settings)
        lim.rate = 4.0
        lim.penalise()
        d = MigrationDB(settings.db_path)
        assert d.load_rate_ceiling("seed") == pytest.approx(lim.ceiling)

    def test_a_broken_db_read_does_not_stop_a_seed_from_starting(
            self, settings, monkeypatch, capsys):
        class Broken:
            def __init__(self, *a, **k):
                raise RuntimeError("disk is gone")

        # _seed_drive_limiter does `import db as db_module` lazily inside the
        # function; patching sys.modules is what that import actually sees.
        import sys
        monkeypatch.setitem(sys.modules, "db",
                            type("FakeDbModule", (), {"MigrationDB": Broken}))
        lim = s._seed_drive_limiter(settings)
        assert isinstance(lim, AdaptiveRateLimiter)
        assert "could not read a learned seed rate ceiling" in capsys.readouterr().out


class TestRetryFactoryPacing:
    def test_no_limiter_behaves_exactly_as_before(self, settings):
        calls = []
        retry = s._retry_factory(settings)
        retry(lambda: calls.append(1) or "ok")()
        assert calls == [1]

    def test_a_limiter_is_acquired_before_the_call(self, settings):
        acquired = []

        class Fake:
            ceiling = 10.0

            def acquire(self, n=1):
                acquired.append(n)

            def penalise(self):
                pass

        retry = s._retry_factory(settings, limiter=Fake())
        assert retry(lambda: "ok")() == "ok"
        assert acquired == [1]

    def test_a_quota_rejection_penalises_the_limiter(self, settings):
        penalised = []

        class Fake:
            ceiling = 10.0

            def acquire(self, n=1):
                pass

            def penalise(self):
                penalised.append(1)
                return 5.0

        settings.max_retries = 0

        def boom():
            raise Exception("User rate limit exceeded")

        retry = s._retry_factory(settings, limiter=Fake())
        with pytest.raises(Exception):
            retry(boom)()
        assert penalised

    def test_a_non_quota_error_never_penalises(self, settings):
        penalised = []

        class Fake:
            ceiling = 10.0

            def acquire(self, n=1):
                pass

            def penalise(self):
                penalised.append(1)

        settings.max_retries = 0

        def boom():
            raise Exception("File not found: 404")

        retry = s._retry_factory(settings, limiter=Fake())
        with pytest.raises(Exception):
            retry(boom)()
        assert penalised == []
