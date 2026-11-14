"""Scoped live-canary transport: all Google calls are mocked here."""

import sqlite3
import os
import sys
from types import SimpleNamespace

import pytest
import candidate_canary as canary
import error_resolution as E
from tests.test_error_resolution_api import case
from tests.test_incidents_api import _signup

pytest_plugins = ["tests.test_incidents_api"]


def start(cp, monkeypatch):
    account = _signup(cp, "canary-owner@example.invalid")
    cid = case(account)
    _, token = E.create_runner(account, "test canary")
    headers = {"Authorization": "Bearer " + token}
    monkeypatch.setattr(E, "globally_idle", lambda root: True)
    monkeypatch.setattr(canary, "_installed_release", lambda root: "a" * 40)
    monkeypatch.setattr(
        canary.candidate_scope,
        "validate_proof",
        lambda proof, candidate, base: "f" * 64,
    )
    attempt = cp.post(
        "/api/v2/error-resolution/worker/claim", headers=headers, json={}
    ).json()["attempt"]
    commit = "b" * 40
    endpoint = f"/api/v2/error-resolution/worker/{attempt['attemptId']}"
    report = {
        "lease": attempt["lease"],
        "state": "TESTING",
        "candidate_commit": commit,
        "source_commit": "a" * 40,
    }
    assert (
        cp.post(endpoint + "/report", headers=headers, json=report).status_code == 200
    )
    return cid, attempt, commit, endpoint, headers


def test_sandbox_probe_is_scoped_budgeted_and_never_returns_provider_data(
    cp, monkeypatch
):
    cid, attempt, commit, endpoint, headers = start(cp, monkeypatch)
    calls = []
    monkeypatch.setattr(
        canary,
        "gmail_read",
        lambda side, slot: calls.append((side, slot)) or {"status": 200},
    )

    def request(side, slot, **overrides):
        body = {
            "lease": attempt["lease"],
            "candidate_commit": commit,
            "proof": {},
            "side": side,
            "slot": slot,
            **overrides,
        }
        return cp.post(endpoint + "/canary", headers=headers, json=body)

    assert request("customer", 0).status_code == 409
    assert request("source", 2).status_code == 409
    assert request("source", 0, candidate_commit="c" * 40).status_code == 409
    assert request("source", 0, lease="x" * 32).status_code == 409
    for side in ("source", "target"):
        for slot in (0, 1):
            response = request(side, slot)
            assert response.status_code == 200 and response.json() == {"status": 200}
    assert calls == [("source", 0), ("source", 1), ("target", 0), ("target", 1)]
    evidence = {
        "testsPassed": True,
        "qualified": True,
        "qualificationScenario": "gmail-quota-live-v1",
        "artifactHash": "f" * 64,
        "deploymentConfigured": False,
    }
    response = cp.post(
        endpoint + "/report",
        headers=headers,
        json={
            "lease": attempt["lease"],
            "state": "WAITING_DEPLOYMENT",
            "candidate_commit": commit,
            "source_commit": "a" * 40,
            "evidence": evidence,
        },
    )
    assert response.status_code == 200, response.text
    detail = E.case_detail(cid)
    assert (
        detail["state"] == "WAITING_DEPLOYMENT" and detail["attempts"][0]["finished_at"]
    )
    assert request("source", 0).status_code == 409


def test_canary_cannot_claim_qualification_after_missing_or_failed_slot(
    cp, monkeypatch
):
    _, attempt, commit, endpoint, headers = start(cp, monkeypatch)
    monkeypatch.setattr(canary, "gmail_read", lambda side, slot: {"status": 200})
    body = {
        "lease": attempt["lease"],
        "candidate_commit": commit,
        "proof": {},
        "side": "source",
        "slot": 0,
    }
    assert cp.post(endpoint + "/canary", headers=headers, json=body).status_code == 200
    evidence = {
        "testsPassed": True,
        "qualified": True,
        "qualificationScenario": "gmail-quota-live-v1",
        "artifactHash": "f" * 64,
        "deploymentConfigured": False,
    }
    r = cp.post(
        endpoint + "/report",
        headers=headers,
        json={
            "lease": attempt["lease"],
            "state": "WAITING_DEPLOYMENT",
            "candidate_commit": commit,
            "source_commit": "a" * 40,
            "evidence": evidence,
        },
    )
    assert r.status_code == 409

    # Failed Google reads consume the budget, and the underlying error is never exposed.
    def failed(*args):
        raise RuntimeError("https://gmail.example/private-id?token=secret")

    monkeypatch.setattr(canary, "gmail_read", failed)
    body["side"] = "target"
    for _ in range(canary.MAX_READS_PER_SIDE):
        r = cp.post(endpoint + "/canary", headers=headers, json=body)
        assert (
            r.status_code == 409
            and "secret" not in r.text
            and "private-id" not in r.text
        )
    assert (
        "budget exhausted"
        in cp.post(endpoint + "/canary", headers=headers, json=body).text
    )


