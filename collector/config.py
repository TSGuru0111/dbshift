from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_USER = "dbmig_collector"
DEFAULT_DSN = "localhost:1521/XEPDB1"
DEFAULT_SCHEMAS = ("DBMIG_APP", "DBMIG_RPT", "DBMIG_COLLECTOR")

PASSWORD_ENV = "DBSHIFT_COLLECTOR_PASSWORD"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    user: str
    password: str
    dsn: str
    schemas: tuple[str, ...]
    output_dir: Path

    def redacted(self) -> dict:
        return {"user": self.user, "dsn": self.dsn, "schemas": list(self.schemas)}


def _schemas_from_env() -> tuple[str, ...]:
    raw = os.environ.get("DBSHIFT_SCHEMAS")
    if not raw:
        return DEFAULT_SCHEMAS
    names = tuple(n.strip().upper() for n in raw.split(",") if n.strip())
    if not names:
        raise ConfigError("DBSHIFT_SCHEMAS was set but parsed to an empty list.")
    for n in names:
        if not n.replace("_", "").replace("$", "").isalnum():
            raise ConfigError(f"Refusing schema name with unexpected characters: {n!r}")
    return names


def load(output_dir: Path | None = None) -> Config:
    password = os.environ.get(PASSWORD_ENV)
    if not password:
        raise ConfigError(
            f"{PASSWORD_ENV} is not set. Export the read-only collector password, "
            f"e.g.  $env:{PASSWORD_ENV}='...'   (never commit it to a file)"
        )
    return Config(
        user=os.environ.get("DBSHIFT_COLLECTOR_USER", DEFAULT_USER),
        password=password,
        dsn=os.environ.get("DBSHIFT_DSN", DEFAULT_DSN),
        schemas=_schemas_from_env(),
        output_dir=output_dir or Path(__file__).resolve().parent / "output",
    )
