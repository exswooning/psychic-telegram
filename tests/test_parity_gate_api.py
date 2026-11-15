"""The parity gate on the API: the migration page reads one verdict, computed from the ledger."""
import pytest

pytest.importorskip("httpx2", reason="starlette TestClient needs httpx2")
from tests.test_one_to_one_api import _row, _signup, _users, cp  # noqa: E402,F401 - fixture

import parity_gate  # noqa: E402


def test_it_needs_a_login(cp):  # noqa: F811
    assert cp.get("/api/v2/parity-gate").status_code in (401, 403)


def test_a_migration_whose_users_were_all_checked_identical_reads_parity(cp):  # noqa: F811
    aid = _signup(cp)
    _users(cp.ledger, "ann", "bob")
    for u in ("ann", "bob"):
        for s in parity_gate.SERVICES:
            _row(cp.ledger, u, s, "IDENTICAL")
    v = cp.get("/api/v2/parity-gate", params={"account_id": aid}).json()
    assert v["verdict"] == "PARITY" and v["usersAtParity"] == 2 and v["differenceKinds"] == []


def test_differences_come_back_grouped_by_kind_with_who_acts(cp):  # noqa: F811
    aid = _signup(cp)
    _users(cp.ledger, "ann")
    for s in parity_gate.SERVICES:
        _row(cp.ledger, "ann", s, "DIFFERENCES" if s == "drive" else "IDENTICAL")
    v = cp.get("/api/v2/parity-gate", params={"account_id": aid}).json()
    assert v["verdict"] == "NOT_PARITY"
    assert v["differenceKinds"] and v["differenceKinds"][0]["users"] == ["ann@a.com"]
