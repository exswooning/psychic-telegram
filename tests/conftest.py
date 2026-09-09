"""
tests/conftest.py
=================
Shared fixtures. Every test runs against the *real* engine modules — only the
Google transport and the media helpers are swapped for fakes.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import calendar_engine  # noqa: E402
import drive_engine  # noqa: E402
import gmail_engine  # noqa: E402
from config import Settings  # noqa: E402
from db import MigrationDB  # noqa: E402
from resilience import DailyQuotaGuard  # noqa: E402
from tests.fakes import FakeAuth, FakeDownloader, FakeMediaUpload  # noqa: E402

SRC_USER = "alice@tenanta.com"
TGT_USER = "alice@tenantb.com"


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings()
    s.db_path = str(tmp_path / "migration.db")
    s.scratch_dir = str(tmp_path / "scratch")
    s.source_domain = "tenanta.com"
    s.target_domain = "tenantb.com"
    # Keep the suite fast: real backoff would make the retry tests take minutes.
    s.max_retries = 4
    s.base_backoff = 0.001
    s.max_backoff = 0.004
    s.per_user_qps = 10_000.0     # effectively disable pacing in tests
    # Same reason, for the write bucket: its real default is Google's 3/sec
    # ceiling, which would pace every faked write and turn a 14s suite into
    # a multi-minute one. Tests that care about pacing set it themselves.
    s.drive_write_qps = 10_000.0
    s.dry_run = False
    s.owned_only = True
    # Off here, on in production. gmail_engine refuses to start when this is
    # on and no Drive has migrated -- correct for a real run, and it would
    # otherwise fail every mail test that never asked about rewriting. Tests
    # that DO ask set it True and seed a Drive mapping; the production
    # default is pinned in test_rewrite_toggle.py, not here.
    s.rewrite_drive_links = False
    os.makedirs(s.scratch_dir, exist_ok=True)
    return s


@pytest.fixture
def db(settings) -> MigrationDB:
    d = MigrationDB(settings.db_path)
    yield d
    d.close()


@pytest.fixture
def identity(db):
    """The default one-user mapping used by most tests."""
    from db import bulk_seed_identities

    bulk_seed_identities(db, [(SRC_USER, TGT_USER)])
    return db


@pytest.fixture(autouse=True)
def _reset_chat_shared_state():
    """FakeChat shares one store per tenant (as real Chat does), so it has to
    be cleared between tests or spaces leak across them."""
    from tests.fakes import FakeChat

    FakeChat.reset_shared()
    yield
    FakeChat.reset_shared()


@pytest.fixture(autouse=True)
def _sandbox_is_unprotected(tmp_path_factory, monkeypatch):
    """These tests operate a sandbox, so give them one.

    domain_guard protects every CONFIGURED domain by default -- the setup
    wizard writing a tenant_configs row IS the protection. That is the right
    default for a deployment and the wrong one for a suite: almost every
    test here builds a seed or reset command against whatever domain it just
    put in the environment, which the guard then correctly refuses.

    So configured_domains() is emptied for the suite. What remains is the
    behaviour these tests were written against and still assert: an explicit
    PROTECTED_DOMAINS entry refuses, and nothing else does. Protection by
    default is exercised directly, and only, in test_domain_guard.py, which
    patches this same seam itself and is therefore unaffected.

    REVOCATIONS_PATH is redirected too, so no test can write a revocation
    into the checkout -- an unprotected_domains.json left behind by a test
    run would travel to a deployment as real state.
    """
    import domain_guard

    monkeypatch.setattr(
        domain_guard, "REVOCATIONS_PATH",
        str(tmp_path_factory.mktemp("guard") / "unprotected.json"))
    monkeypatch.setattr(domain_guard, "configured_domains", lambda: set())
    yield


@pytest.fixture(autouse=True)
def _cleanup_account_dirs():
    """accounts_auth.create_account() writes real directories under
    data/accounts/{id}/ and keys/{id}/, relative to the actual repo
    checkout (accounts_auth.HERE) -- not a tmp_path, because the whole
    point is that they need to exist at a stable path across process
    restarts. Every test that signs up an account (directly or through
    api_server.py's /api/v2/auth/signup) would otherwise leave numbered
    directories behind in the real repo on every run. Snapshot which
    numeric dirs exist before, sweep anything new after.
    """
    import shutil

    import accounts_auth

    def _numeric_dirs(base: str) -> set[str]:
        if not os.path.isdir(base):
            return set()
        return {name for name in os.listdir(base) if name.isdigit()}

    data_accounts = os.path.join(accounts_auth.HERE, "data", "accounts")
    keys_dir = os.path.join(accounts_auth.HERE, "keys")
    before_data = _numeric_dirs(data_accounts)
    before_keys = _numeric_dirs(keys_dir)
    yield
    for name in _numeric_dirs(data_accounts) - before_data:
        shutil.rmtree(os.path.join(data_accounts, name), ignore_errors=True)
    for name in _numeric_dirs(keys_dir) - before_keys:
        shutil.rmtree(os.path.join(keys_dir, name), ignore_errors=True)


@pytest.fixture
def auth(settings, monkeypatch) -> FakeAuth:
    """
    Swap the media helpers in the engine modules for in-memory doubles.

    Note we patch the *module attribute* the engine actually calls, not the
    googleapiclient package — so a refactor that changes how the engine imports
    these will surface as a test failure rather than silently bypassing the fake.
    """
    monkeypatch.setattr(drive_engine, "MediaFileUpload", FakeMediaUpload)
    monkeypatch.setattr(drive_engine, "MediaIoBaseDownload", FakeDownloader)
    monkeypatch.setattr(gmail_engine, "MediaFileUpload", FakeMediaUpload)
    return FakeAuth(settings)


@pytest.fixture
def quota(db, settings) -> DailyQuotaGuard:
    return DailyQuotaGuard(db, TGT_USER, settings.effective_upload_cap())


@pytest.fixture
def migrator(auth, db, settings, identity, quota):
    """A DriveMigrator wired to empty source and target fakes."""
    return drive_engine.DriveMigrator(
        auth, db, settings, SRC_USER, TGT_USER, quota
    )


@pytest.fixture
def gmail_migrator(auth, db, settings, identity):
    return gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER)


@pytest.fixture
def cal_migrator(auth, db, settings, identity):
    return calendar_engine.CalendarMigrator(auth, db, settings, SRC_USER, TGT_USER)
