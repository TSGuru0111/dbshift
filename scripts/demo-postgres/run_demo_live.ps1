# The PostgreSQL path with the model tier LIVE, phases 1 to 10.
#
#   .\scripts\demo-postgres\run_demo_live.ps1
#
# Identical to run_demo.ps1 except that Phase 3 asks Bedrock to propose the
# sizing, Phase 4 asks it to write remediation SQL, and Phase 4b asks it to
# convert any PL/SQL the deterministic rules decline.
#
# The gates do not change. A model-authored fix passes exactly the five gates a
# template does, and the rules engine still overrides the sizing proposal where
# the evidence disagrees. That is the point of running it this way: the demo
# shows the model being *checked*, not trusted.

[CmdletBinding()]
param(
    [switch]$SkipDiscover,
    [string]$Estate      = 'DBMIG_APP',
    [string]$ApprovedBy  = 'guru.ts@ganitinc.com',
    [string]$PgDsn       = 'localhost:5432/dbshift',
    [string]$PgUser      = 'dbshift',
    [string]$PgPassword  = 'dbshift-local-only',
    [string]$PriceFile   = ''
)

# Not 'Stop': several phases log progress to stderr, and with Stop PowerShell
# treats a native command's stderr as a terminating error. Exit codes are
# checked explicitly instead.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
$py = Join-Path $root '.venv\Scripts\python.exe'

$env:DBSHIFT_PG_DSN      = $PgDsn
$env:DBSHIFT_PG_USER     = $PgUser
$env:DBSHIFT_PG_PASSWORD = $PgPassword
$env:PYTHONIOENCODING    = 'utf-8'
if (-not $env:DBSHIFT_COLLECTOR_PASSWORD) {
    Write-Host 'Set DBSHIFT_COLLECTOR_PASSWORD first.' -ForegroundColor Red
    exit 1
}
if (-not $env:AWS_PROFILE) { $env:AWS_PROFILE = 'dbshift-bedrock' }

function Phase {
    param([string]$Number, [string]$Name, [string[]]$Argv, [int[]]$Ok = @(0))
    Write-Host ''
    Write-Host ('=' * 72) -ForegroundColor DarkGray
    Write-Host "PHASE $Number  $Name" -ForegroundColor Cyan
    Write-Host ('=' * 72) -ForegroundColor DarkGray
    & $py @Argv
    $code = $LASTEXITCODE
    if ($Ok -notcontains $code) {
        Write-Host "  phase $Number exited $code" -ForegroundColor Red
        exit $code
    }
}

Write-Host ''
Write-Host 'Model tier: LIVE (Bedrock, Sonnet 4.6). The gates are unchanged.' -ForegroundColor Yellow

# --- 0  the model actually answers -------------------------------------------
# Before anything else: if the model cannot be invoked, a live run would fall
# back silently at each phase and the demo would claim more than it did.
Phase '0' 'Verify the model tier invokes' @('-m', 'bedrock.verify')

# --- 1  Discover -------------------------------------------------------------
if (-not $SkipDiscover) {
    Phase '1' 'Discover the source'       @('-m', 'collector.run')
    Phase '1' 'Verify what was collected' @('-m', 'collector.verify')
} else {
    Write-Host 'skipping discovery; reusing the last collector run' -ForegroundColor Yellow
}

# --- 2  Assess ---------------------------------------------------------------
Phase '2' 'Assess against the rules' @('-m', 'assess.run')

# --- 4b Convert, with the model available ------------------------------------
# Before Phase 3 on purpose: the target decision is only honest once the stored
# code has been converted and compiled, and this is what measures it.
Phase '4b' 'Convert PL/SQL (model available for what rules decline)' `
    @('-m', 'convert.run', '--approved-by', $ApprovedBy, '--model-mode', 'live')

# --- 3  Target and sizing, proposed by the model -----------------------------
Phase '3' 'Model proposes the target; the rules engine decides' `
    @('-m', 'sizing.run', '--bedrock', '--engine', 'postgresql', '--chosen-by', $ApprovedBy,
      '--conversion', 'convert/output/conversion_plan.json')

# --- 4c Schema DDL -----------------------------------------------------------
Phase '4c' 'Generate the schema DDL and prove it runs' @('-m', 'convert.ddl_run', '--compile')

# --- build the target, then apply -------------------------------------------
Write-Host ''
Write-Host ('=' * 72) -ForegroundColor DarkGray
Write-Host 'BUILD THE TARGET SCHEMA  (drops and recreates the estate schema)' -ForegroundColor Cyan
Write-Host ('=' * 72) -ForegroundColor DarkGray
& $py (Join-Path $PSScriptRoot 'build_target.py') $Estate
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Phase '4b' 'Apply the approved code FOR REAL' `
    @('-m', 'convert.apply_run', '--apply', '--confirm', $PgDsn, '--applied-by', $ApprovedBy)

& $py (Join-Path $PSScriptRoot 'build_target.py') $Estate 'count'

# --- 4  Remediate, with the model writing SQL --------------------------------
Phase '4' 'Model writes remediation SQL; the five gates judge it' `
    @('-m', 'remediate.run', '--model-mode', 'live')

# --- 5  Blocker gate ---------------------------------------------------------
# Exit 1 means HALT, which is a verdict rather than a failure.
Phase '5' 'The blocker gate' @('-m', 'blocker.run') @(0, 1)

# --- 6  Provision ------------------------------------------------------------
$provArgs = @('-m', 'provision.run')
if ($PriceFile) { $provArgs += @('--price-file', $PriceFile) }
Phase '6' 'Render the target and check it (nothing is deployed)' $provArgs @(0, 2)

# --- 7  Migrate --------------------------------------------------------------
Phase '7' 'Plan the DMS migration (nothing is created)' @('-m', 'dms.run') @(0, 2)

# --- 10 Report ---------------------------------------------------------------
Phase '10' 'Build the report' @('-m', 'report.run')

Write-Host ''
Write-Host ('=' * 72) -ForegroundColor DarkGray
Write-Host 'DONE  -- model tier was live throughout' -ForegroundColor Green
Write-Host ('=' * 72) -ForegroundColor DarkGray
Write-Host '  report : report\output\migration_report.html'
Write-Host '  console: python -m web.server   then open http://127.0.0.1:8765'
Write-Host ''
Write-Host '  Phases 8 and 9 need a provisioned target and migrated data.' -ForegroundColor Yellow
