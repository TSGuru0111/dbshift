"""Where SCT, its JVM and the JDBC driver are, and whether each is actually usable.

Three prerequisites, all manual downloads, none of them installable by this
project: Amazon Corretto 11 (SCT's CLI requires 11 specifically), AWS SCT
itself, and an Oracle JDBC driver.

**Every check reports which one is missing and how to get it.** The failure this
guards against is not "SCT is absent" -- that is obvious -- it is SCT present
with the wrong JVM, or present with no JDBC driver registered, which fails deep
inside a batch run with a Java stack trace that says nothing a client could act
on.

Paths are resolved in this order, and the order matters: an explicit environment
variable always wins, so a customer install in a non-default location needs no
code change.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

OK, MISSING, UNUSABLE = "ok", "missing", "unusable"

# **SCT ships its own JVM, and it is the one to use.** Established by reading
# 1.0.677's own `RunSCTBatch.cmd`, which defaults to
# `<install>/runtime/bin/java.exe` -- Corretto **17**, not 11. The published CLI
# reference says "install Corretto 11", which is what an operator needs when
# running the GUI installer; it is not what the batch wrapper actually invokes.
#
# So a standalone JVM is a *fallback*, and the accepted majors are both: 17
# because that is what SCT 1.0.677 is built with (`Build-Jdk: 17.0.15` in the
# batch jar's manifest), 11 because the documentation asks for it and an older
# SCT may need it.
ACCEPTED_JAVA_MAJORS = (17, 11)
REQUIRED_JAVA_MAJOR = ACCEPTED_JAVA_MAJORS[0]

ENV_SCT_HOME = "DBSHIFT_SCT_HOME"
ENV_JAVA_HOME = "DBSHIFT_SCT_JAVA_HOME"
ENV_JDBC = "DBSHIFT_ORACLE_JDBC_JAR"

# Where the no-elevation install this project uses puts things, and where the
# MSI installer puts them. Both are searched so either install works.
_LOCAL_TOOLS = Path(os.environ.get("LOCALAPPDATA", "")) / "dbshift-tools"

_SCT_CANDIDATES = [
    Path(r"C:\Program Files\AWS Schema Conversion Tool"),
    Path(r"C:\Program Files (x86)\AWS Schema Conversion Tool"),
    _LOCAL_TOOLS / "AWS Schema Conversion Tool",
    _LOCAL_TOOLS / "sct",
]

_JDBC_CANDIDATES = [
    _LOCAL_TOOLS / "jdbc" / "ojdbc8.jar",
    _LOCAL_TOOLS / "jdbc" / "ojdbc11.jar",
]


@dataclass
class Check:
    name: str
    status: str
    detail: str
    remedy: str | None = None
    path: str | None = None


@dataclass
class Toolchain:
    java: Path | None = None
    sct_batch_jar: Path | None = None
    sct_home: Path | None = None
    jdbc_jar: Path | None = None
    checks: list[Check] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return all(c.status == OK for c in self.checks)

    def as_dict(self) -> dict:
        return {
            "ready": self.ready,
            "java": str(self.java) if self.java else None,
            "sct_home": str(self.sct_home) if self.sct_home else None,
            "sct_batch_jar": str(self.sct_batch_jar) if self.sct_batch_jar else None,
            "jdbc_jar": str(self.jdbc_jar) if self.jdbc_jar else None,
            "checks": [vars(c) for c in self.checks],
        }


def _java_major(java_exe: Path) -> int | None:
    """The JVM's major version, by asking it rather than parsing its path.

    A directory named `jdk11...` holding a 17 JVM is exactly the kind of thing
    that produces an unreadable failure two hours later.
    """
    try:
        proc = subprocess.run(
            [str(java_exe), "-version"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # `java -version` writes to stderr on every JVM this project will meet.
    text = (proc.stderr or "") + (proc.stdout or "")
    m = re.search(r'version "(\d+)(?:\.(\d+))?', text)
    if not m:
        return None
    major = int(m.group(1))
    # 1.8 style versioning: the major is the second component.
    if major == 1 and m.group(2):
        return int(m.group(2))
    return major


def _find_java(sct_home: Path | None = None) -> tuple[Path | None, Check]:
    """The JVM to run SCT with. **SCT's own bundled runtime wins.**

    Order: an explicit override, then SCT's embedded JVM (what `RunSCTBatch.cmd`
    itself uses), then a standalone Corretto as a fallback.
    """
    candidates: list[tuple[Path, str]] = []

    explicit = os.environ.get(ENV_JAVA_HOME)
    if explicit:
        candidates.append((Path(explicit) / "bin" / "java.exe", f"{ENV_JAVA_HOME} override"))

    # SCT's embedded runtime. This is what the vendor's own wrapper invokes, so
    # it is the JVM SCT is tested against -- prefer it over anything installed.
    if sct_home:
        for rt in sorted(Path(sct_home).rglob("runtime/bin/java.exe")):
            candidates.append((rt, "SCT's embedded runtime"))

    for root, pattern in ((_LOCAL_TOOLS, "jdk*"),
                          (Path(r"C:\Program Files\Amazon Corretto"), "jdk*")):
        if root.is_dir():
            for p in sorted(root.glob(pattern), reverse=True):
                if p.is_dir():
                    candidates.append((p / "bin" / "java.exe", "installed Corretto"))

    tried = []
    for exe, origin in candidates:
        if not exe.is_file():
            continue
        major = _java_major(exe)
        tried.append(f"{exe} (major {major}, {origin})")
        if major in ACCEPTED_JAVA_MAJORS:
            return exe, Check(
                "java_runtime", OK,
                f"Java {major} -- {origin} -- {exe}", path=str(exe),
            )

    accepted = " or ".join(str(m) for m in ACCEPTED_JAVA_MAJORS)
    if tried:
        return None, Check(
            "java_runtime", UNUSABLE,
            f"a JVM was found but none is major {accepted}: " + "; ".join(tried),
            f"Install Amazon Corretto {REQUIRED_JAVA_MAJOR}, or set {ENV_JAVA_HOME}. "
            "Normally unnecessary: SCT ships its own runtime and that is preferred.",
        )
    return None, Check(
        "java_runtime", MISSING,
        f"no Java {accepted} found, and SCT's embedded runtime was not located",
        "Installing AWS SCT normally supplies the runtime. Otherwise install Amazon "
        f"Corretto {REQUIRED_JAVA_MAJOR} (winget: Amazon.Corretto.11.JDK, or the portable "
        f"zip from corretto.aws), or set {ENV_JAVA_HOME} to its home directory.",
    )


def _find_sct() -> tuple[Path | None, Path | None, Check]:
    """SCT's batch jar. `app/AWSSchemaConversionToolBatch.jar` in a normal install."""
    explicit = os.environ.get(ENV_SCT_HOME)
    roots = ([Path(explicit)] if explicit else []) + _SCT_CANDIDATES

    searched = []
    for root in roots:
        if not root.is_dir():
            searched.append(f"{root} (absent)")
            continue
        searched.append(str(root))
        # Normal install is app/, but the zip distribution nests a version dir.
        for jar in root.rglob("AWSSchemaConversionToolBatch.jar"):
            return jar, root, Check(
                "sct_installed", OK, f"SCT batch jar at {jar}", path=str(jar),
            )

    return None, None, Check(
        "sct_installed", MISSING,
        "AWSSchemaConversionToolBatch.jar not found. Searched: " + "; ".join(searched),
        "Install AWS Schema Conversion Tool (the Windows installer from "
        "s3.amazonaws.com/publicsctdownload), or set "
        f"{ENV_SCT_HOME} to the install directory.",
    )


