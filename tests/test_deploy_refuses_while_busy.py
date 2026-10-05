"""A deploy that would restart services refuses while anything is running.

Live, a printed `ps` warning let a deploy go out over a migration, and later one
cut off the repair that runs as a thread inside bitport-api, which `ps` cannot
see. The check runs before a byte is copied; these run the real script with an
ssh stub that executes the busy check locally against a fake install.
"""
import os
import sqlite3
import stat
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "sync_vps.sh")


@pytest.fixture
def box(tmp_path):
    bindir, dest, log = tmp_path / "bin", tmp_path / "app", tmp_path / "log"
    bindir.mkdir()
    (dest / "data" / "accounts" / "9").mkdir(parents=True)
    (dest / ".venv" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, dest / ".venv" / "bin" / "python")
    log.write_text("")
    (bindir / "rsync").write_text(
        '#!/usr/bin/env bash\n'
        'if [[ " $* " == *" -azcn "* ]]; then printf "%s\\n" $FAKE_WOULD; exit 0; fi\n'
        'echo "rsync: $*" >> "$FAKE_LOG"; exit 0\n')
    (bindir / "ssh").write_text(
        '#!/usr/bin/env bash\nlast="${@: -1}"\n'
        'if [[ "$last" == *repair_runs* ]]; then bash -c "$last"; exit 0; fi\n'
        'echo "ssh: $*" >> "$FAKE_LOG"; exit 0\n')
    for f in bindir.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    ledger = dest / "data" / "accounts" / "9" / "migration.db"
    c = sqlite3.connect(ledger)
    c.execute("CREATE TABLE repair_runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, summary TEXT, error TEXT)")
    c.commit(); c.close()

    def deploy(would="api_server.py", **env):
        log.write_text("")
        e = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_LOG": str(log),
             "FAKE_WOULD": would, **{k: str(v) for k, v in env.items()}}
        r = subprocess.run(["bash", SCRIPT, "root@box", str(dest)], capture_output=True,
                           text=True, env=e, cwd=ROOT)
        return r, log.read_text()

    def repair(started: str, finished: str | None = None):
        c = sqlite3.connect(ledger)
        c.execute("INSERT INTO repair_runs (started_at, finished_at) VALUES (?, ?)", (started, finished))
        c.commit(); c.close()

    deploy.repair, deploy.tmp = repair, tmp_path
    return deploy


def _now(offset_h=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_h * 3600))


def test_an_unfinished_repair_refuses_before_anything_is_copied(box):
    box.repair(_now())
    r, log = box()
    assert r.returncode == 3 and "REFUSING" in r.stderr and "repair" in r.stderr
    assert "rsync:" not in log and "systemctl" not in log


def test_a_finished_repair_does_not(box):
    box.repair(_now(-1), _now())
    r, log = box()
    assert r.returncode == 0 and "systemctl restart" in log


def test_a_stale_unfinished_repair_from_a_dead_process_does_not(box):
    box.repair(_now(-48))
    r, log = box()
    assert r.returncode == 0


def test_a_running_job_refuses(box):
    job = box.tmp / "tally.py"
    job.write_text("import time; time.sleep(30)\n")
    p = subprocess.Popen([sys.executable, str(job)])
    try:
        time.sleep(0.5)
        r, log = box()
        assert r.returncode == 3 and "tally.py" in r.stderr
    finally:
        p.kill()


def test_the_override_deploys_anyway(box):
    box.repair(_now())
    r, log = box(DEPLOY_OVER_JOBS=1)
    assert r.returncode == 0 and "systemctl restart" in log


def test_a_frontend_only_deploy_is_never_refused(box):
    box.repair(_now())
    r, log = box(would="migration-webui/src/pages/Jobs.tsx")
    assert r.returncode == 0 and "FRONTEND-ONLY" in r.stdout


def _mirror_cycle(box, seconds):
    """A stand-in `main.py ... mirror` process, as the busy check sees one."""
    fake = box.tmp / "main.py"
    fake.write_text(f"import time\ntime.sleep({seconds})\n")
    return subprocess.Popen([sys.executable, str(fake), "--account-id", "9", "mirror"])


def test_a_mirror_cycle_alone_is_waited_out_not_refused(box):
    p = _mirror_cycle(box, 4)
    try:
        r, log = box(MIRROR_WAIT_SEC=60)
    finally:
        p.kill()
    assert r.returncode == 0, r.stderr
    assert "waiting for it to finish" in r.stdout and "systemctl restart" in log


def test_a_mirror_cycle_that_outlasts_the_wait_still_refuses(box):
    p = _mirror_cycle(box, 60)
    try:
        r, log = box(MIRROR_WAIT_SEC=0)
    finally:
        p.kill()
    assert r.returncode == 3 and "mirror" in r.stderr and "rsync:" not in log
