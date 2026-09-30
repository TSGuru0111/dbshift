"""Build the `.scts` scenario SCT's batch CLI executes.

**The format is SCT's own, not JSON.** `.scts` is a custom language parsed by
ANTLR inside SCT: one command per block, parameters as `-name: 'value'`, each
command terminated by a bare `/` on its own line. A JSON file is rejected with
`AntlrParsingException ... at line 1:0`, which is how the first version of this
module was caught being wrong.

The shape here is taken from **SCT 1.0.677's own `ReportCreationTemplate.scts`**,
obtained by running SCT's `GetCliScenario` command rather than from
documentation or guesswork. That matters: the real template differs from the
documented prose in several places that each break a run.

    SetGlobalSettings   register the JDBC driver -- SCT will not connect without it
    CreateProject       source and target come from the mapping, not from here
    CreateFilter        which schemas are in scope
    AddSource           -vendor / -connectionType / -host / -port / -serviceName
    AddServerMapping    source server -> a **virtual** target platform
    CreateReport        the assessment itself
    SaveReportPDF       -file:      a full file path
    SaveReportCSV       -directory: a directory, not a file
    SaveProject

**The virtual target is the mechanism behind the dropdown.** Assessing needs no
live target database: `Servers.<PostgreSQL (virtual)>` tells SCT to report the
conversion against that platform. Changing the dropdown changes that one string,
which is why each target is a separate SCT run rather than a filter over one
result.

Passwords appear in the file because SCT's CLI has no other way to take them,
which is why `runner.py` writes it to a private directory and deletes it
afterwards. That is a real exposure, handled there rather than hidden here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import engines

from . import targets

# SCT names the source platform by vendor, and the connection parameters differ
# per vendor -- which is why this is a table rather than three constants.
#
# `BASIC_SERVICE_NAME` matches the collector's easy-connect DSN, which carries a
# service name rather than a SID. The alternative, `BASIC_SID`, would silently
# fail against XEPDB1 -- a pluggable database is reached by service name.
#
# MySQL has no service name and no SID: it connects to a host, a port and
# optionally a default database. `database` is the parameter SCT names, and it is
# optional, because the schema filter is what actually scopes the assessment.
#
# `driver_setting` is the key inside SCT's global settings JSON that registers
# the JDBC jar. Without the driver registered, AddSource fails with a driver
# error rather than a connection error -- confusing, and a step people skip.
ORACLE_VENDOR = "ORACLE"
ORACLE_CONNECTION_TYPE = "BASIC_SERVICE_NAME"

SOURCE_VENDORS = {
    engines.ORACLE: {
        "vendor": "ORACLE",
        # The tree-path name SCT knows the source server by. Used by the filter
        # and the server mapping, so it must match AddSource's `name`.
        "source_name": "ORACLE",
        "connection_type": "BASIC_SERVICE_NAME",
        "driver_setting": "oracle_driver_file",
        # The parameter SCT wants the third DSN field in, and whether it is
        # required. Oracle cannot connect without a service name.
        "database_param": "serviceName",
        "database_required": True,
        "dsn_shape": "host:port/service",
        "dsn_example": "localhost:1521/XEPDB1",
    },
    engines.MYSQL: {
        "vendor": "MYSQL",
        "source_name": "MYSQL",
        # **MySQL takes no connectionType at all.** Measured from SCT 1.0.677's
        # own bytecode on 2026-09-29: `MySqlConnectionProperties` declares
        # serverName, port, username, password and useSSL, and -- unlike
        # `OracleConnectionProperties` -- has no `$ConnectionType` inner class.
        #
        # Passing one made SCT fall back to Oracle's property class and fail with
        #   No enum constant OracleConnectionProperties.ConnectionType.BASIC
        # followed by `Not found object(s) for path "Servers.MYSQL"` because
        # AddSource had already failed. The second error is the one that shows in
        # the console, and it looks like a tree-path bug rather than a parameter
        # that should not have been there.
        "connection_type": None,
        "driver_setting": "mysql_driver_file",
        # No database parameter either: the schema filter scopes the assessment,
        # and `MySqlConnectionProperties` has nowhere to put one.
        "database_param": None,
        "database_required": False,
        # The DSN still accepts a database for symmetry with the collector, which
        # does use it -- it is simply not passed to SCT.
        "dsn_shape": "host:port/database",
        "dsn_example": "10.0.1.42:3306/dbmig_mysql_app",
    },
}

# Oracle's, for callers that predate the source-engine flag.
SOURCE_NAME = SOURCE_VENDORS[engines.ORACLE]["source_name"]
FILTER_NAME = "DBShiftScope"


def source_vendor(source_engine: str | None = None) -> dict:
    return SOURCE_VENDORS[engines.normalize(source_engine)]


def split_dsn(dsn: str, source_engine: str | None = None) -> tuple[str, int, str | None]:
    """Split a DSN into the three fields SCT names separately.

    One function for both engines, because the shape is the same -- `host:port`
    plus an optional third field -- and only whether that field is *required*
    differs. Oracle refuses without it; MySQL treats it as a default database and
    the schema filter does the scoping either way.
    """
    spec = source_vendor(source_engine)
    rest, sep, tail = str(dsn or "").partition("/")
    if spec["database_required"] and (not sep or not tail):
        raise ValueError(
            f"DSN {dsn!r} has no {spec['database_param']}; expected "
            f"{spec['dsn_shape']} (for example {spec['dsn_example']})"
        )
    host, sep_port, port = rest.partition(":")
    if not host:
        raise ValueError(f"DSN {dsn!r} names no host; expected {spec['dsn_shape']}")
    if not sep_port or not port.isdigit():
        raise ValueError(
            f"DSN {dsn!r} has no numeric port; expected {spec['dsn_shape']}"
        )
    return host, int(port), (tail or None)


def _oracle_host_port_service(dsn: str) -> tuple[str, int, str]:
    """Oracle's DSN split, kept as its own name for existing callers and tests."""
    host, port, service = split_dsn(dsn, engines.ORACLE)
    return host, port, service


