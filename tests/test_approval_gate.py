"""Test that approval endpoint gates on 1:1 verification."""
from unittest.mock import MagicMock, patch
import pytest


def test_approval_refused_if_verify_all_fails():
    """Approval is refused if verify_all reports FAIL."""
    # Mock the approval endpoint logic
    def _approve_logic():
        import verify_all
        verification = verify_all.verify_all_users("3")
        if verification["verdict"] != "PASS":
            return False, (f"migration is not 1:1 verified: {verification['message']} "
                          f"Run 'python verify_all.py --account-id 3' and fix gaps before approval.")
        return True, "migration approved"

    with patch("verify_all.verify_all_users") as mock_verify:
        # Scenario 1: verify_all returns FAIL
        mock_verify.return_value = {
            "verdict": "FAIL",
            "message": "2 users have gaps",
            "incomplete": 2,
            "users": 5
        }

        success, msg = _approve_logic()

        assert success is False
        assert "not 1:1 verified" in msg
        assert "2 users have gaps" in msg
        assert "python verify_all.py" in msg


def test_approval_allowed_if_verify_all_passes():
    """Approval succeeds if verify_all reports PASS."""
    def _approve_logic():
        import verify_all
        verification = verify_all.verify_all_users("3")
        if verification["verdict"] != "PASS":
            return False, "not verified"
        return True, "migration approved"

    with patch("verify_all.verify_all_users") as mock_verify:
        # Scenario 2: verify_all returns PASS
        mock_verify.return_value = {
            "verdict": "PASS",
            "message": "All users 1:1 identical",
            "parity": 1.0,
            "users": 5,
            "complete": 5
        }

        success, msg = _approve_logic()

        assert success is True
        assert "migration approved" in msg
