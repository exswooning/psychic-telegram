"""A deploy ships only the tree ./gate.sh passed (tests/gate.txt): the 13 Nov lint failure reached HEAD because
the full suite was too slow to run before a deploy, and a stale frontend shipped because nothing built it."""
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _repo(tmp_path):
    for name in ("sync_vps.sh", "gate.sh"):
        shutil.copy(ROOT / name, tmp_path / name)
    run = lambda *a: subprocess.run(a, cwd=tmp_path, capture_output=True, text=True)
    run("git", "init", "-q"); run("git", "add", "."); run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
    return run


def test_the_tree_id_changes_with_any_edit(tmp_path):
    run = _repo(tmp_path)
    first = run("bash", "gate.sh", "--tree-id").stdout
    (tmp_path / "new.py").write_text("x = 1\n")
    assert run("bash", "gate.sh", "--tree-id").stdout != first


def test_a_tree_the_gate_did_not_pass_is_refused_before_anything_is_copied(tmp_path):
    run = _repo(tmp_path)
    (tmp_path / ".gate-passed").write_text("not this tree\n")
    r = run("bash", "sync_vps.sh", "nobody@invalid.invalid", "/nowhere")
    assert r.returncode == 3 and "has not passed ./gate.sh" in r.stderr