def test_canary_refuses_busy_server_before_any_provider_read(cp, monkeypatch):
    _, attempt, commit, endpoint, headers = start(cp, monkeypatch)
    monkeypatch.setattr(E, "globally_idle", lambda root: False)
    monkeypatch.setattr(
        canary,
        "gmail_read",
        lambda *args: (_ for _ in ()).throw(AssertionError("unexpected read")),
    )
    r = cp.post(
        endpoint + "/canary",
        headers=headers,
        json={
            "lease": attempt["lease"],
            "candidate_commit": commit,
            "proof": {},
            "side": "source",
            "slot": 0,
        },
    )
    assert r.status_code == 409 and "priority" in r.text


def test_canary_refuses_when_coding_policy_is_paused(cp, monkeypatch):
    _, attempt, commit, endpoint, headers = start(cp, monkeypatch)
    monkeypatch.setattr(
        canary,
        "gmail_read",
        lambda *args: (_ for _ in ()).throw(AssertionError("unexpected read")),
    )
    E.set_policy(False, 3)
    r = cp.post(
        endpoint + "/canary",
        headers=headers,
        json={
            "lease": attempt["lease"],
            "candidate_commit": commit,
            "proof": {},
            "side": "source",
            "slot": 0,
        },
    )
    assert r.status_code == 409 and "paused" in r.text


def test_new_occurrence_invalidates_the_inflight_candidate_canary(cp, monkeypatch):
    cid, attempt, commit, endpoint, headers = start(cp, monkeypatch)
    calls = []
    monkeypatch.setattr(
        canary,
        "gmail_read",
        lambda side, slot: calls.append((side, slot)) or {"status": 200},
    )
    body = {
        "lease": attempt["lease"],
        "candidate_commit": commit,
        "proof": {},
        "side": "source",
        "slot": 0,
    }
    assert cp.post(endpoint + "/canary", headers=headers, json=body).status_code == 200
    assert case(E.case_detail(cid)["account_id"]) == cid
    body["slot"] = 1
    result = cp.post(endpoint + "/canary", headers=headers, json=body)
    assert result.status_code == 409 and "case changed" in result.text
    assert calls == [("source", 0)]


def test_sandbox_pair_and_mapping_are_required(tmp_path, monkeypatch):
    path = tmp_path / "ledger.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE identity_map(source_email TEXT, target_email TEXT)")
        db.execute(
            "INSERT INTO identity_map VALUES(?,?)",
            (canary.SOURCE_USER, canary.TARGET_USER),
        )
    current = SimpleNamespace(
        source_domain=canary.SOURCE_DOMAIN,
        target_domain=canary.TARGET_DOMAIN,
        auth_mode="key",
        db_path=str(path),
    )
    import config

    monkeypatch.setattr(config, "Settings", lambda **kwargs: current)
    assert canary._sandbox_settings() is current
    current.target_domain = "customer.example"
    import pytest

    with pytest.raises(canary.CanaryRefused):
        canary._sandbox_settings()
    current.target_domain = canary.TARGET_DOMAIN
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE identity_map SET target_email=?",
            ("someone-else@" + canary.TARGET_DOMAIN,),
        )
    with pytest.raises(canary.CanaryRefused):
        canary._sandbox_settings()


def test_held_candidate_can_be_requalified_without_another_coding_attempt(
    cp, monkeypatch
):
    cid, attempt, commit, endpoint, headers = start(cp, monkeypatch)
    report = {
        "lease": attempt["lease"],
        "state": "WAITING_QUALIFICATION",
        "source_commit": "a" * 40,
        "candidate_commit": commit,
        "evidence": {"testsPassed": True},
    }
    assert (
        cp.post(endpoint + "/report", headers=headers, json=report).status_code == 200
    )
    # The authenticated operator, not the worker credential, requests the retry.
    response = cp.post(
        f"/api/v2/error-resolution/cases/{cid}/action",
        json={"reason": "retry candidate qualification", "action": "queue"},
    )
    assert response.json()["ok"]
    result = cp.post(
        "/api/v2/error-resolution/worker/claim", headers=headers, json={}
    ).json()["attempt"]
    assert result["resume"] and result["attemptId"] == attempt["attemptId"]
    assert result["candidateCommit"] == commit and result["sourceCommit"] == "a" * 40
    assert result["evidence"]["testsPassed"] is True
    assert len(E.case_detail(cid)["attempts"]) == 1


