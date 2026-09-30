#!/usr/bin/env bash
# Turn a fresh MySQL 8 install on EC2 into the DBShift source estate.
#
#   scp -i <key> -r scripts/mysql-source ec2-user@<host>:~/
#   ssh -i <key> ec2-user@<host> 'sudo bash mysql-source/ec2_bootstrap.sh <password>'
#
# Runs ON THE HOST, and that is the point: MySQL's generated temporary root
# password is read from /var/log/mysqld.log, used, and rotated here. It is never
# printed to an operator's terminal and never crosses the network.
#
# Idempotent. Re-running it resets the estate from 00_reset.sql onward, which is
# what makes a broken seed recoverable without rebuilding the instance.
set -euo pipefail

PASSWORD="${1:?usage: ec2_bootstrap.sh <password-for-dbshift-accounts>}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG=/var/log/mysqld.log

say() { printf '\n== %s\n' "$*"; }

say "MySQL version and service state"
mysqld --version
systemctl is-active mysqld

# ---------------------------------------------------------------------------
# The root password. MySQL 8 generates one on first start and writes it to the
# error log; it must be changed before the server will accept any other
# statement. On a re-run the marker file tells us it has already been rotated.
# ---------------------------------------------------------------------------
MARKER=/root/.dbshift-root-rotated
if [[ -f "$MARKER" ]]; then
  say "root password already rotated (found $MARKER)"
  MYSQL=(mysql -uroot -p"$PASSWORD")
else
  say "rotating the generated root password"
  TEMP_PW="$(grep 'temporary password' "$LOG" | tail -1 | sed 's/.*root@localhost: //')"
  if [[ -z "$TEMP_PW" ]]; then
    echo "FATAL: no temporary password in $LOG. Was mysqld ever started fresh?" >&2
    exit 1
  fi
  # --connect-expired-password: the generated password is expired by design, so
  # ALTER USER is the only statement the server will accept until it is changed.
  mysql --connect-expired-password -uroot -p"$TEMP_PW" \
    -e "ALTER USER 'root'@'localhost' IDENTIFIED BY '$PASSWORD';"
  touch "$MARKER"; chmod 600 "$MARKER"
  MYSQL=(mysql -uroot -p"$PASSWORD")
fi

say "verifying the settings SCT and the collector depend on"
"${MYSQL[@]}" -N -B -e "
SELECT CONCAT('version          = ', VERSION())
UNION ALL SELECT CONCAT('log_bin          = ', @@log_bin)
UNION ALL SELECT CONCAT('binlog_format    = ', @@binlog_format)
UNION ALL SELECT CONCAT('binlog_row_image = ', @@binlog_row_image)
UNION ALL SELECT CONCAT('lower_case_names = ', @@lower_case_table_names)
UNION ALL SELECT CONCAT('sql_mode         = ', @@sql_mode);"

# ONLY_FULL_GROUP_BY breaks AWS SCT's own load-partitions-by-schema query. The
# cloud-init config already excludes it; this asserts it rather than assuming the
# config was applied, because a silently-ignored my.cnf would surface as an
# unexplained SCT failure 20 minutes later.
if "${MYSQL[@]}" -N -B -e "SELECT @@sql_mode" | grep -q ONLY_FULL_GROUP_BY; then
  echo "FATAL: ONLY_FULL_GROUP_BY is set. AWS SCT's own metadata query is invalid" >&2
  echo "       under it and the assessment will abandon after three retries." >&2
  echo "       Check /etc/my.cnf.d/dbshift.cnf was read: mysqld --verbose --help | grep sql-mode" >&2
  exit 1
fi
echo "ONLY_FULL_GROUP_BY is absent -- SCT can load partition metadata"

# ---------------------------------------------------------------------------
# The estate. Same five scripts the local Docker path runs, in the same order.
# ---------------------------------------------------------------------------
say "00_reset.sql -- dropping and recreating the three schemas"
"${MYSQL[@]}" < "$DIR/00_reset.sql"

say "01_setup_admin.sql -- accounts and grants"
sed -e "s/CHANGE_ME_APP/$PASSWORD/" \
    -e "s/CHANGE_ME_COLL/$PASSWORD/" \
    -e "s/CHANGE_ME_DMS/$PASSWORD/" "$DIR/01_setup_admin.sql" | "${MYSQL[@]}"

for f in 02_schema_objects.sql 03_generate_data.sql 04_seed_defects.sql; do
  say "$f"
  "${MYSQL[@]}" dbmig_mysql_app < "$DIR/$f"
done

say "the estate, counted"
"${MYSQL[@]}" -N -B -e "
SELECT CONCAT('tables     = ', COUNT(*)) FROM information_schema.tables
  WHERE table_schema='dbmig_mysql_app' AND table_type='BASE TABLE'
UNION ALL SELECT CONCAT('views      = ', COUNT(*)) FROM information_schema.views
  WHERE table_schema='dbmig_mysql_app'
UNION ALL SELECT CONCAT('routines   = ', COUNT(*)) FROM information_schema.routines
  WHERE routine_schema='dbmig_mysql_app'
UNION ALL SELECT CONCAT('triggers   = ', COUNT(*)) FROM information_schema.triggers
  WHERE trigger_schema='dbmig_mysql_app'
UNION ALL SELECT CONCAT('events     = ', COUNT(*)) FROM information_schema.events
  WHERE event_schema='dbmig_mysql_app'
UNION ALL SELECT CONCAT('partitions = ', COUNT(*)) FROM information_schema.partitions
  WHERE table_schema='dbmig_mysql_app' AND partition_name IS NOT NULL
UNION ALL SELECT CONCAT('order_line = ', COUNT(*)) FROM dbmig_mysql_app.order_line;"

say "done"
echo "Estate ready. Verify the defects from the operator machine:"
echo "  python scripts/mysql-source/verify_defects.py --dsn <host>:3306/dbmig_mysql_app --user dbmig_collector"
