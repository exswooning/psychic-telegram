"""Test repair_gaps: auto-repair of verification gaps until 1:1."""
from unittest.mock import patch
import pytest


def test_auto_repair_all_passes_if_no_gaps():
    """If verify_all returns PASS, repair_gaps should return PASS immediately."""
    with patch("verify_all.verify_all_users") as mock_verify:
        mock_verify.return_value = {
            "verdict": "PASS",
            "message": "All users 1:1",
            "parity": 1.0
        }

        import repair_gaps
        result = repair_gaps.auto_repair_all("3")

        assert result["verdict"] == "PASS"
        assert result["repaired"] == 0


def test_auto_repair_all_detects_and_repairs_short_users():
    """If verify_all finds SHORT users, repair_gaps should attempt repair."""
    import repair_gaps

    with patch("verify_all.verify_all_users") as mock_verify, \
         patch("repair_gaps.repair_user_gaps") as mock_repair:

        mock_verify.side_effect = [
            {
                "verdict": "FAIL",
                "message": "1 user SHORT",
                "parity": 0.8,
                "details": {
                    "SHORT": ["user1@example.com"],
                    "INCOMPLETE": [],
                    "FAILED": []
                }
            },
            {
                "verdict": "PASS",
                "message": "All users 1:1",
                "parity": 1.0,
                "details": {}
            }
        ]

        mock_repair.return_value = True
        result = repair_gaps.auto_repair_all("3")

        assert result["verdict"] == "PASS"
        assert result["repaired"] == 1
        assert result["still_broken"] == 0
        mock_repair.assert_called_once()


def test_auto_repair_all_returns_partial_success():
    """If some users repair and others don't, report accurately."""
    import repair_gaps

    with patch("verify_all.verify_all_users") as mock_verify, \
         patch("repair_gaps.repair_user_gaps") as mock_repair:

        mock_verify.side_effect = [
            {
                "verdict": "FAIL",
                "parity": 0.6,
                "details": {
                    "SHORT": ["user1@example.com", "user2@example.com"],
                    "INCOMPLETE": [],
                    "FAILED": []
                }
            },
            {
                "verdict": "FAIL",
                "parity": 0.8,
                "details": {
                    "SHORT": ["user2@example.com"],
                    "INCOMPLETE": [],
                    "FAILED": []
                }
            }
        ]

        mock_repair.side_effect = [True, False]
        result = repair_gaps.auto_repair_all("3")

        assert result["verdict"] == "FAIL"
        assert result["repaired"] == 1
        assert result["still_broken"] == 1
