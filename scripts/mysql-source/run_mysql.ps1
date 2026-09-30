<#
  A local MySQL 8 for building and testing the source estate -- the MySQL
  counterpart of scripts/postgres-target/run_pg.ps1.

  Started with the binlog settings the estate needs (ROW + FULL), because CDC
  readiness is one of the things Phase 1 collects and a server without them
  reports NOARCHIVELOG-equivalent and cannot be used to test the ready path.

  lower-case-table-names=0 matters: it is the Linux default and the reason the
  collector must not fold schema names on this path. Starting with 1 would hide
  that class of bug on a developer machine and let it surface on a customer's.

  ONLY_FULL_GROUP_BY is deliberately OMITTED from sql_mode, and that is not a
  convenience. **AWS SCT's own metadata query is invalid under it.** Measured
  2026-09-29 against SCT 1.0.677:

    Error executing 'load-partitions-by-schema' query:
    Expression #1 of ORDER BY clause is not in SELECT list, references column
    'information_schema.PARTITIONS.TABLE_SCHEMA' which is not in SELECT list;
    this is incompatible with DISTINCT

  SCT retries three times and then abandons the whole assessment with
  "Metadata loading was interrupted because of data fetching issues". Since
  ONLY_FULL_GROUP_BY is in MySQL 8's DEFAULT sql_mode, **any stock MySQL 8 will
  fail an SCT assessment** until it is relaxed -- a real finding for a client, and
  one their DBA has to action on the source server. It is recorded in
  docs/19-mysql-source.md for that reason.

  This is for DEVELOPMENT. The real estate lives on the EC2 host -- see
  docs/19-mysql-source.md.
#>
param(
  [int]    $Port      = 3399,
  [string] $Password  = 'dbshift-local-only',
  [string] $Container = 'dbshift-mysql',
  [string] $Tag       = '8.0',
  [switch] $Reseed,
  [switch] $Remove
)

# 'Continue', not 'Stop'. The mysql client writes "[Warning] Using a password
# on the command line interface can be insecure" to stderr on every invocation,
# and under 'Stop' PowerShell 5.1 turns that into a terminating NativeCommandError
# -- so a perfectly successful ping kills the script. Exit codes are checked
# explicitly instead, which is what scripts/demo-postgres/run_demo.ps1 does and
# for the same reason.
# ---------------------------------------------------------------------------
# WHY EVERY PASSWORD ARGUMENT IS QUOTED.
#
# `-p$Password` is WRONG in PowerShell when the password contains a hyphen.
# PowerShell splits the unquoted token and mysql receives a truncated password,
# so every call fails with "Access denied for user 'root'@'localhost'" -- which
# reads as a credentials problem rather than a quoting one, and looks identical
# to a server that has not finished initialising.
#
# Measured 2026-09-28 with password 'dbshift-local-only':
#     docker exec c mysql -uroot -p$Password  -e 'SELECT 1'   -> exit 1
#     docker exec c mysql -uroot "-p$Password" -e 'SELECT 1'  -> exit 0
#
# The same commands run correctly from bash, which is what made this confusing:
# the SQL, the container and the password were all fine.
# ---------------------------------------------------------------------------

$ErrorActionPreference = 'Continue'
# $PSScriptRoot = <repo>/scripts/mysql-source, so two levels up IS the repo
# root. The first version went up two and then re-appended 'dbshift', producing
# <repo>/dbshift/dbshift/... and a CommandNotFoundException on python.exe.
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

if ($Remove) {
  docker rm -f $Container 2>$null | Out-Null
  Write-Host "removed $Container"
  exit 0
}

$running = docker ps -a --filter "name=^/$Container$" --format '{{.Names}}'
if (-not $running) {
  Write-Host "starting $Container (mysql:$Tag) on port $Port ..."
  docker run -d --name $Container `
    -e "MYSQL_ROOT_PASSWORD=$Password" `
    -p "${Port}:3306" `
    "mysql:$Tag" `
    --log-bin=binlog --binlog-format=ROW --binlog-row-image=FULL `
    --server-id=1 --lower-case-table-names=0 `
    --sql-mode=STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION | Out-Null
} else {
  docker start $Container 2>$null | Out-Null
  Write-Host "$Container already exists; started it"
}

