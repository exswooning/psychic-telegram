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
        """Finding the name proves nothing -- the version has to be run."""
        assert "sys.version_info >= (3,10)" in PS1

    def test_it_never_runs_the_microsoft_store_alias(self):
        """Windows ships an App Execution Alias for "python": a 0-byte stub
        under WindowsApps that writes "Python was not found; run without
        arguments to install from the Microsoft Store" to STDERR.

        Hit live on a laptop that ALREADY had Python 3.12 installed. Under
        Windows PowerShell 5.1 with $ErrorActionPreference='Stop', a native
        command's stderr write becomes a TERMINATING error -- the reported
        failure was literally NativeCommandError -- so probing the stub
        killed the run before the loop could reach `py`. 2>$null does not
        help: the stream is redirected, the error record is still raised.

        Skipping it by path is the fix that does not depend on which
        PowerShell is running.
        """
        assert '"*\\WindowsApps\\*"' in CODE

    def test_it_probes_by_full_path_not_by_name(self):
        """A bare name resolves to whichever copy is first on PATH, which is
        how the alias got run in the first place. -All enumerates every
        candidate so a real interpreter behind the stub is still found."""
        assert "-All" in CODE
        assert "$cmd.Source" in CODE or "$src" in CODE

    def test_every_native_probe_is_isolated(self):
        """Defence in depth for the same failure: all streams redirected,
        the preference lowered around the call, and a try/catch so one bad
        candidate continues the loop instead of ending the install."""
        find = CODE[CODE.index("function Find-Python"):CODE.index("$py = Find-Python")]
        assert "*> $null" in find
        assert "SilentlyContinue" in find
        assert "catch" in find

    def test_the_launcher_is_tried_first(self):
        """`py` is the one name that is never an alias stub."""
        assert '@("py", "python", "python3")' in CODE

    def test_winget_is_not_pinned_to_user_scope(self):
        """A plain install needed no elevation on a real laptop -- observed
        -- because python.org's installer goes per-user when not elevated.
        --scope user can instead fail with "no applicable installer found",
        turning a working path into a dead one."""
        assert "--scope user" not in CODE


class TestTheLoopbackPortIsRefusedUpFront:
    def test_it_rejects_8090_before_doing_any_work(self):
        """api_server.py binds 127.0.0.1, so a node aimed at 8090 is
        unreachable. Caught at the top rather than 90 seconds later in the
        probe, where it presents as a network fault."""
        assert ":8090" in PS1
        # Before any work happens: the first Say() line, whatever it is
        # numbered.
        assert PS1.index("':8090\\s*$'") < PS1.index("checking prerequisites")

    def test_the_message_names_the_port_that_works(self):
        assert "Caddy port" in PS1
        assert "81" in PS1


class TestHardeningTheTokenFileCannotKillTheInstall:
    """Set-Acl needs a privilege an ordinary user does not have.

    Live, on a run that had otherwise completed every step:

        Set-Acl : The process does not possess the 'SeSecurityPrivilege'
        privilege which is required for this operation.

    Set-Acl writes back every section the ACL object carries, and one of
    them is the audit (SACL) section -- whose write needs that privilege.
    Nothing about the token file needs the SACL touched at all; the DACL is
    the thing being changed, and icacls changes only that.
    """

    def test_it_does_not_round_trip_get_acl_into_set_acl(self):
        assert "Set-Acl" not in CODE
        assert "Get-Acl" not in CODE

    def test_it_uses_icacls_on_the_dacl_only(self):
        assert "icacls" in CODE
        assert "/inheritance:r" in CODE
        assert "/grant:r" in CODE

    def test_a_failure_here_does_not_abort_the_install(self):
        """By this point the file is written and the node is configured.
        Aborting over a hardening step throws away a working install -- and
        %USERPROFILE% already grants only the user, SYSTEM and
        Administrators, so what this adds is defence in depth."""
        # Anchored on the next real statement, not on a step number --
        # renumbering the steps broke this once already.
        block = CODE[CODE.index("$hardened = $false"):
                     CODE.index("$env:BITPORT_COORDINATOR = $Coordinator")]
        assert "try {" in block and "} catch {" in block
        assert "throw" not in block

    def test_it_says_what_the_permissions_actually_are_when_it_fails(self):
        """"could not restrict it" without saying what that leaves is an
        alarm with no action attached."""
        assert "SYSTEM, Administrators" in PS1

    def test_it_checks_the_exit_code_not_just_the_absence_of_an_exception(self):
        """icacls reports refusal by exit code and a message on stdout, not
        by throwing -- so a try/catch alone would call every failure a
        success."""
        assert "$LASTEXITCODE -eq 0" in CODE
