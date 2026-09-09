"""Optional measured-utilization input.

A read-only catalogue scan cannot produce utilization percentiles -- there is no
history to take a percentile of. An AWS OLA / DB OLA, Migration Evaluator, AWR,
Statspack or any vendor monitoring product can. This module accepts that data so
Phase 3 can produce a **load-derived** recommendation instead of a capacity floor.

This is our own CSV contract, not a claim about any product's native export
format. Whatever the source, map it to these columns:

    metric,unit,p50,p90,p95,p99,max,samples,window_start,window_end,source

Metrics understood (anything else is carried through and reported, not used):

    cpu_cores_used   cores   CPU consumed, in cores -- not percent
    memory_used_gb   gb      resident memory actually used
    iops             iops    optional, reported only
    storage_used_gb  gb      optional, cross-checked against segment bytes

See sizing/samples/utilization_example.csv.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

REQUIRED_COLUMNS = {
    "metric", "unit", "p50", "p90", "p95", "p99", "max",
    "samples", "window_start", "window_end", "source",
}

SIZING_METRICS = ("cpu_cores_used", "memory_used_gb")

# Below this the sample is not a distribution. A weekday-only window also misses
# month-end batch, which is exactly when a database is under its real peak.
MIN_WINDOW_DAYS = 7

# p95, not max and not p50. Max sizes for a single outlier and over-provisions
# an Oracle licence; p50 under-provisions by construction. p95 plus explicit
# headroom is the defensible middle, and the choice is recorded in the output.
SIZING_PERCENTILE = "p95"

# Margin applied over the sizing percentile.
HEADROOM = 1.3


class UtilizationError(ValueError):
    pass


def _num(row: dict, key: str) -> float | None:
    raw = (row.get(key) or "").strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise UtilizationError(f"{row.get('metric','?')}.{key}: {raw!r} is not a number") from exc


def _parse_date(value: str, label: str) -> date:
    try:
        return datetime.fromisoformat(value.strip()).date()
    except ValueError as exc:
        raise UtilizationError(f"{label}: {value!r} is not an ISO date") from exc


def load(path: Path) -> dict:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise UtilizationError(f"{path.name} is empty")
        missing = REQUIRED_COLUMNS - {c.strip() for c in reader.fieldnames}
        if missing:
            raise UtilizationError(f"{path.name} is missing columns: {sorted(missing)}")
        rows = [{k.strip(): v for k, v in r.items() if k} for r in reader]

    if not rows:
        raise UtilizationError(f"{path.name} has headers but no rows")

    metrics: dict[str, dict] = {}
    starts, ends, sources, sample_counts = [], [], set(), []

    for row in rows:
        name = (row.get("metric") or "").strip()
        if not name:
            continue
        entry = {p: _num(row, p) for p in ("p50", "p90", "p95", "p99", "max")}
        entry["unit"] = (row.get("unit") or "").strip()
        entry["samples"] = int(_num(row, "samples") or 0)
        metrics[name] = entry

        starts.append(_parse_date(row["window_start"], f"{name}.window_start"))
        ends.append(_parse_date(row["window_end"], f"{name}.window_end"))
        sources.add((row.get("source") or "unknown").strip())
        sample_counts.append(entry["samples"])

    window_start, window_end = min(starts), max(ends)
    window_days = (window_end - window_start).days
    if window_days < 0:
        raise UtilizationError("window_end precedes window_start")

    present = [m for m in SIZING_METRICS if m in metrics]
    usable = len(present) == len(SIZING_METRICS) and window_days >= MIN_WINDOW_DAYS

    if not present:
        raise UtilizationError(
            f"{path.name} contains none of the sizing metrics {SIZING_METRICS}; "
            "it cannot size an instance"
        )

    return {
        "path": str(path),
        "source": ", ".join(sorted(sources)),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "window_days": window_days,
        "min_samples": min(sample_counts) if sample_counts else 0,
        "metrics": metrics,
        "missing_sizing_metrics": [m for m in SIZING_METRICS if m not in metrics],
        "sizing_percentile": SIZING_PERCENTILE,
        "headroom": HEADROOM,
        "usable_for_sizing": usable,
        "reason": (
            f"{window_days} day window from {', '.join(sorted(sources))}, "
            f"sizing on {SIZING_PERCENTILE} with {HEADROOM}x headroom"
            if usable
            else (
                f"window is {window_days} days, below the {MIN_WINDOW_DAYS} required"
                if window_days < MIN_WINDOW_DAYS
                else "missing " + ", ".join(m for m in SIZING_METRICS if m not in metrics)
            )
        ),
    }


def requirements(util: dict) -> dict:
    """Turn measured load into a vCPU and memory requirement."""
    pct = util["sizing_percentile"]
    cpu = util["metrics"].get("cpu_cores_used", {}).get(pct)
    mem = util["metrics"].get("memory_used_gb", {}).get(pct)

    def ceil(x: float) -> int:
        return int(-(-x // 1))

    return {
        "basis": "measured",
        "percentile": pct,
        "headroom": util["headroom"],
        "cpu_cores_observed": cpu,
        "memory_gb_observed": mem,
        "required_vcpu": ceil(cpu * util["headroom"]) if cpu is not None else None,
        "required_memory_gib": ceil(mem * util["headroom"]) if mem is not None else None,
    }
