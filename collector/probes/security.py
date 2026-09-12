from ..db import in_binds

NAME = "security"

# Datasets whose feeding query is labelled differently from the dataset name.
# Consulted only when the dataset comes back empty, to recover its column list.
QUERY_LABELS = {
    "source_inventory.role_privileges": "security.role_privs",
    "source_inventory.system_privileges": "security.sys_privs",
    "source_inventory.table_privileges": "security.tab_privs",
}


def collect(s, owners):
    frag, binds = in_binds("o", owners)
    users = s.fetch(
        "security.users",
        """SELECT username, account_status, default_tablespace, temporary_tablespace,
                  profile, authentication_type, oracle_maintained, common, created,
                  lock_date, expiry_date
           FROM dba_users
           ORDER BY username""",
    )
    roles = s.fetch(
        "security.roles",
        """SELECT role, authentication_type, oracle_maintained, common
           FROM dba_roles ORDER BY role""",
    )
    role_privs = s.fetch(
        "security.role_privs",
        f"""SELECT grantee, granted_role, admin_option, default_role
            FROM dba_role_privs WHERE grantee IN ({frag})
            ORDER BY grantee, granted_role""",
        binds,
    )
    sys_privs = s.fetch(
        "security.sys_privs",
        f"""SELECT grantee, privilege, admin_option
            FROM dba_sys_privs WHERE grantee IN ({frag})
            ORDER BY grantee, privilege""",
        binds,
    )
    tab_privs = s.fetch(
        "security.tab_privs",
        f"""SELECT grantee, owner, table_name, grantor, privilege, grantable
            FROM dba_tab_privs
            WHERE grantee IN ({frag}) OR owner IN ({frag})
            ORDER BY grantee, owner, table_name, privilege""",
        binds,
    )
    profiles = s.fetch(
        "security.profiles",
        """SELECT profile, resource_name, resource_type, limit
           FROM dba_profiles ORDER BY profile, resource_name""",
    )
    return {
        "source_inventory.users": users,
        "source_inventory.roles": roles,
        "source_inventory.role_privileges": role_privs,
        "source_inventory.system_privileges": sys_privs,
        "source_inventory.table_privileges": tab_privs,
        "source_inventory.profiles": profiles,
    }
