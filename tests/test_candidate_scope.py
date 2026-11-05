"""The server independently verifies Git object and allowed-change evidence."""

import base64
import hashlib
import subprocess

import pytest

import candidate_scope as scope
import incident_worker as worker
from tests.test_candidate_qualification import candidate_repo


def test_exact_candidate_proof_and_tampering(tmp_path):
    _, _, tree, _, base, commit = candidate_repo(tmp_path)
    proof = scope.build_proof(tree, commit, base)
    wanted = hashlib.sha256((tree / "verify_gmail_quota.py").read_bytes()).hexdigest()
    assert scope.validate_proof(proof, commit, base) == wanted
    with pytest.raises(ValueError):
        scope.validate_proof(proof, "f" * 40, base)
    with pytest.raises(ValueError):
        scope.validate_proof(proof, commit, "f" * 40)
    for name in proof:
        tampered = dict(proof)
        tampered[name] = base64.b64encode(base64.b64decode(proof[name]) + b"X").decode()
        with pytest.raises(ValueError):
            scope.validate_proof(tampered, commit, base)


def test_proof_rejects_extra_runtime_changes_and_modified_existing_tests(tmp_path):
    _, _, tree, _, base, _ = candidate_repo(tmp_path)
    (tree / "verify_sample.py").write_text("not allowed")
    subprocess.run(["git", "add", "verify_sample.py"], cwd=tree, check=True)
    subprocess.run(
        ["git", "commit", "--amend", "-qm", "extra runtime"], cwd=tree, check=True
    )
    commit = worker.git(tree, "rev-parse", "HEAD")
    with pytest.raises(ValueError):
        scope.validate_proof(scope.build_proof(tree, commit, base), commit, base)
    subprocess.run(
        ["git", "checkout", base, "--", "verify_sample.py"], cwd=tree, check=True
    )
    (tree / "tests/test_verify_gmail_quota.py").write_text("not allowed")
    subprocess.run(
        ["git", "add", "verify_sample.py", "tests/test_verify_gmail_quota.py"],
        cwd=tree,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "--amend", "-qm", "modified tests"], cwd=tree, check=True
    )
    commit = worker.git(tree, "rev-parse", "HEAD")
    with pytest.raises(ValueError):
        scope.validate_proof(scope.build_proof(tree, commit, base), commit, base)


def test_proof_rejects_classifier_with_side_effects(tmp_path):
    _, _, tree, _, base, _ = candidate_repo(tmp_path)
    path = tree / "verify_gmail_quota.py"
    path.write_text(
        path.read_text().replace(
            "    status = getattr(",
            "    __import__('os').system('false')\n    status = getattr(",
            1,
        )
    )
    subprocess.run(["git", "add", "verify_gmail_quota.py"], cwd=tree, check=True)
    subprocess.run(
        ["git", "commit", "--amend", "-qm", "unsafe classifier"], cwd=tree, check=True
    )
    commit = worker.git(tree, "rev-parse", "HEAD")
    with pytest.raises(ValueError):
        scope.validate_proof(scope.build_proof(tree, commit, base), commit, base)
