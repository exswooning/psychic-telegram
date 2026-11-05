"""Exercise real Git, pytest subprocesses and OS boundaries, never tenant APIs.

Opt in on macOS with BITPORT_OS_QUALIFICATION=1. The coding CLI alone is a
controlled fixture; regression/tests/qualification/deployment are real child
processes, with deployment confined to a temporary fake installation.
"""

import os
from pathlib import Path
import subprocess
import sys

import pytest

import incident_worker as W


def fixture_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in [
        ("user.name", "Qualification"),
        ("user.email", "test@example.invalid"),
    ]:
        subprocess.run(["git", "config", key, value], cwd=repo, check=True)
    (repo / "engine.py").write_text(
        "def retry(status, reason):\n    return status == 429\n"
    )
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_existing.py").write_text(
        "from engine import retry\n"
        "def test_old_contract():\n"
        "    assert retry(429, '')\n"
        "    assert not retry(403, 'permissionDenied')\n"
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture baseline"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(origin)], cwd=repo, check=True
    )
    (repo / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    return repo, origin


def test_isolation_profile_is_outside_both_writable_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(W.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(W.shutil, "which", lambda _: "/usr/bin/sandbox-exec")
    tree = tmp_path / "source"
    home = tmp_path / "checks-home"
    tree.mkdir()
    home.mkdir()
    prefix = W.check_prefix(tree, tmp_path, home)
    profile = Path(prefix[-1]).resolve()
    assert not profile.is_relative_to(home) and not profile.is_relative_to(tree)


def test_tests_cannot_change_a_candidate_after_it_was_validated(tmp_path, monkeypatch):
    repo, origin = fixture_repo(tmp_path)
    states = []

    class Client:
        def call(self, path, body):
            if path.endswith("/report"):
                states.append(body.copy())
            return {"ok": True}

    def process(argv, cwd, log, **kwargs):
        if argv[1] == "exec":
            (cwd / "engine.py").write_text("def retry(status, reason): return True\n")
            (cwd / "tests/test_new.py").write_text("def test_new(): assert True\n")
        elif "pytest" in argv:
            if "tests/test_new.py" in argv:
                return 1
            (cwd / "engine.py").write_text(
                "def retry(status, reason): return 'changed after validation'\n"
            )
        return 0

    monkeypatch.setattr(W, "run_process", process)
    monkeypatch.setattr(W, "check_prefix", lambda *args: [])
    W.process_attempt(
        Client(),
        {"attemptId": 1, "lease": "a" * 32, "bundle": {"caseId": 7}},
        repo,
        tmp_path / "attempts",
        {"codex": "/usr/bin/true"},
    )
    assert [r["state"] for r in states] == ["TESTING", "FAILED"]
    assert "Tests changed the candidate" in states[-1]["detail"]
    assert (
        subprocess.run(["git", "show-ref"], cwd=origin, capture_output=True).returncode
        == 1
    )


@pytest.mark.skipif(
    os.environ.get("BITPORT_OS_QUALIFICATION") != "1" or sys.platform != "darwin",
    reason="Explicit macOS OS-isolation qualification only",
)
@pytest.mark.parametrize(
    "mode", ["pass", "qualification-refused", "deployment-refused"]
)
def test_real_isolated_regression_and_release_hook_flow(tmp_path, mode):
    repo, origin = fixture_repo(tmp_path)
    base = W.git(repo, "rev-parse", "HEAD")
    installation = tmp_path / "fake-installation"
    installation.mkdir()
    cli = tmp_path / "controlled-coding-cli"
    regression = (
        "from engine import retry\n"
        "def test_quota_reason():\n"
        "    assert retry(403, 'rateLimitExceeded')\n"
        "    assert not retry(403, 'permissionDenied')\n"
    )
    cli.write_text(
        "#!" + sys.executable + "\nimport sys\nfrom pathlib import Path\n"
        "if sys.argv[1] == 'login': raise SystemExit(0)\n"
        "root=Path(sys.argv[sys.argv.index('-C')+1])\n"
        "(root/'engine.py').write_text(\"def retry(status, reason):\\n    return status == 429 or reason == 'rateLimitExceeded'\\n\")\n"
        "(root/'tests/test_quota_regression.py').write_text(" + repr(regression) + ")\n"
    )
    cli.chmod(0o700)
    hook = tmp_path / "trusted-hook.py"
    hook.write_text(
        "import sys,hashlib,json\nfrom pathlib import Path\n"
        "phase,tree,commit,install,mode=sys.argv[1:]\n"
        "tree=Path(tree); install=Path(install)\n"
        "if mode == phase+'-refused': raise SystemExit(23)\n"
        "if phase == 'qualification':\n"
        "    assert 'rateLimitExceeded' in (tree/'engine.py').read_text()\n"
        "    (install/'qualification.json').write_text(json.dumps({'commit':commit,'sha256':hashlib.sha256((tree/'engine.py').read_bytes()).hexdigest()}))\n"
        "else:\n"
        "    q=json.loads((install/'qualification.json').read_text()); assert q['commit']==commit\n"
        "    assert q['sha256']==hashlib.sha256((tree/'engine.py').read_bytes()).hexdigest()\n"
        "    (install/'engine.py').write_bytes((tree/'engine.py').read_bytes())\n"
        "    (install/'DEPLOYED_COMMIT').write_text(commit)\n"
    )
    reports = []

    class Client:
        def call(self, path, body):
            if path.endswith("/heartbeat"):
                return {"enabled": True, "yield": False}
            if path.endswith("/report"):
                reports.append(body.copy())
                if body["state"] == "DEPLOYED":
                    assert (installation / "DEPLOYED_COMMIT").read_text() == body[
                        "candidate_commit"
                    ]
            return {"ok": True}

    config = {"codex": str(cli)}
    for key, phase in [
        ("qualifyCommand", "qualification"),
        ("deployCommand", "deployment"),
    ]:
        config[key] = [
            sys.executable,
            str(hook),
            phase,
            "{worktree}",
            "{commit}",
            str(installation),
            mode,
        ]
    W.process_attempt(
        Client(),
        {"attemptId": 1, "lease": "a" * 32, "bundle": {"caseId": 7}},
        repo,
        tmp_path / "attempts",
        config,
    )
    states = [r["state"] for r in reports]
    if mode == "pass":
        assert states == ["TESTING", "WAITING_DEPLOYMENT", "DEPLOYED", "RECHECKING"], (
            reports
        )
        assert (
            reports[-1]["evidence"]["testsPassed"]
            and reports[-1]["evidence"]["qualified"]
        )
    elif mode == "qualification-refused":
        assert states == ["TESTING", "WAITING_QUALIFICATION"], reports
        assert not (installation / "DEPLOYED_COMMIT").exists()
    else:
        assert states == ["TESTING", "WAITING_DEPLOYMENT", "FAILED"], reports
        assert not (installation / "DEPLOYED_COMMIT").exists()
    attempt = tmp_path / "attempts/attempt-1"
    assert "1 failed" in (attempt / "regression-before.log").read_text()
    assert "2 passed" in (attempt / "tests.log").read_text()
    assert W.git(repo, "rev-parse", "HEAD") == base
    assert W.git(origin, "show-ref", "refs/heads/bitport/incident-7-1")


@pytest.mark.skipif(
    os.environ.get("BITPORT_OS_QUALIFICATION") != "1" or sys.platform != "darwin",
    reason="Explicit macOS OS-isolation qualification only",
)
def test_real_tests_cannot_write_profile_read_host_secret_or_use_network(tmp_path):
    repo, _ = fixture_repo(tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    tree = private / "source"
    tree.mkdir()
    home = private / "checks-home"
    home.mkdir()
    outside = tmp_path / "host-secret.txt"
    outside.write_text("controlled test secret")
    prefix = W.check_prefix(tree, repo, home)
    profile = Path(prefix[-1])
    before = profile.read_bytes()
    script = (
        "from pathlib import Path\nimport socket\n"
        "for path in " + repr([str(profile), str(outside)]) + ":\n"
        "    try:\n"
        "        if path.endswith('.sb'): Path(path).write_text('(version 1) (allow default)')\n"
        "        else: Path(path).read_bytes()\n"
        "    except PermissionError: continue\n"
        "    raise AssertionError('isolation permitted forbidden file access')\n"
        "try: socket.socket().connect(('127.0.0.1',9))\n"
        "except PermissionError: pass\n"
        "else: raise AssertionError('network access permitted')\n"
        "print('ISOLATION_PASS')\n"
    )
    result = subprocess.run(
        prefix + [sys.executable, "-c", script],
        cwd=tree,
        env=W.clean_env(home),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "ISOLATION_PASS" in result.stdout
    assert profile.read_bytes() == before
