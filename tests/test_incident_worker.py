from pathlib import Path
import io
import json
import subprocess
import urllib.error

import pytest

import incident_worker as W


def test_canary_failure_reports_only_safe_actionable_reason():
    client = W.Client({"apiUrl": "http://127.0.0.1", "token": "x" * 40})

    def fail(detail):
        def open_request(*args, **kwargs):
            raise urllib.error.HTTPError(
                "http://local",
                409,
                "refused",
                {},
                io.BytesIO(json.dumps({"detail": detail}).encode()),
            )

        client.http.open = open_request
        with pytest.raises(RuntimeError) as exc:
            client.call("7/canary", {})
        return str(exc.value)

    assert "mapping has changed" in fail(
        "The authorized sandbox user mapping has changed"
    )
    assert "HTTP 403" in fail("Sandbox Gmail metadata read failed (HTTP 403)")
    assert "secret" not in fail("token=secret and private data")


def test_candidate_runs_regression_on_base_then_tests_patch_and_stops_without_qualification(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in [("user.name", "Tests"), ("user.email", "tests@example.com")]:
        subprocess.run(["git", "config", key, value], cwd=repo, check=True)
    (repo / "engine.py").write_text("VALUE=1\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_existing.py").write_text("def test_original(): pass\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, check=True)
    original = W.git(repo, "rev-parse", "HEAD")
    calls = []

    class Client:
        def call(self, path, body):
            calls.append((path, body.copy()))
            return {"ok": True}

    runs = []

    def fake_run(argv, cwd, log, **kw):
        runs.append(argv)
        if argv[1] == "exec":
            (cwd / "engine.py").write_text("VALUE=2\n")
            (cwd / "tests/test_regression.py").write_text(
                "def test_fixed():\n import engine\n assert engine.VALUE == 2\n"
            )
        elif "pytest" in argv:
            # These assertions exercise the runner's actual source restore path.
            if "tests/test_regression.py" in argv:
                assert (cwd / "engine.py").read_text() == "VALUE=1\n"
                return 1
            assert (cwd / "engine.py").read_text() == "VALUE=2\n"
            assert "token" not in kw["env"] and kw["env"]["HOME"] != str(Path.home())
        return 0

    monkeypatch.setattr(W, "run_process", fake_run)
    monkeypatch.setattr(W, "check_prefix", lambda *a: [])
    root = tmp_path / "attempts"
    root.mkdir()
    W.process_attempt(
        Client(),
        {
            "attemptId": 1,
            "lease": "a" * 32,
            "bundle": {"caseId": 7, "category": "code", "service": "gmail"},
        },
        repo,
        root,
        {"codex": "/usr/bin/true"},
    )
    states = [body["state"] for path, body in calls if path.endswith("/report")]
    assert states == ["TESTING", "WAITING_QUALIFICATION"]
    assert calls[-1][1]["evidence"]["testsPassed"] is True
    assert calls[-1][1]["evidence"]["qualified"] is False
    assert W.git(repo, "rev-parse", "HEAD") == original
    assert (repo / "engine.py").read_text() == "VALUE=1\n"
    assert W.git(bare, "show-ref", "refs/heads/bitport/incident-7-1")
    # The agent prompt mentions deployment as forbidden; no deployment process ran.
    assert len(runs) == 4
    assert runs[0][1] == "exec"
    assert all("-m" in argv for argv in runs[1:])


def test_failed_model_never_tests_commits_pushes_or_deploys(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    states = []

    class Client:
        def call(self, path, body):
            states.append(body.get("state"))
            return {"ok": True}

    monkeypatch.setattr(W, "run_process", lambda *a, **kw: 1)
    W.process_attempt(
        Client(),
        {"attemptId": 1, "lease": "a" * 32, "bundle": {"caseId": 7}},
        repo,
        tmp_path / "work",
        {"codex": "/usr/bin/true"},
    )
    assert states == ["FAILED"]


def test_candidate_is_qualified_even_when_deployment_is_not_configured(
    tmp_path, monkeypatch
):
    import candidate_qualification as qualification
    from tests.test_worker_qualification import fixture_repo

    repo, _ = fixture_repo(tmp_path)
    reports = []

    class Client:
        def call(self, path, body):
            if path.endswith("/report"):
                reports.append(body.copy())
            return {"ok": True}

    def run(argv, cwd, log, **kwargs):
        if argv[1] == "exec":
            (cwd / "engine.py").write_text("def retry(status, reason): return True\n")
            (cwd / "tests/test_candidate.py").write_text(
                "def test_candidate(): assert True\n"
            )
        elif "pytest" in argv and "tests/test_candidate.py" in argv:
            return 1
        return 0

    monkeypatch.setattr(W, "run_process", run)
    monkeypatch.setattr(W, "check_prefix", lambda *args, **kwargs: [])
    monkeypatch.setattr(qualification, "supported", lambda *args: True)
    monkeypatch.setattr(
        qualification,
        "qualify",
        lambda *args: (
            True,
            "passed",
            {"qualificationScenario": "gmail-quota-live-v1", "artifactHash": "f" * 64},
        ),
    )
    W.process_attempt(
        Client(),
        {
            "attemptId": 1,
            "lease": "x" * 32,
            "bundle": {"caseId": 7, "service": "gmail"},
        },
        repo,
        tmp_path / "attempts",
        {"codex": "/usr/bin/true"},
    )
    assert [r["state"] for r in reports] == ["TESTING", "TESTING", "WAITING_DEPLOYMENT"]
    assert reports[-1]["evidence"]["qualified"] is True
    assert reports[-1]["evidence"]["deploymentConfigured"] is False
