"""22 Nov 2026: a dry run of the deploy listed 9,197 files under .claude/worktrees -- whole checkouts an agent
works in -- to be copied into the box's live directory."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_deploy_does_not_ship_agent_worktrees():
    with open(os.path.join(ROOT, "sync_vps.sh"), encoding="utf-8") as fh:
        assert "--exclude '.claude/'" in fh.read()
