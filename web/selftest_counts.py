"""Offline checks for the discovery counts the console shows. No database, no AWS.

The console tile is the first number a client sees, and they will reconcile it
against their own schema. Reporting Oracle's internals in it -- Text index
tables, a queue table, a materialized view's container -- makes the tool look
wrong about an estate the client knows better than we do.

The classification is duplicated: Phase 2 owns it as the SQLite view
`v_user_tables`, and the console cannot use that view because Phase 1 runs
before Phase 2 builds it. So the real check here is the last one -- the console
and the view must return the same number for the same run. If they ever
disagree, one of the two definitions has drifted.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "web"

from assess import loader as assess_loader
from .server import _discovery_summary, _is_internal, _owned_table_names

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [ok] {label}")
    else:
        FAIL += 1
        print(f"  [XX] {label}\n       got  {got!r}\n       want {want!r}")


def _datasets(run_dir: Path) -> list[dict]:
    """The dataset shape /api/discovery builds, including `all_rows`."""
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    out = []
    for entry in m["datasets"]:
        rows = json.loads((run_dir / entry["file"]).read_text(encoding="utf-8"))["rows"]
        out.append({"name": entry["dataset"].split(".")[-1],
                    "row_count": entry["row_count"], "all_rows": rows})
    owned = _owned_table_names(out)
    for d in out:
        key = {"tables": "table_name", "objects": "object_name"}.get(d["name"])
        rows = d.pop("all_rows")
        scope = owned if d["name"] == "tables" else frozenset()
        d["internal_count"] = (
            sum(1 for r in rows if _is_internal(r.get(key), scope))
            if key and rows and key in rows[0] else 0
        )
    return out


def _tile(datasets: list[dict], label: str) -> dict:
    return next(t for t in _discovery_summary(datasets, {}) if t["label"] == label)


def _latest_run() -> Path | None:
    base = Path(__file__).resolve().parent.parent / "collector" / "output"
    runs = [d for d in base.glob("*/") if (d / "manifest.json").exists()]
    return max(runs, key=lambda d: d.stat().st_mtime) if runs else None


def main() -> int:
    print("classification")
    check("a Text index internal is not a user table", _is_internal("DR$IX_X$I"), True)
    check("a materialized view log is not a user table", _is_internal("MLOG$_LOAN"), True)
    check("a queue table's history is not a user table", _is_internal("AQ$_Q_H"), True)
    check("an ordinary table is", _is_internal("CUSTOMER"), False)
    check("lower case is classified the same", _is_internal("dr$ix_x$i"), True)
    check("a None name does not raise", _is_internal(None), False)
    # The three that carry no `$` and that a prefix-only check would miss.
    owned = {"MV_LOAN_SUMMARY", "LOAN_EVENT_QTAB", "EXT_CUSTOMER_EXTRACT"}
    check("a materialized view's container is not a user table",
          _is_internal("MV_LOAN_SUMMARY", owned), True)
    check("a queue table is not a user table", _is_internal("LOAN_EVENT_QTAB", owned), True)
    check("an external table is not a user table",
          _is_internal("EXT_CUSTOMER_EXTRACT", owned), True)
    check("the same name is a user table when nothing owns it",
          _is_internal("MV_LOAN_SUMMARY"), False)

    print("\nowned-table lists")
    ds = [{"name": "materialized_views", "all_rows": [{"container_name": "MV_X"}]},
          {"name": "queues", "all_rows": [{"queue_table": "Q_X"}, {"queue_table": "Q_X"}]},
          {"name": "external_tables", "all_rows": [{"table_name": "E_X"}]},
          {"name": "tables", "all_rows": []}]
    check("every owner list is read, and de-duplicated",
          _owned_table_names(ds), {"MV_X", "Q_X", "E_X"})
    check("a null container is ignored",
          _owned_table_names([{"name": "materialized_views",
                               "all_rows": [{"container_name": None}]}]), set())
    check("a probe that was switched off contributes nothing",
          _owned_table_names([{"name": "tables", "all_rows": []}]), set())

    # Phase 2 filters objects on prefixes alone. Applying the owned-table rule
    # to objects as well excluded the materialized view itself, under-reporting
    # objects by 4 on DBMIG_APP while tables stayed correct.
    check("a materialized view is a user OBJECT even though its table is not",
          _is_internal("MV_LOAN_SUMMARY", frozenset()), False)

    print("\nthe tile")
    synth = [{"name": "tables", "row_count": 21, "internal_count": 12},
             {"name": "objects", "row_count": 90, "internal_count": 19}]
    check("tables reports the user count", _tile(synth, "Tables")["value"], 9)
    check("the excluded count is stated, not hidden",
          _tile(synth, "Tables")["hint"], "12 Oracle-managed tables excluded")
    check("objects reports the user count", _tile(synth, "Objects")["value"], 71)
    clean = [{"name": "tables", "row_count": 4, "internal_count": 0}]
    check("an estate with no internals says so plainly",
          _tile(clean, "Tables")["hint"], "every table found")
    check("a dataset that was never collected reports zero, not a crash",
          _tile([], "Tables")["value"], 0)

    print("\nagainst the real run, and against Phase 2")
    run = _latest_run()
    if not run:
        print("  [--] no collector run on disk; skipped")
    else:
        print(f"  run: {run.name}")
        datasets = _datasets(run)
        db = Path(tempfile.mkdtemp()) / "assess.db"
        assess_loader.load_run(run, db)
        conn = sqlite3.connect(db)
        for label, view in (("Tables", "v_user_tables"), ("Objects", "v_user_objects")):
            want = conn.execute(f"SELECT COUNT(*) FROM {view}").fetchone()[0]
            check(f"{label.lower()}: the console agrees with Phase 2's {view}",
                  _tile(datasets, label)["value"], want)
        raw = conn.execute("SELECT COUNT(*) FROM tables").fetchone()[0]
        check("and the raw count really was different (the bug was real)",
              _tile(datasets, "Tables")["value"] != raw, True)
        conn.close()

    print(f"\n{PASS}/{PASS + FAIL} checks passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
