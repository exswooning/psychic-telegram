"""Operator run state must not travel with the code.

run_state.json is written by webui when someone changes a toggle. It was
tracked in git AND rsynced by sync_vps.sh, so every deploy overwrote the
box's live settings with whatever sat in the developer's working tree.

That is how dry_run came back on after I had turned it off: the deploy put
a stale copy back. And dry_run:true silently turns every action into a
no-op that still reports a clean run -- so a deploy could disable the
product without anyone touching a switch.
"""
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = "run_state.json"


class TestRunStateStaysOnTheMachineThatWroteIt:
    def test_git_does_not_track_it(self):
        out = subprocess.run(["git", "ls-files", STATE], cwd=ROOT,
                             capture_output=True, text=True).stdout.strip()
        assert out == "", f"{STATE} is tracked; a deploy ships one machine's toggles"

    def test_git_ignores_it(self):
        rc = subprocess.run(["git", "check-ignore", "-q", STATE],
                            cwd=ROOT).returncode
        assert rc == 0, f"{STATE} is not ignored, so it will be committed again"

    def test_the_deploy_does_not_rsync_it(self):
        with open(os.path.join(ROOT, "sync_vps.sh"), encoding="utf-8") as fh:
            assert f"--exclude '{STATE}'" in fh.read(), (
                "sync_vps.sh would copy it over the box's own settings")

    def test_it_is_excluded_beside_the_other_machine_local_state(self):
        """Same class as keys/, env.sh and the ledger: state that belongs to
        one machine and must never be shipped by a code deploy."""
        with open(os.path.join(ROOT, "sync_vps.sh"), encoding="utf-8") as fh:
            src = fh.read()
        for neighbour in ("keys/", "env.sh", "migration.db*"):
            assert f"--exclude '{neighbour}'" in src, neighbour
