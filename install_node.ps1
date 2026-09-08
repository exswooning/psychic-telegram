<#
.SYNOPSIS
  Join this Windows machine to a Bitport coordinator.

.DESCRIPTION
  The Windows half of install_node.sh. Run it ON the machine that is
  joining -- node_setup.sh drives a fresh Ubuntu box over SSH from a third
  machine, which is the wrong shape for a laptop: it moves networks, it
  sleeps, and it is the machine you are already sitting at.

  The coordinator URL is whatever THIS machine can reach. On a tailnet that
  is the coordinator's Tailscale address -- no port forwarding, no public
  exposure, and the token is still required on every call.

  It deliberately does NOT fetch the tenant's service-account keys. Those
  are the credentials for the whole tenant, and an endpoint that served them
  to anything holding a node token would make the token equivalent to the
  keys. Copy them yourself; the last step prints how.

.EXAMPLE
  .\install_node.ps1 -Coordinator http://100.x.y.z:8090 -Token $env:TOKEN
#>
param(
  [Parameter(Mandatory = $true)][string]$Coordinator,
  [Parameter(Mandatory = $true)][string]$Token,
  [string]$Dir = "$env:USERPROFILE\bitport",
  [int]$Account = 7,
  [string]$Repo = "https://github.com/exswooning/psychic-telegram",
  [string]$Branch = "workspace-migrator"
)

$ErrorActionPreference = "Stop"
function Say($m) { Write-Host "`n== $m" }

Say "1/5  checking prerequisites"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  throw "git is not installed. winget install --id Git.Git"
}
# 3.10 is the floor: the code uses `X | None` annotations at runtime.
$py = $null
foreach ($c in @("python", "python3", "py")) {
  $exe = Get-Command $c -ErrorAction SilentlyContinue
  if (-not $exe) { continue }
  & $c -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>$null
  if ($LASTEXITCODE -eq 0) { $py = $c; break }
}
if (-not $py) { throw "no Python 3.10+ found. winget install --id Python.Python.3.12" }
Write-Host "  python: $(& $py --version)"

Say "2/5  fetching the code into $Dir"
if (Test-Path "$Dir\.git") {
  git -C $Dir fetch --quiet; git -C $Dir checkout --quiet $Branch; git -C $Dir pull --quiet
} else {
  git clone --quiet -b $Branch $Repo $Dir
}

Say "3/5  creating the virtualenv"
& $py -m venv "$Dir\.venv"
& "$Dir\.venv\Scripts\pip.exe" install -q --upgrade pip
& "$Dir\.venv\Scripts\pip.exe" install -q -r "$Dir\requirements.txt"

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

  scp -r <coordinator>:/root/migration/keys/$Account $Dir\keys\$Account
  scp    <coordinator>:/root/migration/migration.db  $Dir\migration.db

Then, to take part in a migration:

  cd $Dir
  .\.venv\Scripts\python.exe main.py --account-id $Account migrate --services gmail

Read BEFORE you run Drive across two machines: each node keeps its OWN
ledger, so Drive's duplicate check does not see the other node's work.
Gmail's does -- it asks the target for the Message-ID. See MULTINODE.md.
"@