def test_qualified_evidence_cannot_be_reassigned_to_another_commit(cp, monkeypatch):
    _, attempt, commit, endpoint, headers = start(cp, monkeypatch)
    monkeypatch.setattr(canary, "gmail_read", lambda side, slot: {"status": 200})
    for side in ("source", "target"):
        for slot in (0, 1):
            assert (
                cp.post(
                    endpoint + "/canary",
                    headers=headers,
                    json={
                        "lease": attempt["lease"],
                        "candidate_commit": commit,
                        "proof": {},
                        "side": side,
                        "slot": slot,
                    },
                ).status_code
                == 200
            )
    evidence = {
        "testsPassed": True,
        "qualified": True,
        "qualificationScenario": "gmail-quota-live-v1",
        "artifactHash": "f" * 64,
        "deploymentConfigured": False,
    }
    r = cp.post(
        endpoint + "/report",
        headers=headers,
        json={
            "lease": attempt["lease"],
            "state": "WAITING_DEPLOYMENT",
            "candidate_commit": "c" * 40,
            "evidence": evidence,
        },
    )
    assert r.status_code == 409


def test_provider_calls_are_fixed_to_read_only_operations_and_status_only(monkeypatch):
    import google_auth_httplib2
    import google.oauth2.service_account as service_account
    import googleapiclient.discovery

    settings = SimpleNamespace(source_sa_key="source-key", target_sa_key="target-key")
    monkeypatch.setattr(canary, "_sandbox_settings", lambda: settings)
    seen = []

    class Credentials:
        @classmethod
        def from_service_account_file(cls, key, scopes):
            seen.append(("credentials", key, scopes))
            return cls()

        def with_subject(self, user):
            seen.append(("subject", user))
            return self

    class Resource:
        def users(self):
            return self

        def messages(self):
            return self

        def list(self, **kwargs):
            seen.append(("list", kwargs))
            return SimpleNamespace(
                execute=lambda: {"messages": [{"id": "PRIVATE_1"}, {"id": "PRIVATE_2"}]}
            )

        def get(self, **kwargs):
            seen.append(("get", kwargs))
            return SimpleNamespace(
                execute=lambda: {"id": "PRIVATE_2", "snippet": "PRIVATE_CONTENT"}
            )

        def close(self):
            pass

    monkeypatch.setattr(service_account, "Credentials", Credentials)
    monkeypatch.setattr(google_auth_httplib2, "AuthorizedHttp", lambda *a, **k: None)
    monkeypatch.setattr(googleapiclient.discovery, "build", lambda *a, **k: Resource())
    assert canary.gmail_read("source", 1) == {"status": 200}
    assert seen[0] == (
        "credentials",
        "source-key",
        ["https://www.googleapis.com/auth/gmail.readonly"],
    )
    assert seen[1] == ("subject", canary.SOURCE_USER)
    assert seen[2][0] == "list" and seen[3][0] == "get"
    assert seen[3][1] == {
        "userId": "me",
        "id": "PRIVATE_2",
        "format": "minimal",
        "fields": "id",
    }
    seen.clear()
    assert canary.gmail_read("target", 0) == {"status": 200}
    assert seen[0] == (
        "credentials",
        "target-key",
        ["https://www.googleapis.com/auth/gmail.modify"],
    )
    assert seen[1] == ("subject", canary.TARGET_USER)


