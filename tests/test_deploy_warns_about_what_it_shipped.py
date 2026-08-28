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