def _find_jdbc() -> tuple[Path | None, Check]:
    explicit = os.environ.get(ENV_JDBC)
    candidates = ([Path(explicit)] if explicit else []) + _JDBC_CANDIDATES

    for jar in candidates:
        if jar.is_file():
            return jar, Check(
                "oracle_jdbc_driver", OK, f"Oracle JDBC driver at {jar}", path=str(jar),
            )

    return None, Check(
        "oracle_jdbc_driver", MISSING,
        "no ojdbc jar found in " + "; ".join(str(c) for c in candidates),
        "Download ojdbc8.jar (Maven Central: com/oracle/database/jdbc/ojdbc8) and set "
        f"{ENV_JDBC} to it, or place it in %LOCALAPPDATA%\\dbshift-tools\\jdbc\\.",
    )


def discover() -> Toolchain:
    """Find all three prerequisites. Read-only; installs nothing.

    SCT is located **first**, because its bundled runtime is the JVM to prefer
    and cannot be looked for until the install directory is known.
    """
    tc = Toolchain()
    tc.sct_batch_jar, tc.sct_home, sct_check = _find_sct()
    tc.java, java_check = _find_java(tc.sct_home)
    tc.jdbc_jar, jdbc_check = _find_jdbc()
    tc.checks = [java_check, sct_check, jdbc_check]
    return tc


def summary_lines(tc: Toolchain) -> list[str]:
    """One line per prerequisite, for a CLI or the console's preflight panel."""
    mark = {OK: "[ok]", MISSING: "[--]", UNUSABLE: "[XX]"}
    out = []
    for c in tc.checks:
        out.append(f"  {mark.get(c.status, '[??]')} {c.name}: {c.detail}")
        if c.remedy and c.status != OK:
            out.append(f"       -> {c.remedy}")
    return out
