import json

from tests.test_incidents_api import _signup
import error_resolution as E

pytest_plugins = ["tests.test_incidents_api"]


def case(account, message="TypeError: broken handler"):
    import control_plane_db as db

    event = E.parse_line(
        json.dumps(
            {
                "bitport_event": 1,
                "kind": "error",
                "service": "gmail",
                "source_user": "alice@source.example",
                "message": message,
                "version": "a" * 40,
            }
        ),
        {},
    )
    with db.rw() as c:
        return E._case(c, account, event, "a" * 40, E.utc())


def test_anonymous_and_other_accounts_cannot_read_cases_or_connect_runners(cp):
    assert cp.get("/api/v2/error-resolution").status_code in (401, 403)
    mine = _signup(cp, "alice@example.com")
    own = case(mine)
    other = case(mine + 99)
    assert [x["id"] for x in cp.get("/api/v2/error-resolution").json()["cases"]] == [
        own
    ]
    assert cp.get(f"/api/v2/error-resolution/cases/{other}").status_code == 404
    assert cp.post(
        "/api/v2/error-resolution/runners",
        json={"reason": "connect coding worker", "account_id": mine + 99},
    ).status_code in (403, 404)
    assert (
        cp.post(
            "/api/v2/error-resolution/runners",
            json={"reason": "connect all", "all_accounts": True},
        ).status_code
        == 403
    )


def test_scoped_token_only_leases_its_account_and_is_not_a_tenant_credential(
    cp, monkeypatch
):
    mine = _signup(cp, "worker@example.com")
    own = case(mine)
    case(mine + 99)
    r = cp.post(
        "/api/v2/error-resolution/runners",
        json={"reason": "connect local coding worker", "account_id": mine},
    )
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    rid = r.json()["runnerId"]
    headers = {"Authorization": "Bearer " + token}
    assert cp.get("/api/v2/error-resolution").json()["runners"][0]["id"] == rid
    assert token not in cp.get("/api/v2/actions").text
    monkeypatch.setattr(E, "globally_idle", lambda root: True)
    cp.cookies.clear()
    assert cp.get("/api/v2/failures", headers=headers).status_code in (401, 403)
    claimed = cp.post("/api/v2/error-resolution/worker/claim", json={}, headers=headers)
    assert claimed.status_code == 200, claimed.text
    attempt = claimed.json()["attempt"]
    assert attempt["bundle"]["caseId"] == own
    assert "alice@source.example" not in claimed.text
    assert (
        cp.post(
            f"/api/v2/error-resolution/worker/{attempt['attemptId']}/report",
            headers=headers,
            json={"lease": attempt["lease"], "state": "RESOLVED"},
        ).status_code
        == 409
    )


def test_priority_work_blocks_worker_and_read_only_recheck_never_requests_repair(
    cp, monkeypatch
):
    mine = _signup(cp, "owner@example.com")
    cid = case(mine)
    _, token = E.create_runner(mine, "worker")
    monkeypatch.setattr(E, "globally_idle", lambda root: False)
    assert (
        cp.post(
            "/api/v2/error-resolution/worker/claim",
            json={},
            headers={"Authorization": "Bearer " + token},
        ).json()["attempt"]
        is None
    )
    import api_server as api

    launched = []
    monkeypatch.setattr(E, "globally_idle", lambda root: True)
    monkeypatch.setattr(
        api,
        "_run_admitted",
        lambda argv, *args: (launched.append(argv) or True, "started pid 123"),
    )
    r = cp.post(
        f"/api/v2/error-resolution/cases/{cid}/action",
        json={"reason": "verify affected items", "action": "recheck"},
    )
    assert r.json()["ok"], r.text
    assert (
        "--services" in launched[0]
        and "gmail" in launched[0]
        and "--user" in launched[0]
    )
    assert "--repair-after-user" not in launched[0] and "--limit" not in launched[0]


def test_revoke_prevents_future_claims_and_global_policy_requires_superadmin(cp):
    mine = _signup(cp, "owner2@example.com")
    rid, token = E.create_runner(mine, "worker")
    assert (
        cp.post(
            "/api/v2/error-resolution/policy",
            json={"reason": "pause coding", "enabled": False},
        ).status_code
        == 403
    )
    r = cp.post(
        f"/api/v2/error-resolution/runners/{rid}/revoke",
        json={"reason": "revoke worker connection"},
    )
    assert r.json()["ok"]
    assert (
        cp.post(
            "/api/v2/error-resolution/worker/claim",
            json={},
            headers={"Authorization": "Bearer " + token},
        ).status_code
        == 401
    )
