"""
tests/test_rate_limit_gets_time.py
==================================
A rate-limit 403 is the server asking for time, not refusing. It was given
the standard ladder -- about a minute -- which is short against a
per-project quota window: live, one calendar event exhausted all six
attempts and was lost, on a run whose own profile showed zero threads in
backoff, so nothing was even queued behind it.

Deliberately narrower than the scope budget. The comment beside that one
warns against widening it for everything, because most errors never clear.
These three always do.
"""

from __future__ import annotations

import pytest

import resilience as R


class TestTheBudget:
    def test_a_rate_limit_gets_longer_than_the_standard_ladder(self):
        assert R.RATE_LIMIT_RETRY_BUDGET > 6

    def test_but_not_as_long_as_a_propagating_scope(self):
        """Scope propagation is measured in tens of minutes; a quota window
        is not, and spending that long per item would stall a run."""
        assert R.RATE_LIMIT_RETRY_BUDGET < R.SCOPE_RETRY_BUDGET

    def test_it_covers_the_three_reasons_google_uses_for_slow_down(self):
        assert R.TRANSIENT_403_REASONS == {
            "rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}


class TestItIsActuallyApplied:
    def _call_that_always_429s(self, reason):
        calls = {"n": 0}

        @R.retry_on_google_error(max_retries=6, base_delay=0, max_delay=0)
        def boom():
            calls["n"] += 1
            raise R.HttpError(
                resp=type("R", (), {"status": 403, "reason": "Forbidden",
                                    "get": lambda self, k, d=None: None})(),
                content=('{"error":{"errors":[{"reason":"%s"}],'
                         '"message":"quota"}}' % reason).encode())

        return boom, calls

    def test_a_rate_limit_uses_the_longer_budget(self, monkeypatch):
        monkeypatch.setattr(R.time, "sleep", lambda *_: None)
        boom, calls = self._call_that_always_429s("rateLimitExceeded")
        with pytest.raises(RuntimeError, match="exhausted"):
            boom()
        # One initial attempt plus the budget.
        assert calls["n"] == R.RATE_LIMIT_RETRY_BUDGET + 1, calls

    def test_an_unrelated_403_still_fails_fast(self, monkeypatch):
        """A genuine permission denial must not spend three minutes."""
        monkeypatch.setattr(R.time, "sleep", lambda *_: None)
        boom, calls = self._call_that_always_429s("insufficientPermissions")
        with pytest.raises(Exception):
            boom()
        assert calls["n"] <= 2, "a permanent 403 was retried"


class TestTheLimiterLearnsFromTheFirstRejectionNotOnlyTheLast:
    """A single call's own ladder took up to ~4 minutes to give up, deaf to its own
    rejections the whole time -- a concurrent sibling under the same project limiter
    kept sending at the pre-rejection rate until this call finally raised. Live, that
    read as 425 permanently failed grants during one burst. on_quota_rejection fires on
    every rejected attempt, not just the final one, so a limiter wired to it hears the
    very first."""

    def _call_that_always_429s(self, reason, on_quota_rejection=None, quota_reasons=None):
        calls = {"n": 0}

        @R.retry_on_google_error(max_retries=6, base_delay=0, max_delay=0,
                                 on_quota_rejection=on_quota_rejection,
                                 quota_reasons=quota_reasons)
        def boom():
            calls["n"] += 1
            raise R.HttpError(
                resp=type("R", (), {"status": 403, "reason": "Forbidden",
                                    "get": lambda self, k, d=None: None})(),
                content=('{"error":{"errors":[{"reason":"%s"}],'
                         '"message":"quota"}}' % reason).encode())

        return boom, calls

    def test_it_fires_once_per_rejection_including_the_one_that_exhausts_the_budget(self, monkeypatch):
        monkeypatch.setattr(R.time, "sleep", lambda *_: None)
        seen = []
        boom, calls = self._call_that_always_429s("rateLimitExceeded", on_quota_rejection=lambda: seen.append(1))
        with pytest.raises(RuntimeError, match="exhausted"):
            boom()
        assert len(seen) == calls["n"] == R.RATE_LIMIT_RETRY_BUDGET + 1

    def test_a_project_limiter_does_not_hear_one_users_budget(self, monkeypatch):
        """Live: one seeduser94 userRateLimitExceeded ratcheted the source
        project ceiling 977 -> 281, persisted for every later run."""
        import drive_engine
        monkeypatch.setattr(R.time, "sleep", lambda *_: None)
        seen = []
        boom, _ = self._call_that_always_429s(
            "userRateLimitExceeded", on_quota_rejection=lambda: seen.append(1),
            quota_reasons=drive_engine.PROJECT_QUOTA_REASONS)
        with pytest.raises(RuntimeError):
            boom()
        assert seen == []
        boom, _ = self._call_that_always_429s(
            "rateLimitExceeded", on_quota_rejection=lambda: seen.append(1),
            quota_reasons=drive_engine.PROJECT_QUOTA_REASONS)
        with pytest.raises(RuntimeError):
            boom()
        assert seen, "a project-level rejection must still reach the limiter"

    def test_it_is_not_called_for_a_permanent_403(self, monkeypatch):
        monkeypatch.setattr(R.time, "sleep", lambda *_: None)
        seen = []
        boom, _ = self._call_that_always_429s("insufficientPermissions", on_quota_rejection=lambda: seen.append(1))
        with pytest.raises(Exception):
            boom()
        assert seen == []

    def test_omitting_it_changes_nothing_for_every_existing_caller(self, monkeypatch):
        monkeypatch.setattr(R.time, "sleep", lambda *_: None)
        boom, calls = self._call_that_always_429s("rateLimitExceeded")
        with pytest.raises(RuntimeError, match="exhausted"):
            boom()
        assert calls["n"] == R.RATE_LIMIT_RETRY_BUDGET + 1


class TestWhichLimitRefusedIsRecorded:
    """The reason alone ("rateLimitExceeded") does not say whose limit bound --
    one user's or the whole project's -- and that decides whether a second Cloud
    project helps. Google's message names the limit; it is now kept."""

    def test_each_refusal_is_counted_by_reason_and_by_the_limit_google_named(self, monkeypatch, caplog):
        from tests.fakes import http_error
        monkeypatch.setattr(R, "REJECTIONS", {})
        monkeypatch.setattr(R, "_rej_tally_at", [0.0])
        refusals = iter([
            http_error(429, "rateLimitExceeded", "Quota exceeded for quota metric 'Write requests' and "
                                                 "limit 'Write requests per minute per user' of service drive"),
            http_error(403, "userRateLimitExceeded", "User Rate Limit Exceeded"),
        ])

        @R.retry_on_google_error(max_retries=6, base_delay=0, max_delay=0, label="drive.files.copy")
        def call():
            nxt = next(refusals, None)
            if nxt is not None:
                raise nxt
            return "ok"

        with caplog.at_level("INFO", logger="resilience"):
            assert call() == "ok"
        assert R.REJECTIONS == {
            ("drive.files.copy", 429, "rateLimitExceeded", "Write requests per minute per user"): 1,
            ("drive.files.copy", 403, "userRateLimitExceeded", "User Rate Limit Exceeded"): 1}
        assert "Write requests per minute per user" in caplog.text      # the first of a kind, in full
