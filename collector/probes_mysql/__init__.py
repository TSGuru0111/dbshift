"""MySQL discovery probes.

One module per subject, each exporting `NAME` and `collect(s, owners)` and
returning `{"source_inventory.<dataset>": rows}` -- the same contract as
`collector/probes/`. The `NAME`s deliberately match Oracle's, so the console's
probe toggles, `PROBE_DESCRIPTIONS` and the `--enabled-probes` selection work
unchanged on either engine.

There are fewer modules than Oracle's twelve because MySQL has less to ask
about: no tablespaces, no feature-usage licensing catalogue, no AWR. The
datasets those probes produced are still emitted -- shaped and empty, from
`cdc.py` -- because `assess/loader.py` builds a SQLite table per dataset and a
rule referencing a missing one fails with "no such column" three phases later.
That is why `selftest_probes_mysql` checks dataset parity directly instead of
trusting it.
"""

from . import (
    cdc,
    constraints,
    dataprofile,
    identity,
    indexes,
    objects,
    routines,
    security,
    storage,
    tables,
)

# Order matters the way it does for Oracle: identity first so the run has its
# facts before anything else, dataprofile last because it is the only probe
# whose cost scales with the data and the only one that reads user rows.
PROBES = (
    identity,
    objects,
    tables,
    indexes,
    constraints,
    storage,
    routines,
    security,
    cdc,
    dataprofile,
)


# ---------------------------------------------------------------------------
# Column conformance.
#
# Dataset NAMES matching is not enough, and that was learned the expensive way on
# 2026-09-29: a diff of a real Oracle run against a real MySQL run found 15
# datasets where the MySQL rows carried FEWER columns than Oracle's. Most were
# Oracle-only attributes nothing reads on this path, but `feature_usage` broke
# Phase 3 (`last_usage_date` selected by name) and `plsql_source` had the wrong
# shape entirely. The parity selftest passed both, because it compared names.
#
# So every MySQL dataset is padded to Oracle's column set before it is written:
# a column MySQL has no value for is present and NULL. That makes parity a
# property of the pipeline rather than of each probe author remembering, and a
# reader selecting an Oracle column by name gets NULL instead of "no such column".
# MySQL-only columns are kept -- conformance adds, it never removes.

import json as _json
from pathlib import Path as _Path

_REFERENCE = _Path(__file__).resolve().parent / "oracle_columns.json"
_cache: dict | None = None


def oracle_columns() -> dict:
    """{dataset: [columns]} captured from a real Oracle run."""
    global _cache
    if _cache is None:
        try:
            _cache = _json.loads(_REFERENCE.read_text(encoding="utf-8"))["datasets"]
        except (OSError, ValueError, KeyError):
            _cache = {}
    return _cache


def conform(produced: dict) -> dict:
    """Pad each dataset's rows with Oracle's columns, in place. Returns `produced`."""
    ref = oracle_columns()
    for key, rows in produced.items():
        cols = ref.get(key.split(".")[-1])
        if not cols or not rows:
            continue
        for row in rows:
            for c in cols:
                row.setdefault(c, None)
    return produced
