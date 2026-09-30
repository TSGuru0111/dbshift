"""Accounts and grants -- MySQL's answer to `probes/security.py`.

MySQL's privilege model is not Oracle's. There are no roles before 8.0, no
profiles, and privileges live in five `mysql.*_priv` tables rather than in
`dba_sys_privs` / `dba_role_privs`. What the assessment actually asks of this
data is the same on both engines, though: who can log in, who holds something
sweeping, and who can read data they should not.

So the Oracle dataset names are kept (`users`, `system_privileges`,
`table_privileges`, `role_privileges`) and filled from MySQL's equivalents. Where
MySQL genuinely has no analogue -- a profile, a password-expiry policy expressed
Oracle's way -- the dataset is shaped and empty rather than invented.

**Read from information_schema, not mysql.user.** `information_schema.user_privileges`
is readable with the grants the collector already needs; `mysql.user` requires
SELECT on the `mysql` schema, which is a wider grant than a read-only profiling
account should hold. Same reasoning as the Oracle path's refusal to rely on
SELECT_CATALOG_ROLE for row data.
"""

NAME = "security"


def collect(s, owners):
    frag, binds = s.binds("o", owners)

    # Every account, and whether it can reach the estate from anywhere.
    users = s.fetch(
        "security.users",
        """SELECT DISTINCT grantee AS username,
                  'OPEN'          AS account_status,
                  NULL            AS lock_date,
                  NULL            AS expiry_date,
                  NULL            AS default_tablespace,
                  NULL            AS profile,
                  NULL            AS created,
                  CASE WHEN grantee LIKE '%@\\'%\\'%'
                       THEN SUBSTRING_INDEX(REPLACE(grantee, '\\'', ''), '@', -1)
                       ELSE NULL END AS host
           FROM information_schema.user_privileges
           ORDER BY grantee""",
    )
    # A host of '%' means the account may connect from any address. On a source
    # being migrated that is worth reporting: the same grant on an RDS target is
    # reachable from the whole VPC.
    for u in users:
        u["wildcard_host"] = "YES" if (u.get("host") or "") == "%" else "NO"

    # Server-wide privileges -- the closest thing to dba_sys_privs. SUPER,
    # FILE, PROCESS and SHUTDOWN are the ones that do not exist on RDS at all,
    # so a routine or application depending on them breaks after migration.
    system_privileges = s.fetch(
        "security.system_privileges",
        """SELECT grantee, privilege_type AS privilege, is_grantable
           FROM information_schema.user_privileges
           ORDER BY grantee, privilege_type""",
    )

    table_privileges = s.fetch(
        "security.table_privileges",
        # Aliased to Oracle's dba_tab_privs columns. When the result is EMPTY the
        # dataset's columns come from these aliases, not from conform() -- which
        # has no rows to pad -- so the names have to be right here.
        f"""SELECT grantee, table_schema AS owner, table_name,
                   NULL AS grantor,
                   privilege_type AS privilege, is_grantable AS grantable
            FROM information_schema.table_privileges
            WHERE table_schema IN ({frag})
            ORDER BY table_schema, table_name, grantee""",
        binds,
    )

    schema_privileges = s.fetch(
        "security.schema_privileges",
        f"""SELECT grantee, table_schema AS owner,
                   privilege_type AS privilege, is_grantable
            FROM information_schema.schema_privileges
            WHERE table_schema IN ({frag})
            ORDER BY table_schema, grantee""",
        binds,
    )

    # Role grants. `mysql.role_edges` holds them, but reading it needs SELECT on
    # the `mysql` schema -- which this collector deliberately does NOT have,
    # because that grant also exposes every password hash in `mysql.user`.
    #
    # Measured 2026-09-28 against MySQL 8.0.46: the query returns
    # "SELECT command denied ... for table 'role_edges'", which `Session.fetch`
    # logs as a failed query. That is a *false* failure -- nothing is wrong, the
    # collector simply is not entitled to this table -- and a failed query in the
    # manifest is a signal an operator should be able to trust.
    #
    # `information_schema.applicable_roles` answers the same question (which roles
    # does an account hold) for the roles visible to the current user, and needs
    # no extra grant. It reports less than role_edges on a server with many
    # accounts, and that is the honest trade: fewer rows with no privilege
    # escalation, rather than a wider grant to populate a dataset one rule reads.
    role_privileges = s.fetch(
        "security.role_privileges",
        """SELECT grantee, role_name AS granted_role,
                  is_grantable AS admin_option, is_default AS default_role,
                  role_host AS from_host, role_host AS to_host
           FROM information_schema.applicable_roles
           ORDER BY grantee, role_name""",
    )

    return {
        "source_inventory.users": users,
        "source_inventory.system_privileges": system_privileges,
        "source_inventory.table_privileges": table_privileges,
        "source_inventory.mysql_schema_privileges": schema_privileges,
        "source_inventory.role_privileges": role_privileges,
        # No Oracle-style roles catalogue and no profiles. Shaped and empty.
        "source_inventory.roles": [],
    }
