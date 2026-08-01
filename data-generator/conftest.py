"""
data-generator/conftest.py
==========================
Shared fixtures for the seeder's own offline test suite (test_seed_sandbox.py).
Puts the repo root (for config/resilience/tests.fakes) and this directory
(for corpus/seed_sandbox) on sys.path, mirroring what the standalone scripts
in this directory already do via their own sys.path.insert calls.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import drive_engine  # noqa: E402
import gmail_engine  # noqa: E402
from config import Settings  # noqa: E402
from db import MigrationDB  # noqa: E402
from tests.fakes import FakeAuth, FakeDownloader, FakeMediaUpload  # noqa: E402


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings()
    s.db_path = str(tmp_path / "migration.db")
    s.scratch_dir = str(tmp_path / "scratch")
    s.source_domain = "tenanta.com"
    s.target_domain = "tenantb.com"
    s.max_retries = 4
    s.base_backoff = 0.001
    s.max_backoff = 0.004
    s.per_user_qps = 10_000.0
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
def auth(settings, monkeypatch) -> FakeAuth:
    monkeypatch.setattr(drive_engine, "MediaFileUpload", FakeMediaUpload)
    monkeypatch.setattr(drive_engine, "MediaIoBaseDownload", FakeDownloader)
    monkeypatch.setattr(gmail_engine, "MediaFileUpload", FakeMediaUpload)
    return FakeAuth(settings)