# A first start on a cold image initialises the data directory, which took well
# over a minute on this machine -- so the timeout is generous and, more
# importantly, RUNNING OUT OF IT IS FATAL. The first version of this script
# carried on regardless and began seeding against a server that was not
# listening: every statement failed with "Access denied for user 'root'",
# which reads as a credentials problem rather than a timing one.
$ready = $false
Write-Host -NoNewline 'waiting for mysqld '
for ($i = 0; $i -lt 180; $i++) {
  # Check the EXIT CODE, never the captured output.
  #
  # mysqladmin writes "[Warning] Using a password on the command line interface
  # can be insecure" to stderr on every call. Assigning `$ping = docker exec ...`
  # inside a loop interleaves that warning into the captured value, so
  # `$ping -match 'alive'` is false even while the server is up -- measured, the
  # loop ran 165 times against a healthy mysqld. The same statement works in
  # isolation, which is what made this confusing.
  #
  # `mysqladmin ping` is NOT the check -- measured, it exits 0 even with a wrong
  # password, because "the server answered" is all ping claims. A real query is
  # the signal: `SELECT 1` exits 0 only when the server is up AND the credentials
  # work, which is what the seeding steps actually need.
  docker exec $Container mysql -uroot "-p$Password" -N -B -e 'SELECT 1' > $null
  if ($LASTEXITCODE -eq 0) {
    Write-Host " ready after ${i}s"
    $ready = $true
    break
  }
  Write-Host -NoNewline '.'
  Start-Sleep -Seconds 1
}
if (-not $ready) {
  Write-Host ''
  Write-Error "mysqld did not accept connections within 180s. Check: docker logs $Container"
  exit 1
}

# Prove the settings the estate depends on, rather than assume the flags took.
$settings = docker exec $Container mysql -uroot "-p$Password" -N -B -e `
  "SELECT CONCAT('version=', VERSION(), ' log_bin=', @@log_bin, ' format=', @@binlog_format, ' row_image=', @@binlog_row_image, ' lctn=', @@lower_case_table_names);"
$settings | Where-Object { $_ -notmatch '\[Warning\]' } | ForEach-Object { Write-Host $_ }

if ($Reseed) {
  Write-Host ''
  Write-Host 'seeding the estate (00 -> 01 -> 02 -> 03 -> 04) ...'
  $dir = $PSScriptRoot

  Get-Content (Join-Path $dir '00_reset.sql') -Raw |
    docker exec -i $Container mysql -uroot "-p$Password"
  if ($LASTEXITCODE -ne 0) { Write-Error '00_reset.sql failed'; exit 1 }

  # The placeholders are replaced in-flight; nothing with a real password is
  # written to disk.
  (Get-Content (Join-Path $dir '01_setup_admin.sql') -Raw) `
    -replace 'CHANGE_ME_APP',  $Password `
    -replace 'CHANGE_ME_COLL', $Password `
    -replace 'CHANGE_ME_DMS',  $Password |
    docker exec -i $Container mysql -uroot "-p$Password"
  if ($LASTEXITCODE -ne 0) { Write-Error '01_setup_admin.sql failed'; exit 1 }

  foreach ($f in '02_schema_objects.sql','03_generate_data.sql','04_seed_defects.sql') {
    Write-Host "  $f"
    Get-Content (Join-Path $dir $f) -Raw |
      docker exec -i $Container mysql -uroot "-p$Password" dbmig_mysql_app
    # Checked per file: a failure in 03 would otherwise leave 04 seeding defects
    # against an estate with no data, which fails on a foreign key and reads as a
    # defect problem rather than a data problem.
    if ($LASTEXITCODE -ne 0) { Write-Error "$f failed"; exit 1 }
  }

  Write-Host ''
  Write-Host 'verifying the defects landed ...'
  $env:DBSHIFT_MYSQL_PASSWORD = $Password
  & (Join-Path $repo '.venv\Scripts\python.exe') `
    (Join-Path $dir 'verify_defects.py') `
    --dsn "127.0.0.1:$Port/dbmig_mysql_app" --user root
  if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Warning 'Defects are missing. Do NOT measure recall against this estate.'
    exit 1
  }
}

Write-Host ''
Write-Host 'Discover this estate with:'
Write-Host ''
Write-Host "  `$env:DBSHIFT_SOURCE_ENGINE      = 'MYSQL'"
Write-Host "  `$env:DBSHIFT_DSN                = '127.0.0.1:$Port/dbmig_mysql_app'"
Write-Host "  `$env:DBSHIFT_SCHEMAS            = 'dbmig_mysql_app,dbmig_mysql_rpt'"
Write-Host "  `$env:DBSHIFT_COLLECTOR_USER     = 'dbmig_collector'"
Write-Host "  `$env:DBSHIFT_COLLECTOR_PASSWORD = '$Password'"
Write-Host '  .\.venv\Scripts\python.exe -m collector.run'