def _quote(value: str) -> str:
    """A single-quoted `.scts` value.

    SCT requires straight single quotes around every parameter value. There is
    no documented escape for a quote *inside* one, so a value containing one is
    refused rather than emitted as a script that would parse into something
    unintended -- a password ending a string early is the case that matters.
    """
    text = str(value)
    if "'" in text:
        raise ValueError(
            "a .scts value cannot contain a single quote; SCT's script format has no "
            "escape for one. Offending value starts: " + text[:12] + "..."
        )
    return f"'{text}'"


def _win(path: Path | str) -> str:
    """A Windows path as SCT expects it in a plain quoted value."""
    return str(path).replace("/", "\\")


def _json_value(obj) -> str:
    r"""An inline JSON parameter, matching SCT's own template byte for byte.

    `SetGlobalSettings -settings:` and `CreateFilter -objects:` take JSON inside
    the quoted value, and SCT's template writes a Windows path as
    `C:\\drivers\\ojdbc8.jar` -- one level of escaping, for the JSON parser that
    reads the string afterwards.

    `json.dumps` already produces that escaping from a single-backslash path, so
    escaping again on top of it yields `C:\\\\drivers` and SCT resolves a
    nonexistent driver path. Build the JSON and leave it alone.
    """
    return _quote(json.dumps(obj, indent=2))


def _command(name: str, params: list[tuple[str, str]]) -> str:
    lines = [name]
    for key, value in params:
        lines.append(f"    -{key}: {value}")
    lines.append("/")
    return "\n".join(lines)


def _source_params(spec, host, port, database, user, password):
    """AddSource's parameters for one vendor.

    The third DSN field lands in a differently-named parameter per engine
    (`serviceName` for Oracle, `database` for MySQL) and is omitted entirely when
    MySQL's DSN named none -- passing an empty value would have SCT try to open a
    database called "", which fails as an authentication error and reads like bad
    credentials.
    """
    params = [
        ("name", _quote(spec["source_name"])),
        ("vendor", _quote(spec["vendor"])),
    ]
    # Oracle names a connection type (BASIC_SERVICE_NAME vs BASIC_SID); MySQL's
    # property class has no such concept, and passing one sends SCT down Oracle's
    # code path. Omitted rather than defaulted.
    if spec["connection_type"]:
        params.append(("connectionType", _quote(spec["connection_type"])))
    params.append(("host", _quote(host)))
    params.append(("port", _quote(str(port))))
    # Oracle needs the service name; MySQL has nowhere to put a database.
    if spec["database_param"] and database:
        params.append((spec["database_param"], _quote(database)))
    params.append(("user", _quote(user)))
    params.append(("password", _quote(password)))
    return params


