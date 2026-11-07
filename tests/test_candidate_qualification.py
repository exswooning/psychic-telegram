"""Candidate identity, scenario coverage and isolated sandbox relay."""

import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

import candidate_qualification as qualification
import incident_worker as worker

SOURCE = Path(__file__).resolve().parents[1]


def candidate_repo(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", "--shared", "-q", str(SOURCE), str(repo)], check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Qualification Test"], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "qualification@example.invalid"],
        cwd=repo,
        check=True,
    )
    (repo / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    base = worker.git(repo, "rev-parse", "HEAD")
    private = tmp_path / "attempt-1"
    private.mkdir()
    tree = private / "source"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(tree), base],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    home = private / "checks-home"
    home.mkdir()
    original = (tree / "verify_gmail_quota.py").read_text()
    old = '{"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}'
    assert old in original
    (tree / "verify_gmail_quota.py").write_text(
        original.replace(old, old[:-1] + ', "RESOURCE_EXHAUSTED"}')
    )
    (tree / "tests/test_candidate_resource_exhausted.py").write_text(
        "from types import SimpleNamespace\nimport verify_gmail_quota as Q\n"
        "def test_resource_exhausted(monkeypatch):\n"
        "    monkeypatch.setattr(Q, '_extract_reason', lambda exc: 'RESOURCE_EXHAUSTED')\n"
        "    assert Q.is_quota_error(SimpleNamespace(resp=SimpleNamespace(status=403)))\n"
    )
    subprocess.run(
        [
            "git",
            "add",
            "verify_gmail_quota.py",
            "tests/test_candidate_resource_exhausted.py",
        ],
        cwd=tree,
        check=True,
    )
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture quota fix"],
        cwd=tree,
        check=True,
    )
    return repo, private, tree, home, base, worker.git(tree, "rev-parse", "HEAD")


def test_scenario_rejects_wrong_service_or_candidate(tmp_path):
    repo, _, tree, _, base, commit = candidate_repo(tmp_path)
    assert qualification.supported(tree, base, commit, {"service": "gmail"})
    assert not qualification.supported(tree, base, commit, {"service": "drive"})
    assert not qualification.supported(tree, base, "c" * 40, {"service": "gmail"})
    (tree / "verify_sample.py").write_text("unexpected tracked modification")
    assert not qualification.supported(tree, base, commit, {"service": "gmail"})
    assert worker.git(repo, "rev-parse", "HEAD") == base


def test_qualification_rejects_uncovered_changes(tmp_path):
    repo, _, tree, _, base, _ = candidate_repo(tmp_path)
    (tree / "verify_sample.py").write_text("unexpected tracked modification")
    subprocess.run(["git", "add", "verify_sample.py"], cwd=tree, check=True)
    subprocess.run(
        ["git", "commit", "--amend", "-qm", "extra change"], cwd=tree, check=True
    )
    new_commit = worker.git(tree, "rev-parse", "HEAD")
    assert not qualification.supported(tree, base, new_commit, {"service": "gmail"})


def test_qualification_rejects_active_code_or_changes_outside_classifier(tmp_path):
    _, _, tree, _, base, _ = candidate_repo(tmp_path)
    quota_file = tree / "verify_gmail_quota.py"
    body = quota_file.read_text()
    quota_file.write_text(
        body.replace(
            "    status = getattr(",
            "    __import__('os').system('false')\n    status = getattr(",
            1,
        )
    )
    subprocess.run(["git", "add", "verify_gmail_quota.py"], cwd=tree, check=True)
    subprocess.run(
        ["git", "commit", "--amend", "-qm", "unsafe classifier"], cwd=tree, check=True
    )
    assert not qualification.supported(
        tree, base, worker.git(tree, "rev-parse", "HEAD"), {"service": "gmail"}
    )


def test_existing_candidate_can_resume_without_calling_the_coding_model(
    tmp_path, monkeypatch
):
    repo, _, _, _, base, commit = candidate_repo(tmp_path)
    reports = []

    class Client:
        def call(self, path, body):
            if path.endswith("/report"):
                reports.append(body.copy())
            return {"ok": True}

    monkeypatch.setattr(
        qualification,
        "qualify",
        lambda *args: (
            True,
            "passed",
            {"qualificationScenario": "gmail-quota-live-v1", "artifactHash": "f" * 64},
        ),
    )
    monkeypatch.setattr(
        worker,
        "run_process",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unexpected model or deployment")
        ),
    )
    worker.resume_qualification(
        Client(),
        {
            "attemptId": 1,
            "lease": "x" * 32,
            "sourceCommit": base,
            "candidateCommit": commit,
            "bundle": {"caseId": 7, "service": "gmail"},
            "evidence": {"testsPassed": True},
        },
        repo,
        tmp_path,
        {},
    )
    assert [r["state"] for r in reports] == ["WAITING_DEPLOYMENT"]
    assert reports[0]["candidate_commit"] == commit
    assert reports[0]["evidence"]["deploymentConfigured"] is False


@pytest.mark.skipif(
    os.environ.get("BITPORT_OS_QUALIFICATION") != "1" or sys.platform != "darwin",
    reason="Explicit macOS OS-isolation qualification only",
)
def test_real_candidate_runs_pinned_contract_and_sandbox_relay(tmp_path):
    repo, private, tree, home, base, commit = candidate_repo(tmp_path)
    assert qualification.supported(tree, base, commit, {"service": "gmail"})
    seen = []

    class Client:
        def call(self, path, body):
            assert path == "7/canary" and body["lease"] == "x" * 32
            assert body["candidate_commit"] == commit
            seen.append((body["side"], body["slot"]))
            return {"status": 200}

    passed, detail, evidence = qualification.qualify(
        Client(),
        7,
        "x" * 32,
        commit,
        tree,
        repo,
        private,
        home,
        threading.Event(),
        base,
        {"service": "gmail"},
    )
    assert passed, (
        detail,
        (private / "qualification-contract.log").read_text(errors="replace"),
        (private / "sandbox-canary.log").read_text(errors="replace")
        if (private / "sandbox-canary.log").exists()
        else "",
    )
    assert seen == [("source", 0), ("source", 1), ("target", 0), ("target", 1)]
    assert evidence["qualificationScenario"] == "gmail-quota-live-v1"
    assert evidence["artifactHash"] == qualification._digest(
        tree / "verify_gmail_quota.py"
    )
    assert worker.git(repo, "rev-parse", "HEAD") == base


@pytest.mark.skipif(
    os.environ.get("BITPORT_OS_QUALIFICATION") != "1" or sys.platform != "darwin",
    reason="Explicit macOS OS-isolation qualification only",
)
def test_live_canary_failure_keeps_candidate_unqualified(tmp_path):
    repo, private, tree, home, base, commit = candidate_repo(tmp_path)

    class Refused:
        def call(self, path, body):
            raise RuntimeError("sandbox busy or unavailable")

    passed, detail, evidence = qualification.qualify(
        Refused(),
        7,
        "x" * 32,
        commit,
        tree,
        repo,
        private,
        home,
        threading.Event(),
        base,
        {"service": "gmail"},
    )
    assert not passed and "held" in detail and not evidence
