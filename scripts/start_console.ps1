# Start the DBShift console with everything the demo needs in the environment.
#
# Run it from anywhere: it resolves the repo from its own location, so being in
# the parent folder does not produce "the term '.\.venv\Scripts\python.exe' is
# not recognized".
#
# The collector password is prompted for rather than passed as an argument, so
# it does not land in PowerShell history or in a saved command.
param(
    [int]$Port = 8765,
    [string]$AwsProfile = 'dbshift-bedrock',
    [string]$Region = 'ap-south-1',
    [string]$PgPassword = 'dbshift-local-only'
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Host "No virtualenv at $python" -ForegroundColor Red
    exit 1
}

$env:AWS_PROFILE = $AwsProfile
$env:AWS_DEFAULT_REGION = $Region
$env:DBSHIFT_PG_PASSWORD = $PgPassword

# Keep an already-exported password rather than asking again.
if (-not $env:DBSHIFT_COLLECTOR_PASSWORD) {
    $secure = Read-Host -AsSecureString "Oracle password for dbmig_collector"
    $env:DBSHIFT_COLLECTOR_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}
if (-not $env:DBSHIFT_COLLECTOR_PASSWORD) {
    Write-Host 'No password given; phases 1, 2 and 4b cannot reach the source.' -ForegroundColor Yellow
}

# A console already listening on this port would make the new one fail with a
# bind error that reads like a code fault.
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Host "Port $Port is already in use by pid $($busy.OwningProcess)." -ForegroundColor Yellow
    Write-Host "  Stop-Process -Id $($busy.OwningProcess) -Force" -ForegroundColor Yellow
    exit 1
}

Write-Host "console  http://127.0.0.1:$Port" -ForegroundColor Green
Write-Host "account  $AwsProfile in $Region" -ForegroundColor Green
Write-Host 'Ctrl+C to stop.'
& $python -m uvicorn web.server:app --host 127.0.0.1 --port $Port
