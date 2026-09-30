from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import engines
from engines import spec as engine_spec

from . import mode as migration_mode

DEFAULT_USER = "dbmig_collector"
ENGINE_ENV = engines.ENV
DEFAULT_DSN = "localhost:1521/XEPDB1"
# MySQL names a database, not a service. localhost so a laptop-local MySQL
# works with no configuration, matching how the Oracle default behaves.
DEFAULT_MYSQL_DSN = "localhost:3306"
DEFAULT_SCHEMAS = ("DBMIG_APP", "DBMIG_RPT", "DBMIG_COLLECTOR")

PASSWORD_ENV = "DBSHIFT_COLLECTOR_PASSWORD"
MODE_ENV = "DBSHIFT_MIGRATION_MODE"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    user: str
    password: str
    dsn: str
    schemas: tuple[str, ...]
    output_dir: Path
    # Which engine the estate runs. Oracle unless declared, so every script and
    # docs example that predates the flag behaves exactly as it did.
    source_engine: str = engines.DEFAULT
    # Full load, or full load plus CDC. Decided here in Phase 1 because it
    # changes which findings are blockers -- see collector/mode.py.
    migration_mode: str = migration_mode.DEFAULT
    # False when nobody said, and the default applied. A declared full load is a
    # decision; an undeclared one is an assumption, and the record says which.
    mode_declared: bool = False
    mode_chosen_by: str | None = None

    def redacted(self) -> dict:
        return {
            "user": self.user,
            "dsn": self.dsn,
            "schemas": list(self.schemas),
            "migration_mode": self.migration_mode,
            "source_engine": self.source_engine,
        }


def _schemas_from_env(source_engine: str = engines.DEFAULT) -> tuple[str, ...]:
    """The schemas to collect, cased the way the engine actually stores them.

    **Oracle folds unquoted identifiers up, and MySQL does not.** On Oracle,
    upper-casing a configured name is free: the engine already did it, so
    `hr` and `HR` name the same schema. On MySQL a schema is a directory on
    disk, so on Linux `Sales` and `SALES` are two different databases -- and
    upper-casing here would send the collector looking for one that does not
    exist. It would find nothing, report nothing missing that a reader would
    notice, and produce an empty run that looks like an empty estate.

    That failure mode is not hypothetical for this project: a hardcoded schema
    list once made the console collect nothing on anyone else's database, and
    the fix was the same shape -- stop assuming, ask the engine.
    """
    raw = os.environ.get("DBSHIFT_SCHEMAS")
    if not raw:
        return DEFAULT_SCHEMAS
    fold = engine_spec.spec(source_engine)["fold_schema_names"]
    names = tuple(
        (n.strip().upper() if fold else n.strip())
        for n in raw.split(",") if n.strip()
    )
    if not names:
        raise ConfigError("DBSHIFT_SCHEMAS was set but parsed to an empty list.")
    for n in names:
        if not n.replace("_", "").replace("$", "").isalnum():
            raise ConfigError(f"Refusing schema name with unexpected characters: {n!r}")
    return names


def _engine_from_env() -> str:
    """The declared source engine, or Oracle."""
    try:
        return engines.normalize(os.environ.get(ENGINE_ENV))
    except engines.EngineError as exc:
        raise ConfigError(f"{ENGINE_ENV}: {exc}") from exc


def _mode_from_env() -> tuple[str, bool]:
    """(mode, declared). Unset means the default applied and nobody chose it."""
    raw = os.environ.get(MODE_ENV)
    if not raw or not raw.strip():
        return migration_mode.DEFAULT, False
    try:
        return migration_mode.normalize(raw), True
    except migration_mode.ModeError as exc:
        raise ConfigError(f"{MODE_ENV}: {exc}") from exc


def load(output_dir: Path | None = None) -> Config:
    password = os.environ.get(PASSWORD_ENV)
    if not password:
        raise ConfigError(
            f"{PASSWORD_ENV} is not set. Export the read-only collector password, "
            f"e.g.  $env:{PASSWORD_ENV}='...'   (never commit it to a file)"
        )
    mode, declared = _mode_from_env()
    source_engine = _engine_from_env()
    # The DSN default follows the engine: 1521/XEPDB1 means nothing to MySQL,
    # and a caller who declared MYSQL but not a DSN should get a MySQL-shaped
    # default rather than an Oracle one that cannot parse.
    default_dsn = DEFAULT_DSN if engines.is_oracle(source_engine) else DEFAULT_MYSQL_DSN
    return Config(
        user=os.environ.get("DBSHIFT_COLLECTOR_USER", DEFAULT_USER),
        password=password,
        dsn=os.environ.get("DBSHIFT_DSN", default_dsn),
        schemas=_schemas_from_env(source_engine),
        source_engine=source_engine,
        output_dir=output_dir or Path(__file__).resolve().parent / "output",
        migration_mode=mode,
        mode_declared=declared,
        mode_chosen_by=os.environ.get("DBSHIFT_MIGRATION_MODE_BY") or None,
    )
