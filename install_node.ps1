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

Say "1/5  checking prerequisites"
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
  foreach ($c in @("python", "python3", "py")) {
    $exe = Get-Command $c -ErrorAction SilentlyContinue
    if (-not $exe) { continue }
    # 3.10 is the floor: the code uses `X | None` annotations at runtime.
    # Windows also ships a "python" App Execution Alias that is a 0-byte
    # stub opening the Store, so actually RUN it rather than trusting the
    # name to exist.
    & $c -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { return $c }
  }
  return $null
}
$py = Find-Python
if (-not $py) {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "no Python 3.10+ and no winget to install it with. Install Python 3.12 from python.org and re-run."
  }
  Write-Host "  no Python 3.10+ -- installing (user scope, no admin prompt)"
  winget install --id Python.Python.3.12 --scope user --silent `
    --accept-source-agreements --accept-package-agreements | Out-Null
  Sync-Path
  $py = Find-Python
  if (-not $py) { throw "installed Python but still cannot find it on PATH -- open a new PowerShell and re-run." }
}
Write-Host "  python: $(& $py --version)"

Say "2/5  fetching the code into $Dir"
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

Say "3/5  creating the virtualenv"
& $py -m venv "$Dir\.venv"
& "$Dir\.venv\Scripts\python.exe" -m pip install -q --upgrade pip
& "$Dir\.venv\Scripts\python.exe" -m pip install -q -r "$Dir\requirements.txt"

Say "4/5  writing node configuration"
$envFile = "$Dir\node.env"
@(
  "BITPORT_COORDINATOR=$Coordinator",
  "BITPORT_NODE_TOKEN=$Token",
  "BITPORT_NODE_ID=$env:COMPUTERNAME"
) | Set-Content -Path $envFile -Encoding ASCII
# Owner-only, the NTFS equivalent of chmod 600: a token in a world-readable
# file is a token anyone on the machine has.
$acl = Get-Acl $envFile
$acl.SetAccessRuleProtection($true, $false)
$acl.SetAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
  "$env:USERDOMAIN\$env:USERNAME", "FullControl", "Allow")))
Set-Acl -Path $envFile -AclObject $acl
Write-Host "  wrote $envFile (owner-only)"

Say "5/5  can this machine reach the coordinator?"
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
Write-Host @"

Still needed -- the tenant credentials, which this script will not fetch:

  scp -r <coordinator>:/opt/bitport/keys/$Account $Dir\keys\$Account
  scp    <coordinator>:/opt/bitport/migration.db  $Dir\migration.db

Then, to take part in a migration:

  cd $Dir
  .\.venv\Scripts\python.exe main.py --account-id $Account migrate --services gmail

Read BEFORE you run Drive across two machines: each node keeps its OWN
ledger, so Drive's duplicate check does not see the other node's work.
Gmail's does -- it asks the target for the Message-ID. See MULTINODE.md.
"@
