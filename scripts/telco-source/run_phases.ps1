# =============================================================================
# run_phases.ps1  --  run Phases 1-5, 4b and 10 against the DBMIG_TELCO estate
#
# The point of this script is that it contains NO code changes: every phase is
# pointed at the second estate purely through environment variables. If a phase
# needs editing to run here, that is a finding about the phase, not a missing
# feature of this script.
#
# Phases 6-9 (provision, migrate, validate, cutover) are not here: they need AWS
# credentials and would bill a second RDS target. Phase 4b compiles against the
# local PostgreSQL from scripts\postgres-target\run_pg.ps1, which must be up.
#
# Outputs go to telco-scoped directories so the DBMIG_APP results are not
# overwritten and the two can be compared afterwards.
#
# Order note: the HTML report is rendered AFTER sizing, because it takes
# --sizing and that file has to exist first.
# =============================================================================

$ErrorActionPreference = 'Continue'
$py   = Join-Path $PSScriptRoot '..\..\.venv\Scripts\python.exe'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Push-Location $root

$env:DBSHIFT_COLLECTOR_PASSWORD = 'DbMig2026Coll'
$env:DBSHIFT_SCHEMAS            = 'DBMIG_TELCO'
$env:DBSHIFT_PRIMARY_SCHEMA     = 'DBMIG_TELCO'
$env:DBSHIFT_REFERENCE_SCHEMA   = 'DBMIG_TELCO'
$env:DBSHIFT_ANSWER_KEY         = (Join-Path $root 'scripts\telco-source\answer_key.json')

$out = Join-Path $root 'telco-output'
New-Item -ItemType Directory -Force -Path $out | Out-Null

function Step($n, $name) {
    Write-Host ""
    Write-Host ("=" * 72)
    Write-Host "  PHASE $n -- $name"
    Write-Host ("=" * 72)
}

Step 1 'Discover (collector)'
& $py -m collector.run --output-dir "$out\collector"
Write-Host "exit=$LASTEXITCODE"

Step 1 'Discover -- second run, so verify has two to compare'
& $py -m collector.run --output-dir "$out\collector"
Write-Host "exit=$LASTEXITCODE"

Step 1 'Verify (reconcile against live)'
& $py -m collector.verify --output-dir "$out\collector"
Write-Host "exit=$LASTEXITCODE"

Step 2 'Assess'
& $py -m assess.run --collector-output "$out\collector" --output-dir "$out\assess"
Write-Host "exit=$LASTEXITCODE"

Step 3 'Size & Edition'
& $py -m sizing.run --collector-output "$out\collector" --output-dir "$out\sizing"
Write-Host "exit=$LASTEXITCODE"

Step 2 'Assess -- HTML report (after sizing, which it consumes)'
& $py -m assess.report --output-dir "$out\assess" --collector-output "$out\collector" --sizing "$out\sizing\sizing.json"
Write-Host "exit=$LASTEXITCODE"

Step 4 'Detect & Remediate'
& $py -m remediate.run --assessment "$out\assess\assessment.json" --output-dir "$out\remediate"
Write-Host "exit=$LASTEXITCODE"

Step '4b' 'Convert PL/SQL (compiles on the local PostgreSQL, applies nothing)'
if (-not $env:DBSHIFT_PG_DSN)      { $env:DBSHIFT_PG_DSN      = 'localhost:5432/dbshift' }
if (-not $env:DBSHIFT_PG_USER)     { $env:DBSHIFT_PG_USER     = 'dbshift' }
if (-not $env:DBSHIFT_PG_PASSWORD) { $env:DBSHIFT_PG_PASSWORD = 'dbshift-local-only' }
& $py -m convert.run --collector-output "$out\collector" --output-dir "$out\convert"
Write-Host "exit=$LASTEXITCODE"

Step 5 'Blocker gate'
& $py -m blocker.run --assessment "$out\assess\assessment.json" --remediation "$out\remediate\remediation_plan.json" --output-dir "$out\blocker"
Write-Host "gate exit=$LASTEXITCODE  (1 = HALT, which is a valid result)"

Step 10 'Report (SCT-style conversion assessment + DMS pre-migration assessment)'
& $py -m report.run --records "$out" --output-dir "$out\report"
Write-Host "exit=$LASTEXITCODE"

Write-Host ""
Write-Host ("=" * 72)
Write-Host "  outputs under $out"
Write-Host ("=" * 72)
Pop-Location
