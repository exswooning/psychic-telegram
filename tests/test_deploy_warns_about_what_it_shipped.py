"""A deploy must say when what it shipped cannot be reproduced.

Both of these happened silently in one session and cost real time:

  * DEPLOYED_COMMIT read 204168a-dirty. Code was running that matched no
    commit -- not reproducible, not revertable, and two commits behind the
    checkout it came from. The stamp recorded it; nothing said it.
  * Nine commits sat unpushed on a single VPS. The only copy of a day's
    work was one box, and the deploy that shipped them said nothing.

Warn, never block: a hotfix from a dirty tree is a legitimate thing to
want at 3am. The point is that the operator knows which one they just did.
"""
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "sync_vps.sh")


def _src():
    return open(SCRIPT, encoding="utf-8").read()


class TestItStaysValid:
    def test_the_script_parses(self):
        assert subprocess.run(["bash", "-n", SCRIPT]).returncode == 0


class TestTheDirtyWarning:
    def test_it_warns_rather_than_only_stamping(self):
        src = _src()
        assert "DIRTY" in src
        assert re.search(r"WARNING: deployed from a DIRTY tree", src)

    def test_it_goes_to_stderr(self):
        # stdout is piped through `tail` by every caller in this repo, which
        # is exactly how the -dirty stamp got missed.
        for line in _src().splitlines():
            if "WARNING: deployed from a DIRTY" in line:
                assert ">&2" in line, "the warning is invisible behind a tail"

    def test_it_names_the_files(self):
        assert 'echo "$DIRTY" | head' in _src()

    def test_it_does_not_abort(self):
        """A hotfix from a dirty tree must still deploy."""
        block = _src().split('if [[ -n "$DIRTY" ]]; then', 2)[-1][:600]
        assert "exit 1" not in block


class TestTheUnpushedWarning:
    def test_it_checks_the_upstream(self):
        src = _src()
        assert "@{upstream}" in src
        assert "rev-list --count" in src

    def test_it_warns_when_ahead(self):
        assert re.search(r"commit\(s\) not pushed", _src())

    def test_it_survives_a_branch_with_no_upstream(self):
        # A local-only branch is normal; the check must not fail the deploy.
        src = _src()
        assert 'if UPSTREAM="$(cd "$REPO" && git rev-parse' in src

    def test_it_goes_to_stderr_too(self):
        for line in _src().splitlines():
            if "not pushed to" in line:
                assert ">&2" in line


class TestTheStampStillHappens:
    def test_the_commit_is_still_written(self):
        assert "DEPLOYED_COMMIT" in _src()

    def test_dirty_still_marks_the_stamp(self):
        # The warning is additional to the marker, not a replacement: the
        # stamp is what a later reader on the box sees.
        assert 'COMMIT="$COMMIT-dirty"' in _src()


class TestTheVenvStaysInStepWithRequirements:
    """playwright was pip-installed by hand once, never declared in
    requirements.txt, and a box rebuilt from scratch after the dead man
    switch fired had a venv silently missing it -- the Setup Wizard's very
    first click failed with ModuleNotFoundError. sync_vps.sh only ever
    rsyncs code and restarts services; it never touched the venv, so fixing
    requirements.txt alone would not have fixed a box already running, and
    would not stop the NEXT undeclared dependency doing the same thing."""

    def test_it_installs_from_requirements_on_every_deploy(self):
        src = _src()
        assert "pip install -q -r requirements.txt" in src

    def test_it_runs_before_the_syntax_check_and_restart(self):
        src = _src()
        assert src.index("pip install -q -r requirements.txt") \
            < src.index("compileall")

    def test_a_failed_install_aborts_the_deploy(self):
        # Unlike the dirty-tree warning, this one must block: restarting
        # services against a venv that just failed to update silently ships
        # a broken dependency.
        block = src_between = _src().split(
            "pip install -q -r requirements.txt", 1)[-1][:500]
        assert "exit 1" in block

    def test_it_survives_a_repo_with_no_requirements_file(self):
        assert 'if [[ -f "$(dirname "$0")/requirements.txt" ]]; then' in _src()


class TestItWarnsBeforeKillingAnInProgressSetup:
    """api_server.py's own _reconcile_full_setup_state() already documents
    the mechanism: restarting bitport-api kills a full_setup.py run mid-
    flight, KillMode=process does not save it, and the only prior trace was
    a stale progress file someone had to notice and diagnose from scratch.
    Confirmed live, more than once, in one session: a deploy shipped while
    a 15+ minute gcloud/DWD run was in progress and killed it silently."""

    def test_it_checks_for_a_live_run_before_restarting(self):
        src = _src()
        assert "full_setup.py" in src
        assert src.index("RUNNING_SETUP=") < src.index("systemctl restart bitport-webui bitport-api")

    def test_it_warns_rather_than_silently_restarting(self):
        assert re.search(r"WARNING: a full_setup\.py run is IN PROGRESS", _src())

    def test_it_goes_to_stderr(self):
        for line in _src().splitlines():
            if "IN PROGRESS on the target" in line:
                assert ">&2" in line

    def test_it_does_not_block_the_restart(self):
        """Same reasoning as the dirty-tree warning: a fix that cannot wait
        is a legitimate thing to ship anyway. This makes it a choice, not
        an accident -- not a gate."""
        block = _src().split("RUNNING_SETUP=", 1)[-1][:900]
        assert "exit 1" not in block
        assert "systemctl restart bitport-webui bitport-api" in block
