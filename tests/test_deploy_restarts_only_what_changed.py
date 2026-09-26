"""A cosmetic frontend change must not touch a running job.

sync_vps.sh restarted bitport-webui on EVERY deploy, and a restart of that unit
kills any seed or reset its Job launcher started. So a colour change to a page
cost a twelve-hour seed exactly as a backend fix would. The script now asks
what the deploy would change on the box and restarts only if some of it can
alter what a running process does.

These run the real script against a stub ssh and rsync, and check what it tried
to do to the box -- not what its text says.
"""
import os
import stat
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "sync_vps.sh")


def classify(paths: list[str]) -> tuple[int, str]:
    r = subprocess.run(["bash", SCRIPT, "--classify"], input="\n".join(paths) + "\n",
                       capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


class TestWhatCountsAsRuntime:
    @pytest.mark.parametrize("paths", [
        ["migration-webui/src/pages/Jobs.tsx"],
        ["migration-webui/dist/index.html", "migration-webui/dist/assets/index-abc.js"],
        ["tests/test_x.py", "data-generator/test_seed_sandbox.py", "CLAUDE.md", "AGENT_COORDINATION.md"],
        ["migration-webui/src/a.tsx", "tests/t.py", "README.md"],
    ])
    def test_the_frontend_tests_and_docs_cannot_change_what_runs(self, paths):
        assert classify(paths) == (0, "frontend-only")

    @pytest.mark.parametrize("path", [
        "webui.py", "api_server.py", "seed_sandbox.py", "data-generator/seed_sandbox.py", "requirements.txt",
        "systemd/bitport-webui.service", "config.py", "sync_vps.sh", "migrations/010_run_events_incidents.sql",
    ])
    def test_anything_a_process_imports_or_a_unit_runs_does(self, path):
        rc, out = classify(["migration-webui/src/a.tsx", path])
        assert rc == 1 and out == path            # and it names the file that forced it

    def test_a_directory_entry_alone_is_not_a_change(self):
        assert classify(["migration-webui/", "migration-webui/src/"])[0] == 0

    def test_nothing_at_all_changed_is_frontend_only(self):
        assert classify([])[0] == 0


@pytest.fixture
def box(tmp_path):
    """A stub ssh and rsync in front of PATH. rsync answers a dry run with the
    files the test says would change; everything is logged."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log, would = tmp_path / "log", tmp_path / "would"
    log.write_text("")
    would.write_text("")
    (bindir / "rsync").write_text(
        '#!/usr/bin/env bash\n'
        'if [[ " $* " == *" -azcn "* ]]; then echo "dry: $*" >> "$FAKE_LOG"; cat "$FAKE_WOULD"; exit "${FAKE_DRY_RC:-0}"; fi\n'
        'echo "rsync: $*" >> "$FAKE_LOG"; exit 0\n')
    (bindir / "ssh").write_text('#!/usr/bin/env bash\necho "ssh: $*" >> "$FAKE_LOG"; exit 0\n')
    for f in bindir.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IEXEC)

    def deploy(changed: list[str], **env):
        would.write_text("\n".join(changed) + ("\n" if changed else ""))
        log.write_text("")
        e = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_LOG": str(log),
             "FAKE_WOULD": str(would), **{k: str(v) for k, v in env.items()}}
        r = subprocess.run(["bash", SCRIPT, "root@box", "/srv/app"], capture_output=True, text=True, env=e, cwd=ROOT)
        return r, log.read_text()
    return deploy


def restarts(log: str) -> bool:
    return "systemctl restart" in log


class TestTheDeployItself:
    def test_a_frontend_only_deploy_restarts_nothing_and_says_so(self, box):
        r, log = box(["migration-webui/dist/index.html", "migration-webui/src/pages/Jobs.tsx"])
        assert r.returncode == 0
        assert not restarts(log)
        assert "FRONTEND-ONLY" in r.stdout and "no job was touched" in r.stdout

    def test_it_also_skips_everything_else_that_could_disturb_a_job(self, box):
        """Dependency installs, unit files, permissions and the compile check
        all belong to a deploy that changes something that runs."""
        r, log = box(["migration-webui/src/pages/Jobs.tsx"])
        for touched in ("pip install", "compileall", "daemon-reload", "harden.sh", "systemctl"):
            assert touched not in log, touched

    def test_it_still_records_what_is_deployed(self, box):
        r, log = box(["migration-webui/src/pages/Jobs.tsx"])
        assert "DEPLOYED_COMMIT" in log

    def test_a_backend_change_restarts_and_names_the_files_that_made_it_so(self, box):
        r, log = box(["migration-webui/src/pages/Jobs.tsx", "api_server.py", "webui.py"])
        assert restarts(log) and "systemctl restart bitport-webui" in log
        assert "api_server.py" in r.stdout and "webui.py" in r.stdout
        assert "Jobs.tsx" not in r.stdout.split("restarting, because")[1]

    def test_a_dry_run_that_fails_is_treated_as_a_backend_deploy(self, box):
        """Not knowing what changed is not permission to skip the restart."""
        r, log = box([], FAKE_DRY_RC=23)
        assert restarts(log)

    def test_force_restart_overrides(self, box):
        r, log = box(["migration-webui/src/pages/Jobs.tsx"], FORCE_RESTART=1)
        assert restarts(log)


class TestWhatIsShipped:
    def test_the_dry_run_and_the_real_transfer_use_the_same_exclusions(self, box):
        """What is classified must be exactly what is shipped."""
        r, log = box(["api_server.py"])
        dry = next(l for l in log.splitlines() if l.startswith("dry:"))
        real = next(l for l in log.splitlines() if l.startswith("rsync:") and "--partial" in l)
        pick = lambda l: sorted(t for t in l.split() if t.startswith("--exclude") or t in ("logs/", "node_modules/", "keys/"))  # noqa: E731
        assert pick(dry) == pick(real) and "--exclude logs/" in dry

    def test_the_dry_run_compares_content_not_modification_times(self, box):
        """A checkout or merge rewrites mtimes without changing a byte; going by
        those would call every deploy a backend one."""
        r, log = box([])
        assert " -azcn " in next(l for l in log.splitlines() if l.startswith("dry:"))

    def test_the_boxs_own_logs_and_this_machines_node_modules_are_not_shipped(self, box):
        r, log = box(["api_server.py"])
        real = next(l for l in log.splitlines() if l.startswith("rsync:") and "--partial" in l)
        assert "--exclude logs/" in real and "--exclude node_modules/" in real

    def test_secrets_and_machine_state_are_still_excluded(self, box):
        r, log = box(["api_server.py"])
        real = next(l for l in log.splitlines() if l.startswith("rsync:") and "--partial" in l)
        for name in ("keys/", "oauth/", "env.sh", "run_state.json", "migration.db*", "unprotected_domains.json"):
            assert f"--exclude {name}" in real, name
