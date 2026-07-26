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
    s.dry_run = False
    s.owned_only = True
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