def build_script(
    *,
    project_name: str,
    project_dir: Path,
    report_dir: Path,
    log_dir: Path,
    target_id: str,
    dsn: str,
    user: str,
    password: str,
    schemas: list[str],
    jdbc_jar: Path,
    source_engine: str | None = None,
) -> str:
    """The scenario as `.scts` text, ready to write.

    `source_engine` defaults to Oracle, so every existing caller produces exactly
    the script it produced before this parameter existed.
    """
    spec = source_vendor(source_engine)
    target = targets.get(target_id, source_engine)
    host, port, database = split_dsn(dsn, source_engine)
    source_name = spec["source_name"]

    if not schemas:
        raise ValueError("at least one schema must be in scope")

    pdf_path = Path(report_dir) / f"{project_name}.pdf"

    blocks = [
        # Without the driver registered, AddSource fails with a driver error
        # rather than a connection error -- confusing, and a step people skip.
        _command("SetGlobalSettings", [
            ("save", _quote("false")),
            ("settings", _json_value({spec["driver_setting"]: _win(jdbc_jar)})),
        ]),
        _command("SetGlobalSettings", [
            ("save", _quote("false")),
            ("settings", _json_value({"console_log_folder": _win(log_dir)})),
        ]),
        _command("CreateProject", [
            ("name", _quote(project_name)),
            ("directory", _quote(_win(project_dir))),
        ]),
        # Scope by schema. Without a filter SCT reports the whole instance,
        # burying the client's objects under Oracle's own -- the same mistake
        # assess/loader.py guards against with v_user_objects.
        _command("CreateFilter", [
            ("name", _quote(FILTER_NAME)),
            ("origin", _quote("source")),
            ("objects", _json_value([{
                "type": "include",
                "treePath": f"Servers.{source_name}.Schemas.%",
                "name": list(schemas),
            }])),
        ]),
        _command("AddSource", _source_params(spec, host, port, database, user, password)),
        # The virtual target: this one string is what the dropdown changes.
        _command("AddServerMapping", [
            ("sourceTreePath", _quote(f"Servers.{source_name}")),
            ("targetTreePath", _quote(target["sct_virtual_target"])),
        ]),
        _command("CreateReport", [
            ("filter", _quote(FILTER_NAME)),
        ]),
        # PDF takes a file, CSV takes a directory. Not symmetrical, and getting
        # it the wrong way round is an error at save time.
        _command("SaveReportPDF", [
            ("file", _quote(_win(pdf_path))),
        ]),
        _command("SaveReportCSV", [
            ("directory", _quote(_win(report_dir))),
        ]),
        _command("SaveProject", []),
    ]

    return "\n\n".join(blocks) + "\n"


# The password is the only secret in the script, and it is always the value of
# `-password:`. Redaction targets that parameter rather than searching for the
# secret's text, so it cannot be defeated by a password that looks like a path.
_PASSWORD_LINE = re.compile(r"^(\s*-password:\s*)'.*'$", re.MULTILINE)


def redacted(script: str) -> str:
    """The same script with the password masked, safe to record or log."""
    return _PASSWORD_LINE.sub(r"\1'***'", script)


def command_names(script: str) -> list[str]:
    """The commands a script will run, in order -- for `plan()` and the console.

    Parsed back out of the generated text rather than tracked separately, so the
    plan cannot drift from what would actually be executed.

    A command is a bare identifier at the start of a line. The guard matters
    because an inline JSON value spans lines and its closing `}'` or `]'` also
    sits in column zero -- matching on the identifier shape rather than on
    indentation keeps those out.
    """
    return [
        line.strip()
        for line in script.splitlines()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", line.strip() or "_/")
    ]


def write(script: str, path: Path) -> Path:
    """Write the `.scts` file. The caller is responsible for deleting it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # SCT reads the script as UTF-8; the JVM is launched with -Dfile.encoding=UTF-8.
    path.write_text(script, encoding="utf-8")
    return path
