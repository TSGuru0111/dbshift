# The PostgreSQL path, phases 1 to 10, in demo order.
#
#   .\scripts\demo-postgres\run_demo.ps1
#   .\scripts\demo-postgres\run_demo.ps1 -SkipDiscover      # reuse the last run
#
# Runs every phase that does not bill, in the order a demo should show them, and
# leaves the target populated so the result can be inspected afterwards.
#
# Phases 6 and 7 plan and render only. Deploying an RDS instance and creating a
# DMS replication instance both bill, and a replication instance keeps billing
# for as long as it exists -- so those are an explicit decision, not a step in a
# demo script.

[CmdletBinding()]
param(
    [switch]$SkipDiscover,
    [string]$Estate       = 'DBMIG_APP',
    [string]$ApprovedBy   = 'guru.ts@ganitinc.com',
    [string]$PgDsn        = 'localhost:5432/dbshift',
    [string]$PgUser       = 'dbshift',
    [string]$PgPassword   = 'dbshift-local-only',
    [string]$PriceFile    = ''
)

# Deliberately NOT 'Stop'. Several phases log progress to stderr -- the
# collector, assess, remediate all do -- and with Stop, PowerShell treats a
# native command's stderr as a terminating error and the demo dies on its own
# log output. Exit codes are checked explicitly instead, which is the thing
# that actually says whether a phase succeeded.
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

$script:step = 0
function Phase {
    param([string]$Number, [string]$Name, [string[]]$Argv, [int[]]$Ok = @(0))
    $script:step++
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
    return $code
}

# --- 1  Discover -------------------------------------------------------------
if (-not $SkipDiscover) {
    Phase '1'  'Discover the source'        @('-m', 'collector.run')    | Out-Null
    Phase '1'  'Verify what was collected'  @('-m', 'collector.verify') | Out-Null
} else {
    Write-Host 'skipping discovery; reusing the last collector run' -ForegroundColor Yellow
}

# --- 2  Assess ---------------------------------------------------------------
Phase '2' 'Assess against the rules' @('-m', 'assess.run') | Out-Null

# --- 4b Convert --------------------------------------------------------------
# Before Phase 3 on purpose: the target decision is only honest once the stored
# code has actually been converted and compiled, and this is what measures it.
Phase '4b' 'Convert PL/SQL and compile it' `
    @('-m', 'convert.run', '--approved-by', $ApprovedBy) | Out-Null

# --- 3  Target and sizing ----------------------------------------------------
Phase '3' 'Choose the target, then size it' `
    @('-m', 'sizing.run', '--engine', 'postgresql', '--chosen-by', $ApprovedBy,
      '--conversion', 'convert/output/conversion_plan.json') | Out-Null

# --- 4c Schema DDL -----------------------------------------------------------
Phase '4c' 'Generate the schema DDL and prove it runs' `
    @('-m', 'convert.ddl_run', '--compile') | Out-Null

# --- build the target, then apply -------------------------------------------
# The DDL is applied for real here so the converted code has tables to bind to.
# This is the demo's one destructive step, and it is scoped to the estate schema.
Write-Host ''
Write-Host ('=' * 72) -ForegroundColor DarkGray
Write-Host 'BUILD THE TARGET SCHEMA  (drops and recreates the estate schema)' -ForegroundColor Cyan
Write-Host ('=' * 72) -ForegroundColor DarkGray
& $py (Join-Path $PSScriptRoot 'build_target.py') $Estate
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# --- 4b Apply ----------------------------------------------------------------
Phase '4b' 'Apply the approved code (preflight)' `
    @('-m', 'convert.apply_run') @(0, 2) | Out-Null

Phase '4b' 'Apply the approved code FOR REAL' `
    @('-m', 'convert.apply_run', '--apply', '--confirm', $PgDsn, '--applied-by', $ApprovedBy) | Out-Null

& $py (Join-Path $PSScriptRoot 'build_target.py') $Estate 'count'

# --- 4  Remediate ------------------------------------------------------------
Phase '4' 'Plan the remediations' @('-m', 'remediate.run') | Out-Null

# --- 5  Blocker gate ---------------------------------------------------------
# Exit 1 means HALT, which is a verdict rather than a failure: it is how a
# pipeline stops without parsing JSON.
Phase '5' 'The blocker gate' @('-m', 'blocker.run') @(0, 1) | Out-Null

# --- 6  Provision ------------------------------------------------------------
$provArgs = @('-m', 'provision.run')
if ($PriceFile) { $provArgs += @('--price-file', $PriceFile) }
Phase '6' 'Render the target and check it (nothing is deployed)' $provArgs @(0, 2) | Out-Null

# --- 7  Migrate --------------------------------------------------------------
Phase '7' 'Plan the DMS migration (nothing is created)' @('-m', 'dms.run') @(0, 2) | Out-Null

# --- 10 Report ---------------------------------------------------------------
Phase '10' 'Build the report' @('-m', 'report.run') | Out-Null

Write-Host ''
Write-Host ('=' * 72) -ForegroundColor DarkGray
Write-Host 'DONE' -ForegroundColor Green
Write-Host ('=' * 72) -ForegroundColor DarkGray
Write-Host '  report : report\output\migration_report.html'
Write-Host '  console: python -m web.server   then open http://127.0.0.1:8765'
Write-Host ''
Write-Host '  Phases 8 and 9 need a provisioned target and migrated data.' -ForegroundColor Yellow
Write-Host '  Phases 6 and 7 rendered and planned only -- deploying bills.' -ForegroundColor Yellow