def test_actual_git_proof_is_required_for_live_canary_and_qualification(
    cp, monkeypatch, tmp_path
):
    import hashlib
    import candidate_scope
    from tests.test_candidate_qualification import candidate_repo

    _, _, tree, _, base, commit = candidate_repo(tmp_path)
    account = _signup(cp, "git-proof@example.invalid")
    cid = case(account)
    _, token = E.create_runner(account, "proof test")
    headers = {"Authorization": "Bearer " + token}
    monkeypatch.setattr(E, "globally_idle", lambda root: True)
    monkeypatch.setattr(canary, "_installed_release", lambda root: base)
    reads = []
    monkeypatch.setattr(
        canary,
        "gmail_read",
        lambda side, slot: reads.append((side, slot)) or {"status": 200},
    )
    attempt = cp.post(
        "/api/v2/error-resolution/worker/claim", headers=headers, json={}
    ).json()["attempt"]
    endpoint = f"/api/v2/error-resolution/worker/{attempt['attemptId']}"
    assert (
        cp.post(
            endpoint + "/report",
            headers=headers,
            json={
                "lease": attempt["lease"],
                "state": "TESTING",
                "source_commit": base,
                "candidate_commit": commit,
            },
        ).status_code
        == 200
    )
    proof = candidate_scope.build_proof(tree, commit, base)
    bad = dict(proof)
    bad["candidate_blob"] = bad["parent_blob"]
    body = {
        "lease": attempt["lease"],
        "candidate_commit": commit,
        "proof": bad,
        "side": "source",
        "slot": 0,
    }
    assert cp.post(endpoint + "/canary", headers=headers, json=body).status_code == 409
    assert not reads
    body["proof"] = proof
    for side in ("source", "target"):
        for slot in (0, 1):
            body.update(side=side, slot=slot)
            assert cp.post(endpoint + "/canary", headers=headers, json=body).json() == {
                "status": 200
            }
    artifact_hash = hashlib.sha256(
        (tree / "verify_gmail_quota.py").read_bytes()
    ).hexdigest()
    evidence = {
        "testsPassed": True,
        "qualified": True,
        "qualificationScenario": "gmail-quota-live-v1",
        "artifactHash": artifact_hash,
        "deploymentConfigured": False,
    }
    result = cp.post(
        endpoint + "/report",
        headers=headers,
        json={
            "lease": attempt["lease"],
            "state": "WAITING_DEPLOYMENT",
            "source_commit": base,
            "candidate_commit": commit,
            "evidence": evidence,
        },
    )
    assert result.status_code == 200, result.text
    assert E.case_detail(cid)["state"] == "WAITING_DEPLOYMENT"


@pytest.mark.skipif(
    os.environ.get("BITPORT_OS_QUALIFICATION") != "1" or sys.platform != "darwin",
    reason="Explicit macOS OS-isolation qualification only",
)
def test_held_candidate_resumes_through_real_isolation_and_scoped_api(
    cp, monkeypatch, tmp_path
):
    import incident_worker as worker
    from tests.test_candidate_qualification import candidate_repo

    repo, _, tree, _, base, commit = candidate_repo(tmp_path)
    account = _signup(cp, "end-to-end@example.invalid")
    cid = case(account)
    _, token = E.create_runner(account, "end-to-end worker")
    headers = {"Authorization": "Bearer " + token}
    monkeypatch.setattr(E, "globally_idle", lambda root: True)
    monkeypatch.setattr(canary, "_installed_release", lambda root: base)
    reads = []
    monkeypatch.setattr(
        canary,
        "gmail_read",
        lambda side, slot: reads.append((side, slot)) or {"status": 200},
    )
    first = cp.post(
        "/api/v2/error-resolution/worker/claim", headers=headers, json={}
    ).json()["attempt"]
    endpoint = f"/api/v2/error-resolution/worker/{first['attemptId']}/report"
    assert (
        cp.post(
            endpoint,
            headers=headers,
            json={
                "lease": first["lease"],
                "state": "TESTING",
                "source_commit": base,
                "candidate_commit": commit,
                "evidence": {"testsPassed": True},
            },
        ).status_code
        == 200
    )
    assert (
        cp.post(
            endpoint,
            headers=headers,
            json={
                "lease": first["lease"],
                "state": "WAITING_QUALIFICATION",
                "source_commit": base,
                "candidate_commit": commit,
                "evidence": {"testsPassed": True},
            },
        ).status_code
        == 200
    )
    assert cp.post(
        f"/api/v2/error-resolution/cases/{cid}/action",
        json={"reason": "retry candidate qualification", "action": "queue"},
    ).json()["ok"]
    resumed = cp.post(
        "/api/v2/error-resolution/worker/claim", headers=headers, json={}
    ).json()["attempt"]

    class Client:
        def call(self, path, body):
            response = cp.post(
                "/api/v2/error-resolution/worker/" + path, headers=headers, json=body
            )
            if response.status_code != 200:
                raise RuntimeError("fixture API failed: " + response.text)
            return response.json()

    worker.resume_qualification(Client(), resumed, repo, tmp_path, {})
    detail = E.case_detail(cid)
    assert detail["state"] == "WAITING_DEPLOYMENT", detail["history"]
    assert (
        len(detail["attempts"]) == 1
        and detail["attempts"][0]["candidate_commit"] == commit
    )
    assert reads == [("source", 0), ("source", 1), ("target", 0), ("target", 1)]
    assert (tree / "verify_gmail_quota.py").is_file()
