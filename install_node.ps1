<#
.SYNOPSIS
  Join this Windows machine to a Bitport coordinator. No prompts, no admin.

.DESCRIPTION
  The Windows half of install_node.sh. Run it ON the machine that is
  joining -- node_setup.sh drives a fresh Ubuntu box over SSH from a third
  machine, which is the wrong shape for a laptop: it moves networks, it
  sleeps, and it is the machine you are already sitting at.

  Designed to run start-to-finish without a single click. Three things used
  to need one, and all three are gone:

    * git. `winget install Git.Git` triggers a UAC prompt, and dismissing it
      leaves "You cancelled the installation" plus a run that fails four
      commands later on `git: not recognized`. Observed live. The code now
      arrives as a branch zip over HTTPS, so git is never needed at all.
      Python's installer is user-scope and needs no elevation, so it stays.

    * A new shell. winget writes PATH to the registry; the running process
      keeps its stale copy, so the python it just installed is invisible.
      Reloaded in-process below instead.

    * The execution policy. Windows PowerShell defaults to Restricted, so
      `.\install_node.ps1` refuses to run. Piping into iex is not a file
      and is not subject to it -- which is why the one-liner is the
      documented form.

  Coordinator and token come from parameters OR the environment, and
  nothing is Mandatory: a Mandatory parameter under `iex` stops and prompts,
  which is exactly the interruption this is meant to avoid.

  It deliberately does NOT fetch the tenant's service-account keys. Those
  are the credentials for the whole tenant, and an endpoint that served them
  to anything holding a node token would make the token equivalent to the
  keys. Copy them yourself; the last step prints how.

.EXAMPLE
  $env:BITPORT_COORDINATOR='http://192.168.1.50:81'
  $env:BITPORT_NODE_TOKEN='...'
  irm https://raw.githubusercontent.com/exswooning/psychic-telegram/workspace-migrator/install_node.ps1 | iex

.EXAMPLE
  .\install_node.ps1 -Coordinator http://192.168.1.50:81 -Token $env:TOKEN
#>
param(
  [string]$Coordinator = $env:BITPORT_COORDINATOR,
  [string]$Token       = $env:BITPORT_NODE_TOKEN,
  [string]$Dir         = $(if ($env:BITPORT_DIR) { $env:BITPORT_DIR } else { "$env:USERPROFILE\bitport" }),
  [int]$Account        = $(if ($env:BITPORT_ACCOUNT) { [int]$env:BITPORT_ACCOUNT } else { 7 }),
  [string]$Repo        = "https://github.com/exswooning/psychic-telegram",
  [string]$Branch      = "workspace-migrator"
)

$ErrorActionPreference = "Stop"
function Say($m) { Write-Host "`n== $m" }

if (-not $Coordinator) {
  throw "no coordinator. Pass -Coordinator http://<host>:<caddy-port> or set BITPORT_COORDINATOR."
}
if (-not $Token) {
  throw "no node token. Pass -Token '<token>' or set BITPORT_NODE_TOKEN. Read it off the coordinator: grep BITPORT_NODE_TOKEN /etc/bitport/node.env"
}
# The Caddy port, never 8090 -- api_server.py binds 127.0.0.1 only, so a
# node aimed at 8090 gets connection refused from a machine that is
# otherwise perfectly configured. Caught here rather than 90 seconds later
# in the reachability probe, where it looks like a network fault.
if ($Coordinator -match ':8090\s*$') {
  throw "8090 is api_server's LOOPBACK port and is not reachable from this machine. Use the Caddy port the installer reported (80, or 81 if 80 was taken)."
}

