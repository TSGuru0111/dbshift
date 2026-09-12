"""Confirm all 14 seeded defects are actually present in DBMIG_TELCO.

The assessment engine is about to be scored on finding these. If one was not
really seeded, a "miss" would be blamed on the engine rather than the seed
script -- which is exactly what happened with DBMIG_APP's defect 7.
"""
import oracledb

conn = oracledb.connect(
    user="dbmig_telco", password="DbMig2026Telco", dsn="localhost:1521/XEPDB1"
)
cur = conn.cursor()


def q(sql):
    return cur.execute(sql).fetchone()[0]


checks = [
    (1, "Reserved-word table/columns", q(
        "select count(*) from user_tab_columns where table_name='SESSION' "
        "and column_name in ('COMMENT','LEVEL','DATE')"), 3, "columns"),
    (1, "  table SESSION exists", q(
        "select count(*) from user_tables where table_name='SESSION'"), 1, "table"),
    (2, "No PK on usage_staging", q(
        "select count(*) from user_constraints where table_name='USAGE_STAGING' "
        "and constraint_type='P'"), 0, "PKs (want 0)"),
    (2, "  usage_staging volume", q("select count(*) from usage_staging"), 250000, "rows"),
    (2, "  usage_staging indexes", q(
        "select count(*) from user_indexes where table_name='USAGE_STAGING'"), 0,
     "indexes (want 0)"),
    (3, "Unindexed FK on support_ticket", q(
        "select count(*) from user_constraints where constraint_name='FK_TICKET_CELL'"),
     1, "constraint"),
    (4, "Orphaned invoices", q(
        "select count(*) from invoice i where not exists "
        "(select 1 from subscriber s where s.subscriber_id=i.subscriber_id)"), 40,
     "orphans"),
    (4, "  fk ENABLE NOVALIDATE", q(
        "select count(*) from user_constraints where constraint_name='FK_INVOICE_SUB' "
        "and status='ENABLED' and validated='NOT VALIDATED'"), 1, "constraint"),
    (5, "Duplicate national_id", q(
        "select count(*) from subscriber where national_id='000000001'"), 500, "rows"),
    # data_scale IS NULL, not data_precision: a bare FLOAT reports
    # data_precision = 126 (Oracle's binary default) even though it carries no
    # user-declared precision, so a data_precision test misses it.
    (6, "Unconstrained NUMBER/FLOAT", q(
        "select count(*) from user_tab_columns where table_name='SUBSCRIBER' "
        "and column_name in ('LOYALTY_POINTS','RISK_FACTOR') "
        "and data_scale is null"), 2, "columns"),
    (7, "Non-ASCII in full_name", q(
        "select count(*) from subscriber where ASCIISTR(full_name) != full_name"), 3,
     "rows"),
    (8, "Invalid object", q(
        "select count(*) from user_objects where object_name='SP_RATE_CDR_BROKEN' "
        "and status='INVALID'"), 1, "object"),
    (8, "  stored compile errors", q(
        "select count(*) from user_errors where name='SP_RATE_CDR_BROKEN'"), None,
     "error rows (want > 0)"),
    (9, "Unusable index", q(
        "select count(*) from user_indexes where index_name='IX_PAYMENT_REF' "
        "and status='UNUSABLE'"), 1, "index"),
    (9, "  invisible index", q(
        "select count(*) from user_indexes where index_name='IX_INVOICE_STATUS' "
        "and visibility='INVISIBLE'"), 1, "index"),
    (10, "Disabled constraint", q(
        "select count(*) from user_constraints where constraint_name='CK_DEVICE_STAT' "
        "and status='DISABLED'"), 1, "constraint"),
    (10, "  violating row present", q(
        "select count(*) from device where device_status='ZZ_INVALID'"), 1, "row"),
    (11, "All-NULL column", q(
        "select count(legacy_tariff_ref) from invoice_line"), 0, "non-nulls (want 0)"),
    (12, "Byte-semantics columns", q(
        "select count(*) from user_tab_columns where table_name='SUPPORT_TICKET' "
        "and column_name in ('AGENT_NOTE','AGENT_LOCALE') and char_used='B'"), 2,
     "columns"),
    (13, "Redundant index", q(
        "select count(*) from user_indexes where index_name in "
        "('IX_DEVICE_SUB','IX_DEVICE_SUB_STATUS')"), 2, "indexes"),
    (14, "Sensitive columns", q(
        "select count(*) from user_tab_columns where table_name='SUBSCRIBER' and "
        "column_name in ('BANK_ACCOUNT_NO','CREDIT_CARD_HASH','PASSPORT_NUMBER')"), 3,
     "columns"),
]

print(f"{'#':<4}{'check':<36}{'actual':>9}  {'expected':<22}{'':<6}")
print("-" * 82)
bad = 0
for num, name, actual, expected, unit in checks:
    if expected is None:
        ok = actual > 0
        exp_s = f"> 0 {unit}"
    else:
        ok = actual == expected
        exp_s = f"{expected} {unit}"
    if not ok:
        bad += 1
    print(f"{num:<4}{name:<36}{actual:>9}  {exp_s:<22}{'OK' if ok else 'FAIL'}")

print("-" * 82)
print(f"{len(checks) - bad}/{len(checks)} checks passed")

# Regression check: exactly one INVALID object.
inv = cur.execute(
    "select object_name, object_type from user_objects where status='INVALID'"
).fetchall()
print(f"\nINVALID objects ({len(inv)}, want exactly 1):")
for o in inv:
    print("  ", o)

print("\nestate")
print("  size GB    :", round(q(
    "select nvl(sum(bytes),0)/1024/1024/1024 from user_segments"), 3))
print("  objects    :", q("select count(*) from user_objects"))
print("  constraints:", q("select count(*) from user_constraints"))
print("  tables     :", q("select count(*) from user_tables"))

conn.close()
raise SystemExit(1 if bad else 0)
