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

from . import targets

# SCT names the source platform by vendor. Oracle is the only source in scope.
ORACLE_VENDOR = "ORACLE"

# `BASIC_SERVICE_NAME` matches the collector's easy-connect DSN, which carries a
# service name rather than a SID. The alternative, `BASIC_SID`, would silently
# fail against XEPDB1 -- a pluggable database is reached by service name.
ORACLE_CONNECTION_TYPE = "BASIC_SERVICE_NAME"

SOURCE_NAME = "ORACLE"
FILTER_NAME = "DBShiftScope"


def _oracle_host_port_service(dsn: str) -> tuple[str, int, str]:
    """Split `host:port/service` into the three fields SCT names separately."""
    rest, sep, service = dsn.partition("/")
    if not sep or not service:
        raise ValueError(
            f"DSN {dsn!r} has no service name; expected host:port/service "
            "(for example localhost:1521/XEPDB1)"
        )
    host, sep, port = rest.partition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"DSN {dsn!r} has no numeric port; expected host:port/service")
    return host, int(port), service


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
) -> str:
    """The scenario as `.scts` text, ready to write."""
    target = targets.get(target_id)
    host, port, service = _oracle_host_port_service(dsn)

    if not schemas:
        raise ValueError("at least one schema must be in scope")

    pdf_path = Path(report_dir) / f"{project_name}.pdf"

    blocks = [
        # Without the driver registered, AddSource fails with a driver error
        # rather than a connection error -- confusing, and a step people skip.
        _command("SetGlobalSettings", [
            ("save", _quote("false")),
            ("settings", _json_value({"oracle_driver_file": _win(jdbc_jar)})),
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
                "treePath": f"Servers.{SOURCE_NAME}.Schemas.%",
                "name": list(schemas),
            }])),
        ]),
        _command("AddSource", [
            ("name", _quote(SOURCE_NAME)),
            ("vendor", _quote(ORACLE_VENDOR)),
            ("connectionType", _quote(ORACLE_CONNECTION_TYPE)),
            ("host", _quote(host)),
            ("port", _quote(str(port))),
            ("serviceName", _quote(service)),
            ("user", _quote(user)),
            ("password", _quote(password)),
        ]),
        # The virtual target: this one string is what the dropdown changes.
        _command("AddServerMapping", [
            ("sourceTreePath", _quote(f"Servers.{SOURCE_NAME}")),
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
