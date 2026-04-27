<#
.SYNOPSIS
    Chaos demo script — triggers and fixes RCA incidents by creating PRs.

.DESCRIPTION
    Creates a branch, applies a pre-written patch, commits, pushes, and
    auto-merges a PR. The GitHub Actions workflow deploys the config change
    to the VM, causing the app to fail in a way that matches the target
    incident signature.

.PARAMETER Action
    "break" to trigger an incident, "fix" to restore normal operation.

.PARAMETER Signature
    One of: db_pool_exhaustion, memory_leak_progressive, cpu_saturation_burst,
            cascade_failure, network_partition

.EXAMPLE
    .\demo.ps1 break db_pool_exhaustion
    .\demo.ps1 fix   db_pool_exhaustion

.NOTES
    Prerequisites:
      - git installed
      - gh (GitHub CLI) installed and authenticated: gh auth login
      - This script run from the monitored-repo root
#>

param(
    [Parameter(Mandatory)]
    [ValidateSet("break", "fix")]
    [string]$Action,

    [Parameter(Mandatory)]
    [ValidateSet("db_pool_exhaustion", "memory_leak_progressive",
                 "cpu_saturation_burst", "cascade_failure", "network_partition")]
    [string]$Signature
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
$RepoRoot   = $PSScriptRoot
$PatchDir   = Join-Path $RepoRoot "patches"
$PatchFile  = Join-Path $PatchDir "$Action-$Signature.patch"
$MsgFile    = Join-Path $PatchDir "$Action-$Signature.msg"

# ---------------------------------------------------------------------------
# Validate patch files exist
# ---------------------------------------------------------------------------
if (-not (Test-Path $PatchFile)) {
    Write-Error "Patch file not found: $PatchFile"
    exit 1
}
if (-not (Test-Path $MsgFile)) {
    Write-Error "Message file not found: $MsgFile"
    exit 1
}

$CommitMsg = (Get-Content $MsgFile -Raw).Trim()
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Branch    = "chaos/$Action-$Signature-$Timestamp"

# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------
function Info  ([string]$msg) { Write-Host "  $msg" -ForegroundColor Cyan }
function Ok    ([string]$msg) { Write-Host "  ✅ $msg" -ForegroundColor Green }
function Warn  ([string]$msg) { Write-Host "  ⚠️  $msg" -ForegroundColor Yellow }
function Step  ([string]$msg) { Write-Host "`n▶ $msg" -ForegroundColor White }

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════╗" -ForegroundColor Magenta
Write-Host "║  RCA Chaos Demo — PR-Triggered Incident Generator    ║" -ForegroundColor Magenta
Write-Host "╚══════════════════════════════════════════════════════╝" -ForegroundColor Magenta
Write-Host ""
Info "Action    : $Action"
Info "Signature : $Signature"
Info "Branch    : $Branch"
Info "Commit    : $CommitMsg"
Write-Host ""

# ---------------------------------------------------------------------------
# Step 1: Ensure we're on main and up to date
# ---------------------------------------------------------------------------
Step "Syncing with main branch"
git checkout main | Out-Null
git pull origin main | Out-Null
Ok "On main, pulled latest"

# ---------------------------------------------------------------------------
# Step 2: Create feature branch
# ---------------------------------------------------------------------------
Step "Creating branch: $Branch"
git checkout -b $Branch | Out-Null
Ok "Branch created"

# ---------------------------------------------------------------------------
# Step 3: Apply the patch
# ---------------------------------------------------------------------------
Step "Applying patch: $Action-$Signature.patch"
try {
    git apply $PatchFile
    Ok "Patch applied"
} catch {
    Write-Error "Failed to apply patch: $_"
    git checkout main
    git branch -D $Branch
    exit 1
}

# ---------------------------------------------------------------------------
# Step 4: Commit and push
# ---------------------------------------------------------------------------
Step "Committing and pushing"
git add -A
git commit -m $CommitMsg
git push -u origin $Branch
Ok "Pushed to origin/$Branch"

# ---------------------------------------------------------------------------
# Step 5: Create PR and auto-merge
# ---------------------------------------------------------------------------
Step "Creating and merging PR"
$PrTitle = $CommitMsg
$PrBody  = @"
## Chaos Test: $Action $Signature

**Type**: Automated chaos test  
**Target signature**: ``$Signature``  
**Action**: ``$Action``  
**Timestamp**: $Timestamp  

### What this PR does
$(if ($Action -eq "break") { "Introduces a configuration change that causes a ``$Signature`` incident." } else { "Reverts the configuration change that caused the ``$Signature`` incident." })

### Expected outcome
$(if ($Action -eq "break") {
    "- GitHub Actions deploys config to VM`n- App starts failing in ~2 min`n- Azure Monitor alert fires in ~5 min`n- RCA pipeline classifies as ``$Signature```n- RAG finds this PR as root cause"
} else {
    "- GitHub Actions restores config on VM`n- App recovers within 2 min`n- Metrics return to baseline`n- Ready for next demo"
})
"@

gh pr create `
    --title $PrTitle `
    --body $PrBody `
    --base main `
    --head $Branch

gh pr merge $Branch `
    --squash `
    --delete-branch `
    --auto

Ok "PR created and set to auto-merge"

# ---------------------------------------------------------------------------
# Step 6: Return to main
# ---------------------------------------------------------------------------
git checkout main
git pull origin main

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║  PR merged — incident pipeline triggered!            ║" -ForegroundColor Green
Write-Host "╚══════════════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""

if ($Action -eq "break") {
    Write-Host "⏱️  Expected timeline:" -ForegroundColor Yellow
    Write-Host "   0 min  — GitHub Actions deploys config to VM"
    Write-Host "   2 min  — App restarts with broken config, metrics spike"
    Write-Host "   5 min  — Azure Monitor alert fires (3 consecutive breaches)"
    Write-Host "   5 min  — POST /api/incident/new → ML classifies → RAG finds this PR"
    Write-Host "   6 min  — Incident appears in RCA dashboard"
    Write-Host ""
    Write-Host "👀 Watch the dashboard: http://localhost:3000" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "🔧 To fix: .\demo.ps1 fix $Signature" -ForegroundColor Yellow
} else {
    Write-Host "✅ Recovery PR merged — app will restore in ~2 min" -ForegroundColor Green
    Write-Host "🚀 Ready for next demo!" -ForegroundColor Green
}
Write-Host ""
