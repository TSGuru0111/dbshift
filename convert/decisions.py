"""Decisions a person made about the target schema, recorded with their name.

    python -m convert.decisions --zero-dates-nullable --by you@x.com --run <collector-run-id>
    python -m convert.decisions --show

Some findings have no correct automatic answer and several acceptable ones. A
MySQL zero date in a NOT NULL column is the first: PostgreSQL can hold neither
the date nor a NULL, so either the source is corrected, the table is left out,
or the target column is made nullable and the value arrives as NULL -- which is
data loss, reported as such by Phase 8. Choosing is the data owner's call.

A decision is recorded here, per column, with who made it and when, and read
by the phases it affects -- Phase 4c emits the column nullable with a note, and
Phase 7's preflight stops refusing that load. Nothing is inferred: a column not
listed keeps its NOT NULL and the load stays refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "convert"

PATH = Path(__file__).resolve().parent / "output" / "decisions.json"


def load(path: Path = PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def nullable_for_zero_dates(estate: str, path: Path = PATH) -> dict[str, dict]:
    """{"table.column": decision} for columns a person made nullable for zero dates."""
    d = (load(path).get("zero_dates_nullable") or {})
    if (d.get("estate") or "").lower() != (estate or "").lower():
        return {}
    return {c: d for c in d.get("columns") or []}


def record_zero_dates_nullable(*, estate: str, columns: list[str], decided_by: str,
                               collector_run_id: str, path: Path = PATH) -> dict:
    if not decided_by or "@" not in decided_by:
        raise PermissionError("a decision needs the name (email) of the person making it")
    if not columns:
        raise ValueError("no zero-date columns to decide about")
    data = load(path)
    data["zero_dates_nullable"] = {
        "estate": estate, "columns": sorted(columns), "decided_by": decided_by,
        "decided_at_utc": datetime.now(timezone.utc).isoformat(),
        "collector_run_id": collector_run_id,
        "consequence": ("The zero dates arrive as NULL. That is data loss, and Phase 8 reports "
                        "the affected tables as a mismatch; it is not normalised away."),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data["zero_dates_nullable"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--zero-dates-nullable", action="store_true")
    ap.add_argument("--by", default="")
    ap.add_argument("--run", default=None, help="the collector run whose zero dates are decided")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args(argv)
    if args.show or not args.zero_dates_nullable:
        print(json.dumps(load(), indent=2))
        return 0
    from provision import records
    rows = [r for r in records._dataset(args.run, "mysql_zero_dates")
            if str(r.get("nullable")).upper() in ("NO", "N")]
    if not rows:
        print(f"run {args.run} records no zero dates in NOT NULL columns; nothing to decide")
        return 1
    d = record_zero_dates_nullable(
        estate=rows[0]["owner"], columns=[f"{r['table_name']}.{r['column_name']}" for r in rows],
        decided_by=args.by, collector_run_id=args.run)
    print(f"recorded: {', '.join(d['columns'])} nullable on the target, by {d['decided_by']}")
    print(d["consequence"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
