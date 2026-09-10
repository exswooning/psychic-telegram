<#
.SYNOPSIS
  Take Bitport off this Windows machine.

.DESCRIPTION
  The inverse of install_node.ps1, with the same asymmetry as uninstall.sh:
  it removes the SOFTWARE and keeps the DATA unless you say otherwise.

  keys\ holds the service-account credentials for real tenants -- replacing
  them means re-doing domain-wide delegation on both Google tenants by hand.
  migration.db is the record of what this node already copied, and Drive's
  duplicate check reads it: lose the ledger and a resumed migration re-copies
  every file it already moved. Neither should go to a flag nobody read.

  A node holds no systemd units and no /etc/bitport, so this is a directory
  and a token -- much less than the Linux coordinator has to undo.

.EXAMPLE
  irm https://raw.githubusercontent.com/exswooning/psychic-telegram/workspace-migrator/uninstall_node.ps1 | iex

.EXAMPLE
  .\uninstall_node.ps1 -Yes -Purge
#>
param(
  [string]$Dir = $(if ($env:BITPORT_DIR) { $env:BITPORT_DIR } else { "$env:USERPROFILE\bitport" }),
  # Not Mandatory, and not [switch] with a default: under `iex` there is no
  # argv, so the environment is the only way to answer, exactly as with the
  # installer.
  [bool]$Yes   = ($env:BITPORT_UNINSTALL_YES -eq '1'),
  [bool]$Purge = ($env:BITPORT_UNINSTALL_PURGE -eq '1'),
  [bool]$Force = ($env:BITPORT_UNINSTALL_FORCE -eq '1')
)

$ErrorActionPreference = "Stop"
function Say($m) { Write-Host "`n== $m" }

if (-not (Test-Path $Dir)) {
  Write-Host "Nothing to remove: $Dir does not exist."
  return
}

Say "Found"
Write-Host "  install directory: $Dir"

# Removing the tree under a running migration leaves it writing into deleted
# files: it neither stops nor finishes, and the ledger it was updating is the
# thing you would need in order to resume.
# try/catch, not -ErrorAction: with $ErrorActionPreference='Stop' a cmdlet
# that does not exist at all is a TERMINATING error and -ErrorAction does
# not cover command-not-found. The same shape killed install_node.ps1 on the
# Store's python stub. A safety check that can itself block the uninstall is
# worse than no check -- if the query fails, say so and carry on.
$busy = @()
try {
  $busy = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -match 'main\.py.*(migrate|seed)' })
} catch {
  Write-Host "  ! could not check for running migrations on this machine ($($_.Exception.GetType().Name))"
}
if ($busy.Count -gt 0) {
  Write-Host "  ! a migration looks like it is RUNNING on this machine:"
  $busy | Select-Object -First 3 | ForEach-Object {
    Write-Host ("      pid {0}: {1}" -f $_.ProcessId, $_.CommandLine.Substring(0, [Math]::Min(90, $_.CommandLine.Length)))
  }
  if (-not $Force) {
    throw "refusing while it runs. Let it finish, or set BITPORT_UNINSTALL_FORCE=1."
  }
  Write-Host "  ! force given: continuing anyway"
}

$keepable = @("keys", "migration.db", "data", "identities.csv")

Say "Would remove"
Write-Host "  code      $Dir (excluding the items below unless purging)"
foreach ($k in $keepable) {
  $p = Join-Path $Dir $k
  if (Test-Path $p) {
    if ($Purge) { Write-Host "  DATA      $p" } else { Write-Host "  keeping   $p" }
  }
}
$envFile = Join-Path $Dir "node.env"
if (Test-Path $envFile) {
  if ($Purge) { Write-Host "  SECRETS   $envFile (node token)" }
  else        { Write-Host "  keeping   $envFile" }
}

if (-not $Yes) {
  Write-Host "`nNothing was changed. Set BITPORT_UNINSTALL_YES=1 to do it"
  Write-Host "(and BITPORT_UNINSTALL_PURGE=1 to include the data), or pass -Yes."
  return
}

if ($Purge) {
  Write-Host "`n--purge deletes the service-account keys and the migration ledger."
  Write-Host "Re-doing domain-wide delegation on both tenants is the only way back."
  $reply = Read-Host "Type PURGE to confirm"
  if ($reply -ne "PURGE") { throw "not confirmed -- nothing was changed" }
}

Say "Files"
# Never delete the directory we are standing in.
if ((Get-Location).Path -like "$Dir*") { Set-Location $env:USERPROFILE }

if ($Purge) {
  Remove-Item -Recurse -Force $Dir
  Write-Host "  removed $Dir"
} else {
  # Move the unrecreatable things aside, delete the rest, move them back.
  # Simpler to get right than enumerating what to delete, and it never
  # leaves a half-removed tree if something fails midway.
  $save = Join-Path ([System.IO.Path]::GetTempPath()) "bitport-keep-$PID"
  New-Item -ItemType Directory -Force -Path $save | Out-Null
  $moved = 0
  foreach ($k in $keepable + @("node.env")) {
    $p = Join-Path $Dir $k
    if (Test-Path $p) { Move-Item -Force $p $save; $moved++ }
  }
  Remove-Item -Recurse -Force $Dir
  New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  Get-ChildItem -Force $save | ForEach-Object { Move-Item -Force $_.FullName $Dir }
  Remove-Item -Recurse -Force $save -ErrorAction SilentlyContinue
  Write-Host "  removed the code from $Dir, kept $moved item(s)"
}

Say "Done"
Write-Host "  Bitport is off this machine."
if (-not $Purge) {
  Write-Host "  Still there: $Dir (the keys, the ledger and the node token)."
  Write-Host "  Re-running the installer over it picks them back up."
}
Write-Host "  Python and any other tools the installer added are left alone --"
Write-Host "  this machine may be using them for something else."