Say "1/6  checking prerequisites"
function Sync-Path {
  # winget writes PATH to the registry; this process still holds the copy it
  # started with. Without this, a just-installed python is invisible until a
  # new window is opened -- which is a click, and this script has none.
  $env:Path = (@(
    [Environment]::GetEnvironmentVariable('Path', 'Machine'),
    [Environment]::GetEnvironmentVariable('Path', 'User')
  ) | Where-Object { $_ }) -join ';'
}
function Find-Python {
  # Windows ships an App Execution Alias for "python": a 0-byte stub under
  # %LOCALAPPDATA%\Microsoft\WindowsApps that writes "Python was not found;
  # run without arguments to install from the Microsoft Store" to STDERR and
  # opens the Store. Two separate problems come from it, and both were hit
  # live on a laptop that already had Python 3.12 installed:
  #
  #   * $ErrorActionPreference='Stop' turns a native command's stderr write
  #     into a TERMINATING error, so probing the stub killed the whole run
  #     before the loop could reach `py`. 2>$null was not enough; the stream
  #     is redirected but the error record is still raised.
  #   * Even suppressed, running it can pop the Store.
  #
  # So: skip anything under WindowsApps by path, probe by full path rather
  # than by name (a name resolves to whichever copy is first on PATH), and
  # keep every native call inside SilentlyContinue plus a try/catch.
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'SilentlyContinue'
  try {
    # `py`, the official launcher, first: it is the one name that is never
    # an alias stub.
    foreach ($name in @("py", "python", "python3")) {
      foreach ($cmd in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
        $src = $cmd.Source
        if (-not $src) { continue }
        if ($src -like "*\WindowsApps\*") { continue }
        try {
          # 3.10 is the floor: the code uses `X | None` annotations at
          # runtime. *> $null covers every stream, not just stderr.
          & $src -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" *> $null
          if ($LASTEXITCODE -eq 0) { return $src }
        } catch { continue }
      }
    }
  } finally { $ErrorActionPreference = $prev }
  return $null
}
$py = Find-Python
if (-not $py) {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "no Python 3.10+ and no winget to install it with. Install Python 3.12 from python.org and re-run."
  }
  Write-Host "  no Python 3.10+ -- installing (user scope, no admin prompt)"
  # No --scope: observed live, a plain install needs no elevation because
  # python.org's installer goes per-user when it is not run elevated, while
  # --scope user can fail outright with "no applicable installer found".
  winget install --id Python.Python.3.12 --silent `
    --accept-source-agreements --accept-package-agreements | Out-Null
  Sync-Path
  $py = Find-Python
  if (-not $py) { throw "installed Python but still cannot find it on PATH -- open a new PowerShell and re-run." }
}
Write-Host "  python: $(& $py --version)"

Say "2/6  fetching the code into $Dir"
# A branch zip over HTTPS, not a git clone: git's installer is machine-scope
# and prompts for elevation, and that prompt is the single most likely thing
# to stop this run. Nothing here needs git's history.
# [IO.Path]::GetTempPath() rather than $env:TEMP: same directory on
# Windows, but defined everywhere, so this script can be run end-to-end
# on a non-Windows box to check it before it ever meets a real laptop.
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) "bitport-fetch-$PID"
$zip = "$tmp\src.zip"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
  $url = "$Repo/archive/refs/heads/$Branch.zip"
  Write-Host "  downloading $url"
  # -UseBasicParsing for Windows PowerShell 5.1, where the default path
  # wants Internet Explorer's engine and fails on a machine where IE has
  # never been opened.
  Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip
  Expand-Archive -Path $zip -DestinationPath $tmp -Force
  $extracted = Get-ChildItem -Path $tmp -Directory | Select-Object -First 1
  if (-not $extracted) { throw "the archive contained no directory" }
  New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  # Copy, not move-over-the-top: an existing $Dir may already hold node.env
  # and the keys copied in by hand, and neither is in the archive. A merge
  # updates the code and leaves those standing.
  Copy-Item -Path "$($extracted.FullName)\*" -Destination $Dir -Recurse -Force
} finally {
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
Write-Host "  code in place"

Say "3/6  creating the virtualenv"
& $py -m venv "$Dir\.venv"
& "$Dir\.venv\Scripts\python.exe" -m pip install -q --upgrade pip
& "$Dir\.venv\Scripts\python.exe" -m pip install -q -r "$Dir\requirements.txt"

Say "4/6  writing node configuration"
$envFile = "$Dir\node.env"
@(
  "BITPORT_COORDINATOR=$Coordinator",
  "BITPORT_NODE_TOKEN=$Token",
  "BITPORT_NODE_ID=$env:COMPUTERNAME"
) | Set-Content -Path $envFile -Encoding ASCII
# Owner-only, the NTFS equivalent of chmod 600: a token in a world-readable
# file is a token anyone on the machine has.
#
# icacls, not Get-Acl/Set-Acl. Set-Acl writes back every section the object
# it was given carries, and one of those is the audit (SACL) section, whose
# write needs SeSecurityPrivilege -- which an ordinary user does not have.
# Observed live, on a run that had otherwise completed:
#
#   Set-Acl : The process does not possess the 'SeSecurityPrivilege'
#   privilege which is required for this operation.
#
# icacls touches the DACL only, which is the thing actually being changed.
#   /inheritance:r  drop the rules inherited from the parent directory
#   /grant:r        replace, rather than add to, this user's grants
#
# NON-FATAL. By this point the file is written and the node is configured;
# aborting over a hardening step would throw away a working install. And the
# default is not open: %USERPROFILE% already grants only this user, SYSTEM
# and Administrators, so what this removes is inherited access an admin
# could grant themselves anyway. Worth doing, not worth failing for.
$hardened = $false
try {
  $out = & icacls $envFile /inheritance:r /grant:r "${env:USERNAME}:(F)" 2>&1
  $hardened = ($LASTEXITCODE -eq 0)
  if (-not $hardened) { Write-Host "  ! icacls: $out" }
} catch {
  Write-Host "  ! could not restrict the file ($($_.Exception.GetType().Name))"
}
if ($hardened) {
  Write-Host "  wrote $envFile (owner-only)"
} else {
  Write-Host "  wrote $envFile"
  Write-Host "    could not restrict it further -- it still inherits your"
  Write-Host "    profile's permissions (you, SYSTEM, Administrators)."
}

Say "5/6  can this machine reach the coordinator?"
$env:BITPORT_COORDINATOR = $Coordinator
$env:BITPORT_NODE_TOKEN  = $Token
$env:BITPORT_NODE_ID     = $env:COMPUTERNAME
Push-Location $Dir
$probe = @"
import user_claims as uc
print('  node id     :', uc.node_id())
print('  coordinator :', uc.coordinator_url())
try:
    ok, why = uc.acquire($Account, '__preflight__@invalid', node=uc.node_id())
    print('  reachable   :', 'yes' if ok else why)
    uc.release($Account, '__preflight__@invalid', node=uc.node_id())
    # Announce this machine, so the page that handed out the join code can
    # say "connected" instead of leaving the operator to guess. Best effort:
    # a coordinator that will not take a heartbeat has still proved
    # reachable on the line above, which is what this step is testing.
    try:
        import json, os, urllib.request
        req = urllib.request.Request(
            uc.coordinator_url().rstrip('/') + '/api/v2/fleet/heartbeat',
            data=json.dumps({'node_id': uc.node_id(),
                             'hostname': uc.node_id()}).encode(),
            method='POST',
            headers={'Content-Type': 'application/json',
                     'X-Node-Token': os.getenv('BITPORT_NODE_TOKEN', '')})
        urllib.request.urlopen(req, timeout=15).read()
        print('  announced   : yes')
    except Exception as exc:
        print('  announced   : no --', str(exc)[:80])
except Exception as exc:
    print('  reachable   : NO --', str(exc)[:160])
    raise SystemExit(1)
"@
& "$Dir\.venv\Scripts\python.exe" -c $probe
$rc = $LASTEXITCODE
Pop-Location

Write-Host ""
if ($rc -eq 0) { Write-Host "Node is ready." }
else { Write-Host "Node installed, but it could NOT reach the coordinator." }
Say "6/6  starting the agent at logon"
# One-time setup, so nothing has to be started by hand again -- including
# after a reboot, which a laptop does often.
#
# A Scheduled Task, not a Windows service: registering a service needs
# admin, and this whole installer deliberately needs none. AtLogOn runs it
# as this user, with this user's profile, which is where node.env and the
# venv live.
#
# Non-fatal for the same reason the ACL step is: the node is configured and
# working by now, and a machine where this cannot be registered still runs
# the agent perfectly well when started by hand.
$taskName = "Bitport node agent"
try {
  $action = New-ScheduledTaskAction -Execute "$Dir\.venv\Scripts\python.exe" `
                                    -Argument "node_agent.py" -WorkingDirectory $Dir
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
  Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
      -Settings $settings -Force -ErrorAction Stop | Out-Null
  Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
  Write-Host "  registered '$taskName' -- it starts at logon and is running now"
  $agentStarted = $true
} catch {
  Write-Host "  ! could not register the scheduled task ($($_.Exception.GetType().Name))"
  Write-Host "    start it by hand instead:"
  Write-Host "      cd $Dir; .\.venv\Scripts\python.exe node_agent.py"
  $agentStarted = $false
}

Write-Host @"

Still needed -- the tenant credentials, which this script will not fetch.
Run the first line ON THE COORDINATOR, in whatever directory Bitport is
installed in there (/root/migration and /opt/bitport are both common):

  ./export_node_config.py --account-id $Account --out node-config.db

Then copy both to this machine:

  scp <coordinator>:<bitport-dir>/node-config.db     $Dir\migration.db
  scp -r <coordinator>:<bitport-dir>/keys/$Account   $Dir\keys\$Account

node-config.db, not the coordinator's own migration.db. That file also
holds every customer's password hash, live sessions and the audit log, and
a node reads exactly five columns of one table out of it.

Then, to take part in a migration:

  cd $Dir
  .\.venv\Scripts\python.exe main.py --account-id $Account migrate --services gmail

Read BEFORE you run Drive across two machines: each node keeps its OWN
ledger, so Drive's duplicate check does not see the other node's work.
Gmail's does -- it asks the target for the Message-ID. See MULTINODE.md.
"@
