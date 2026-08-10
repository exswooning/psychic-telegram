"""
tests/test_verify_scopes.py
============================
The functional DWD-scope checker.

Google publishes no API to read a delegation entry, so "is this scope
granted?" can only be answered by trying to mint a token for it and reading
the failure. These tests pin the error classification (an operator acts
differently on "not delegated" than on "wrong subject") and the scope-union
logic that keeps an Overwrite from silently revoking something live.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import verify_scopes as vs  # noqa: E402


class FakeCreds:
    def __init__(self, exc: Exception | None):
        self._exc = exc

    def refresh(self, request):
        if self._exc:
            raise self._exc


class TestProbeClassification:
    """One scope, one token request, one of a small set of outcomes -- the
    UI and the CLI both read `detail` to tell an operator what to do next,
    so the classification has to be right, not just non-empty."""

    def _probe_with(self, monkeypatch, exc):
        class _Cred:
            def with_subject(self, subject):
                return FakeCreds(exc)

        monkeypatch.setattr(
            "google.oauth2.service_account.Credentials.from_service_account_file",
            lambda *a, **kw: _Cred())
        return vs.probe_scope("/tmp/does-not-matter.json", "a@b.com", "scope-x")

    def test_unauthorized_client_reads_as_not_delegated(self, monkeypatch):
        ok, detail = self._probe_with(
            monkeypatch, Exception("400: unauthorized_client blah"))
        assert not ok
        assert detail == "not delegated"

    def test_invalid_grant_reads_as_subject_rejected(self, monkeypatch):
        ok, detail = self._probe_with(
            monkeypatch, Exception("invalid_grant: bad user"))
        assert not ok
        assert "subject rejected" in detail

    def test_success_reports_ok(self, monkeypatch):
        ok, detail = self._probe_with(monkeypatch, None)
        assert ok
        assert detail == ""

    def test_unrecognised_error_is_still_reported_not_swallowed(self, monkeypatch):
        ok, detail = self._probe_with(monkeypatch, Exception("connection reset"))
        assert not ok
        assert "connection reset" in detail


class TestRequiredScopesIsAUnion:
    """The scope list a token request needs is everything the CODE will ask
    for against that tenant -- migration scopes, the seeder's write scopes,
    and account provisioning -- because a token request fails whole if any
    one requested scope is unauthorised."""

    def test_source_includes_directory_write_for_provisioning(self, settings):
        from provision import DIRECTORY_WRITE_SCOPE
        req = vs.required_scopes(settings, "source")
        assert DIRECTORY_WRITE_SCOPE in req

    def test_source_includes_seed_scopes_by_default(self, settings):
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data-generator"))
        from seed_sandbox import SEED_SCOPES
        req = set(vs.required_scopes(settings, "source"))
        assert set(SEED_SCOPES) <= req

    def test_no_seed_excludes_seed_only_scopes(self, settings):
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data-generator"))
        from seed_sandbox import SEED_SCOPES
        with_seed = set(vs.required_scopes(settings, "source", include_seed=True))
        without = set(vs.required_scopes(settings, "source", include_seed=False))
        # Every seed-only scope not otherwise required must disappear.
        seed_only = set(SEED_SCOPES) - without
        assert seed_only & with_seed


class TestMissingSubjectOrKeyFailsClearly:
    def test_no_admin_raises_valueerror_not_systemexit(self, tmp_path, settings):
        """SystemExit derives from BaseException and blows through a plain
        `except Exception` -- dwd_helper's merge fallback catches Exception,
        so raising SystemExit here would crash the whole grant attempt
        instead of degrading to an unmerged submission."""
        settings.source_admin = ""
        try:
            vs.verify(settings, "source", ["https://example.com/scope"])
            assert False, "should have raised"
        except ValueError as exc:
            assert "SOURCE_ADMIN" in str(exc)
        except SystemExit:
            assert False, "must be ValueError, not SystemExit"
