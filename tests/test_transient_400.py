"""A 400 that is not the caller's fault.

400 is permanent by default and that is right -- a malformed request does not
improve on retry. Drive's "you are trying to invite X, there is no Google
account associated with this email address" is the exception: it is the same
freshly-created-account lag already handled for 401 impersonation, because
Drive's sharing check does not see a new Workspace account for some minutes
after the Directory API reports it created.

Measured on a from-scratch run into a tenant provisioned two hours earlier:
134 grants refused this way, every one naming an account that existed. As a
permanent 400 they were never retried, so each became a failed grant the
post-run repair had to verify against the directory and resolve -- work that
only existed because the first call gave up instantly.
"""
import resilience


class FakeHttpError(Exception):
    pass


def _err(text):
    return FakeHttpError(text)


class TestTheFreshAccountLagIsRetried:
    SINGULAR = ('<HttpError 400 ...> "Bad Request. User message: "You are '
                'trying to invite a@t. Since there is no Google account '
                'associated with this email address, you must check the '
                '"Notify people" box."')
    PLURAL = ('<HttpError 400 ...> "You are trying to invite a@t, b@t. Since '
              'there are no Google accounts associated with these email '
              'addresses, you must check the "Notify people" box."')

    def test_the_singular_form_is_transient(self):
        assert resilience._is_permanent(400, "badRequest", _err(self.SINGULAR)) is False

    def test_the_plural_form_is_transient(self):
        assert resilience._is_permanent(400, "badRequest", _err(self.PLURAL)) is False

    def test_an_ordinary_400_is_still_permanent(self):
        """A malformed request does not improve on retry, and retrying it
        burns the quota that real work needs."""
        assert resilience._is_permanent(
            400, "invalidArgument", _err('<HttpError 400> "Invalid label"')) is True

    def test_a_400_with_no_exception_body_is_permanent(self):
        assert resilience._is_permanent(400, "badRequest", None) is True


class TestTheOtherClassificationsAreUnchanged:
    def test_quota_403_is_still_transient(self):
        assert resilience._is_permanent(403, "rateLimitExceeded") is False

    def test_a_permission_403_is_still_permanent(self):
        assert resilience._is_permanent(403, "insufficientPermissions") is True

    def test_500_is_still_retryable(self):
        assert resilience._is_permanent(500, "internalError") is False

    def test_404_is_still_permanent(self):
        assert resilience._is_permanent(404, "notFound") is True


class TestTheTokenScope403IsRetriedButTheFileDenialIsNot:
    """Google sends two different things under reason=insufficientPermissions
    and only one is a denial. 87 files were lost to the other kind on a
    single run -- every copy strategy "failed", none retried, no mapping
    written -- on a run whose scope_guard reported every required scope
    authorised, and on files their owner could export by hand minutes later."""

    SCOPES = ('<HttpError 403 ...> "Request had insufficient authentication '
              'scopes.". Details: [{"reason": "insufficientPermissions"}]')
    DENIAL = ('<HttpError 403 ...> "The user does not have sufficient '
              'permissions for this file." [{"reason": "insufficientPermissions"}]')

    def test_a_token_scope_403_is_retried(self):
        assert resilience._is_permanent(
            403, "insufficientPermissions", _err(self.SCOPES)) is False

    def test_a_real_file_denial_stays_permanent(self):
        """Retrying a denial six times per file wastes the quota real work
        needs, and it will never succeed."""
        assert resilience._is_permanent(
            403, "insufficientPermissions", _err(self.DENIAL)) is True

    def test_a_403_with_no_body_keeps_the_old_behaviour(self):
        assert resilience._is_permanent(403, "insufficientPermissions") is True

    def test_quota_403_is_unaffected(self):
        assert resilience._is_permanent(403, "rateLimitExceeded") is False


class TestTheTokenScope403GetsALongerBudget:
    """It is the only transient here measured in minutes rather than seconds.

    The standard ladder is 1+2+4+8+16+32 -- about 63 seconds. Live that was
    not enough: 49 files failed "Request had insufficient authentication
    scopes" having exhausted all six attempts, on a tenant whose scopes were
    fully authorised, and the identical export succeeded by hand minutes
    later. Every one was a real file loss -- none had a mapping afterwards.
    """

    SCOPES = ('<HttpError 403 ...> "Request had insufficient authentication '
              'scopes.". Details: [{"reason": "insufficientPermissions"}]')
    QUOTA = '<HttpError 403 ...> "Quota exceeded" [{"reason": "rateLimitExceeded"}]'

    def _count_attempts(self, monkeypatch, message, status=403):
        """How many times does the wrapper call fn before giving up?"""
        import resilience
        monkeypatch.setattr(resilience.time, "sleep", lambda s: None)
        calls = []

        class _Resp:
            def __init__(self):
                self.status = status
                self.reason = "fake"

            def get(self, k, default=None):
                return default

        class Boom(Exception):
            def __init__(self):
                super().__init__(message)
                self.resp = _Resp()

        monkeypatch.setattr(resilience, "HttpError", Boom)

        @resilience.retry_on_google_error(max_retries=6, base_delay=0.0,
                                          max_delay=0.0)
        def fn():
            calls.append(1)
            raise Boom()

        try:
            fn()
        except Exception:
            pass
        return len(calls)

    def test_the_scope_error_is_tried_far_more_than_six_times(self, monkeypatch):
        import resilience
        n = self._count_attempts(monkeypatch, self.SCOPES)
        assert n > 6, f"only {n} attempts -- the whole point is more than six"
        assert n <= resilience.SCOPE_RETRY_BUDGET + 1

    def test_a_quota_403_keeps_the_standard_budget(self, monkeypatch):
        """Widening the budget for everything would spend minutes per file on
        errors that will never clear."""
        n = self._count_attempts(monkeypatch, self.QUOTA)
        assert n <= 7, f"{n} attempts -- quota should keep the short ladder"

    def test_the_budget_is_longer_than_a_minute_of_backoff(self):
        """The condition outlives ~63s, which is what the standard ladder
        buys. The extra attempts each wait max_delay."""
        import resilience
        standard = sum(min(60, 2 ** (i - 1)) for i in range(1, 7))
        scoped = sum(min(60, 2 ** (i - 1))
                     for i in range(1, resilience.SCOPE_RETRY_BUDGET + 1))
        assert standard < 100
        assert scoped > 300
