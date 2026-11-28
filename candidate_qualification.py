"""Qualify a narrowly scoped candidate against trusted contracts and a live canary.

This module runs on the coding laptop. Candidate code runs without network or
credentials; a trusted relay requests only fixed, read-only sandbox statuses.
The initial scenario covers only changes to verify_gmail_quota.py plus new tests.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time

import incident_worker as worker
import candidate_scope

TRUSTED_BASE = "61ed91fdecec719d9652cb3eb5c1e26309bb063b"
CONTRACT_PATH = "tests/test_verify_gmail_quota.py"
CONTRACT_SHA256 = "57dac7831ddd3155bbc8943ac8185679f591ec989e1837e1cb071584c975076f"
HARNESS_SHA256 = "e482bd2e7756abace2905927a29b52178b2d39a5a800de9a64ca326172a35120"
SCENARIO = "gmail-quota-live-v1"


def supported(tree, base, candidate, case):
    """Fail closed for changes outside the independently tested scenario."""
    if case.get("service") != "gmail" or not re.fullmatch(r"[0-9a-f]{40}", candidate):
        return False
    if worker.git(tree, "rev-parse", "HEAD") != candidate:
        return False
    parents = worker.git(tree, "rev-list", "--parents", "-n", "1", candidate).split()
    if parents != [candidate, base] or worker.git(
        tree, "status", "--porcelain", "--untracked-files=all"
    ):
        return False
    changes = worker.git(
        tree, "diff", "--no-renames", "--name-status", base, candidate
    ).splitlines()
    quota = False
    tests = 0
    for line in changes:
        parts = line.split("\t")
        if len(parts) != 2:
            return False
        status, name = parts
        if name == "verify_gmail_quota.py" and status == "M":
            quota = True
        elif status == "A" and re.fullmatch(r"tests/test_[A-Za-z0-9_]+\.py", name):
            tests += 1
        else:
            return False
    if not quota or not tests:
        return False
    try:
        original = subprocess.check_output(
            ["git", "-C", str(tree), "show", base + ":verify_gmail_quota.py"]
        )
        return candidate_scope.classification_only(
            original, (tree / "verify_gmail_quota.py").read_bytes()
        )
    except (OSError, subprocess.CalledProcessError):
        return False


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _clean(tree, candidate):
    return worker.git(tree, "rev-parse", "HEAD") == candidate and not worker.git(
        tree, "status", "--porcelain", "--untracked-files=all"
    )


def _live_canary(
    client, attempt, lease, commit, tree, repo, private, home, stop, harness, proof
):
    prefix = worker.check_prefix(tree, repo, home, readonly_files=[harness])
    env = worker.clean_env(home)
    env.update(
        TMPDIR=str(home),
        PYTHONPATH=str(tree),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        VERIFY_GMAIL_UNITS_PER_MINUTE="6000",
        VERIFY_GMAIL_BATCH_SIZE="1",
    )
    command = prefix + [str((repo / ".venv/bin/python").absolute()), str(harness)]
    seen = set()
    buffer = b""
    started = time.monotonic()
    with open(private / "sandbox-canary.log", "wb") as log:
        proc = subprocess.Popen(
            command,
            cwd=tree,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            env=env,
            start_new_session=True,
        )
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        try:
            done = False
            while not done:
                if stop.is_set() or time.monotonic() - started > 180:
                    raise RuntimeError("Canary yielded or exceeded its time budget")
                if not selector.select(timeout=0.5):
                    if proc.poll() is not None:
                        raise RuntimeError("Isolated canary exited before completing")
                    continue
                chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    raise RuntimeError("Isolated canary closed its status relay")
                buffer += chunk
                if len(buffer) > 8192:
                    raise RuntimeError("Isolated canary output exceeded its limit")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    request = json.loads(line)
                    if request == {"done": True}:
                        done = True
                        break
                    if (
                        not isinstance(request, dict)
                        or set(request) != {"side", "slot"}
                        or request["side"] not in {"source", "target"}
                        or type(request["slot"]) is not int
                        or request["slot"] not in {0, 1}
                        or (request["side"], request["slot"]) in seen
                    ):
                        raise RuntimeError(
                            "Isolated canary requested an invalid sandbox slot"
                        )
                    if stop.is_set():
                        raise RuntimeError("Canary yielded to priority work")
                    reply = client.call(
                        f"{attempt}/canary",
                        {
                            "lease": lease,
                            "candidate_commit": commit,
                            "proof": proof,
                            "side": request["side"],
                            "slot": request["slot"],
                        },
                    )
                    if reply != {"status": 200}:
                        raise RuntimeError("Sandbox returned an unsuccessful status")
                    seen.add((request["side"], request["slot"]))
                    proc.stdin.write(b'{"status":200}\n')
                    proc.stdin.flush()
            if seen != {
                (side, slot) for side in ("source", "target") for slot in (0, 1)
            }:
                raise RuntimeError(
                    "Both sandbox mailboxes and both slots must be exercised"
                )
            if proc.wait(timeout=10) != 0:
                raise RuntimeError("Isolated canary failed")
        finally:
            selector.close()
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()


def qualify(
    client, attempt, lease, commit, tree, repo, private, home, stop, base, case
):
    """Run pinned independent contracts then actual sandbox read-only requests."""
    try:
        if not supported(tree, base, commit, case):
            raise RuntimeError("Candidate no longer matches the trusted scenario")
        candidate_hash = _digest(tree / "verify_gmail_quota.py")
        contract = subprocess.check_output(
            ["git", "-C", str(repo), "show", TRUSTED_BASE + ":" + CONTRACT_PATH]
        )
        if hashlib.sha256(contract).hexdigest() != CONTRACT_SHA256:
            raise RuntimeError("Trusted Gmail contract hash does not match")
        contract_dir = home / "trusted-contract"
        contract_dir.mkdir(mode=0o700, exist_ok=True)
        if contract_dir.is_symlink() or not contract_dir.resolve().is_relative_to(
            home.resolve()
        ):
            raise RuntimeError("Unsafe trusted contract directory")
        contract_file = contract_dir / "test_trusted_gmail_quota_contract.py"
        fd = os.open(
            contract_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "wb") as out:
            out.write(contract)
        harness = Path(__file__).with_name("candidate_canary_harness.py").resolve()
        if _digest(harness) != HARNESS_SHA256:
            raise RuntimeError("Trusted sandbox harness hash does not match")
        prefix = worker.check_prefix(tree, repo, home)
        env = worker.clean_env(home)
        env.update(
            TMPDIR=str(home),
            PYTHONPATH=str(tree),
            PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
            MIGRATION_DB=str(home / "qualification.db"),
        )
        rc = worker.run_process(
            prefix
            + [
                str((repo / ".venv/bin/python").absolute()),
                "-m",
                "pytest",
                "-q",
                "-o",
                "addopts=",
                "--confcutdir",
                str(contract_dir),
                str(contract_file),
                "-k",
                "not separate_processes",
            ],
            tree,
            private / "qualification-contract.log",
            timeout=180,
            stop=stop,
            env=env,
        )
        if rc or _digest(contract_file) != CONTRACT_SHA256 or not _clean(tree, commit):
            raise RuntimeError("Trusted Gmail contract failed or the candidate changed")
        _live_canary(
            client,
            attempt,
            lease,
            commit,
            tree,
            repo,
            private,
            home,
            stop,
            harness,
            candidate_scope.build_proof(tree, commit, base),
        )
        if (
            _digest(harness) != HARNESS_SHA256
            or _digest(tree / "verify_gmail_quota.py") != candidate_hash
            or not _clean(tree, commit)
        ):
            raise RuntimeError("Candidate or trusted harness changed during the canary")
        return (
            True,
            "Candidate-specific sandbox canary passed.",
            {"qualificationScenario": SCENARIO, "artifactHash": candidate_hash},
        )
    except Exception as exc:
        return False, "Candidate qualification held: " + str(exc)[:250], {}
