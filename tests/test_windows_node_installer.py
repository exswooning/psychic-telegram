"""Joining a Windows machine must not need a single click.

Three things asked for one, and all three were hit live on a real laptop:

  * git. `winget install Git.Git` is machine-scope and raises a UAC prompt.
    Dismissing it printed "You cancelled the installation. Installer failed
    with exit code: 2", and the run then died four commands later on
    "git : The term 'git' is not recognized". Python's own installer is
    user-scope and needed no prompt, which is the asymmetry that made this
    look like bad luck rather than a design problem.
  * A new shell. winget writes PATH to the registry; the running process
    keeps its stale copy, so a just-installed python is invisible.
  * The execution policy. Windows PowerShell defaults to Restricted, so
    running the .ps1 as a file is refused outright.

The script now fetches a branch zip over HTTPS (no git at all), reloads
PATH in-process, and is documented as `irm ... | iex`, which is not a file
and so is not subject to the execution policy.

Parsed with the real PowerShell 7.6.6 parser during development (605
tokens, zero errors) and run end-to-end on a non-Windows box as far as the
Windows-only paths allow: steps 1 and 2 complete, including a live
download+extract of the branch archive and a re-run that left an existing
node.env and keys/ standing.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "install_node.ps1"), encoding="utf-8") as fh:
    PS1 = fh.read()

# The same source with comments removed. The comments explain at length WHY
# git and Mandatory parameters are gone, so an "is it absent" assertion read
# against the raw text finds the explanation and fails -- which is a test
# bug that would push someone to delete the explanation.
CODE = re.sub(r"<#.*?#>", "", PS1, flags=re.S)
CODE = re.sub(r"#.*$", "", CODE, flags=re.M)


class TestNothingStopsToAsk:
    def test_no_parameter_is_mandatory(self):
        """A Mandatory parameter under `iex` stops and prompts for a value,
        which is precisely the interruption this script exists to avoid --
        and there is no sensible way to answer it in a piped one-liner."""
        assert "Mandatory" not in CODE

    def test_the_two_that_matter_read_the_environment(self):
        assert "$Coordinator = $env:BITPORT_COORDINATOR" in PS1
        assert "$Token       = $env:BITPORT_NODE_TOKEN" in PS1

    def test_a_missing_one_fails_loudly_rather_than_installing_nothing(self):
        assert 'throw "no coordinator.' in PS1
        assert 'throw "no node token.' in PS1


class TestGitIsNotRequired:
    def test_it_never_shells_out_to_git(self):
        """The UAC prompt for git is the single most likely thing to stop
        this run, and nothing here needs git's history."""
        assert not re.search(r"^\s*git[\s\.]", CODE, re.M)
        assert "git clone" not in CODE
        assert "Get-Command git" not in CODE

    def test_it_downloads_the_branch_archive_instead(self):
        assert "archive/refs/heads/" in PS1
        assert "Expand-Archive" in PS1

    def test_the_download_works_on_windows_powershell_5(self):
        """Without -UseBasicParsing, Invoke-WebRequest wants Internet
        Explorer's engine and fails on a machine where IE never ran."""
        assert "-UseBasicParsing" in PS1

    def test_a_rerun_keeps_the_token_and_the_keys(self):
        """Neither is in the archive, and both are hand-placed. A move over
        the top would silently delete the tenant's credentials -- verified
        the other way live: after a second run, node.env and keys/7 both
        survived."""
        assert "Copy-Item" in PS1
        assert "Recurse -Force" in PS1
        assert "Move-Item" not in PS1


class TestItFindsPythonWithoutANewWindow:
    def test_it_reloads_path_after_installing(self):
        assert "Sync-Path" in PS1
        assert PS1.index("winget install") < PS1.rindex("Sync-Path")

    def test_path_is_rebuilt_from_the_registry_not_appended_to(self):
        assert "GetEnvironmentVariable('Path', 'Machine')" in PS1
        assert "GetEnvironmentVariable('Path', 'User')" in PS1

    def test_winget_is_told_not_to_ask_anything(self):
        """The msstore source agreement prompt blocks an unattended run --
        it was answered by hand on the first real attempt."""
        assert "--accept-source-agreements" in PS1
        assert "--accept-package-agreements" in PS1
        assert "--silent" in PS1

    def test_it_runs_python_rather_than_trusting_the_name(self):
        """Windows ships a 0-byte "python" App Execution Alias that opens
        the Store. Finding the name proves nothing."""
        assert "sys.version_info >= (3,10)" in PS1


class TestTheLoopbackPortIsRefusedUpFront:
    def test_it_rejects_8090_before_doing_any_work(self):
        """api_server.py binds 127.0.0.1, so a node aimed at 8090 is
        unreachable. Caught at the top rather than 90 seconds later in the
        probe, where it presents as a network fault."""
        assert ":8090" in PS1
        assert PS1.index("':8090\\s*$'") < PS1.index("1/5")

    def test_the_message_names_the_port_that_works(self):
        assert "Caddy port" in PS1
        assert "81" in PS1
