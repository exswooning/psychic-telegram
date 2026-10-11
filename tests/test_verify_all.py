"""Test verify_all: ensure 1:1 verification gates approval."""
import json
from unittest.mock import MagicMock, patch

import pytest

from verify_all import verify_all_users


@pytest.fixture
def mock_db():
    """Mock migration ledger."""
    db = MagicMock()
    db.read.return_value = [
        ("user1@example.com",),
        ("user2@example.com",),
        ("user3@example.com",),
    ]
    return db


def test_verify_all_pass_all_identical(mock_db):
    """All users IDENTICAL: approval should pass."""
    with patch("verify_all.MigrationDB", return_value=mock_db), \
         patch("verify_all.Settings"), \
         patch("verify_all.verify_user", side_effect=["IDENTICAL", "IDENTICAL", "IDENTICAL"]):

        result = verify_all_users("3")

        assert result["verdict"] == "PASS"
        assert result["parity"] == 1.0
        assert result["complete"] == 3
        assert result["users"] == 3
        assert "1:1 identical" in result["message"]


def test_verify_all_fail_one_incomplete(mock_db):
    """One user INCOMPLETE: approval should fail."""
    with patch("verify_all.MigrationDB", return_value=mock_db), \
         patch("verify_all.Settings"), \
         patch("verify_all.verify_user", side_effect=["IDENTICAL", "INCOMPLETE", "IDENTICAL"]):

        result = verify_all_users("3")

        assert result["verdict"] == "FAIL"
        assert result["incomplete"] == 1
        assert "gaps" in result["message"]


def test_verify_all_fail_one_failed(mock_db):
    """One user verification fails (exception): approval should fail."""
    def verify_side_effect(*args, **kwargs):
        if len(verify_side_effect.call_count) < 2:
            verify_side_effect.call_count.append(1)
            return "IDENTICAL"
        raise RuntimeError("Network error")
    verify_side_effect.call_count = []

    with patch("verify_all.MigrationDB", return_value=mock_db), \
         patch("verify_all.Settings"), \
         patch("verify_all.verify_user", side_effect=["IDENTICAL", RuntimeError("Network error"), "IDENTICAL"]):

        result = verify_all_users("3")

        assert result["verdict"] == "FAIL"
        assert result["failed"] == 1


def test_verify_all_no_users(mock_db):
    """No users mapped: special case."""
    mock_db.read.return_value = []

    with patch("verify_all.MigrationDB", return_value=mock_db), \
         patch("verify_all.Settings"):

        result = verify_all_users("3")

        assert result["verdict"] == "NO_USERS_MAPPED"
        assert result["users"] == 0


def test_verify_all_mixed_verdicts(mock_db):
    """Mix of COMPLETE and IDENTICAL: should pass (both count as complete)."""
    with patch("verify_all.MigrationDB", return_value=mock_db), \
         patch("verify_all.Settings"), \
         patch("verify_all.verify_user", side_effect=["COMPLETE", "IDENTICAL", "COMPLETE"]):

        result = verify_all_users("3")

        assert result["verdict"] == "PASS"
        assert result["complete"] == 3


def test_verify_all_short_users_dont_fail(mock_db):
    """SHORT verdicts: parity < 1.0 but not INCOMPLETE, should fail."""
    with patch("verify_all.MigrationDB", return_value=mock_db), \
         patch("verify_all.Settings"), \
         patch("verify_all.verify_user", side_effect=["IDENTICAL", "SHORT", "IDENTICAL"]):

        result = verify_all_users("3")

        # SHORT means some items are missing, so parity != 1.0 -> FAIL
        assert result["verdict"] == "FAIL"
        assert result["parity"] < 1.0
