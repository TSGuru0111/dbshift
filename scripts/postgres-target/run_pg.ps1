# Local PostgreSQL for Phase 4b's compile gate.
#
# Nothing PostgreSQL-shaped is installed on this machine and installing it needs
# admin rights, so the compile target is the official postgres image in Docker
# Desktop. It stands in for an Aurora PostgreSQL target the same way
# DBMIG_REHEARSAL stands in for a rehearsal RDS instance: pointing the phase at
# the real thing later is a DSN change.
#
# The gate creates every converted object inside one transaction and rolls it
# back, so this database stays empty between runs. Local only; the password is
# for this container and nothing else.
#
#   .\scripts\postgres-target\run_pg.ps1            # start (or restart) it
#   .\scripts\postgres-target\run_pg.ps1 -Stop      # stop and remove it
#
# Then, in the shell that runs the phase:
#   $env:DBSHIFT_PG_DSN      = 'localhost:5432/dbshift'
#   $env:DBSHIFT_PG_USER     = 'dbshift'
#   $env:DBSHIFT_PG_PASSWORD = '<the password below>'

param(
    [switch]$Stop,
    [string]$Password = $(if ($env:DBSHIFT_PG_PASSWORD) { $env:DBSHIFT_PG_PASSWORD } else { 'dbshift-local-only' }),
    [int]$Port = 5432,
    [string]$Image = 'postgres:16-alpine'
)

$name = 'dbshift-pg'

if ($Stop) {
    docker rm -f $name 2>$null | Out-Null
    Write-Host "$name removed."
    exit 0
}

docker info 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Docker engine is not running. Start Docker Desktop and retry."
    exit 1
}

$existing = docker ps -a --filter "name=^/$name$" --format '{{.Status}}'
if ($existing) {
    if ($existing -like 'Up*') { Write-Host "$name already running."; }
    else { docker start $name | Out-Null; Write-Host "$name started." }
} else {
    docker run -d --name $name `
        -e POSTGRES_USER=dbshift -e POSTGRES_PASSWORD=$Password -e POSTGRES_DB=dbshift `
        -p "${Port}:5432" $Image | Out-Null
    Write-Host "$name created from $Image on port $Port."
}

# Wait until it accepts connections; the image restarts once during init.
$ready = $false
for ($i = 0; $i -lt 30 -and -not $ready; $i++) {
    Start-Sleep -Seconds 2
    docker exec $name pg_isready -U dbshift -d dbshift 2>$null | Out-Null
    $ready = ($LASTEXITCODE -eq 0)
}
if (-not $ready) { Write-Error "PostgreSQL did not become ready."; exit 1 }

Write-Host ""
Write-Host "PostgreSQL ready: localhost:$Port/dbshift as dbshift"
Write-Host "  `$env:DBSHIFT_PG_DSN = 'localhost:$Port/dbshift'; `$env:DBSHIFT_PG_USER = 'dbshift'; `$env:DBSHIFT_PG_PASSWORD = '<password>'"
