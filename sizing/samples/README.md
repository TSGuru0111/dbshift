# Utilization input

`utilization_example.csv` is **illustrative, not real data** — every `source`
value says so. It exists to document the contract, not to be run against a
client estate.

## Why this file exists

A read-only catalogue scan cannot produce utilization percentiles. There is no
history in the data dictionary to take a percentile of, so Phase 3 without this
input produces a **capacity-derived floor** and says as much.

An **AWS OLA / DB OLA** does exactly this measurement — it is a funded,
partner-delivered engagement whose discovery phase collects real utilization
over a window. Migration Evaluator, AWR, Statspack and vendor monitoring
products produce equivalent data. None of them expose an API, so the handoff is
a file.

## The contract

```
metric,unit,p50,p90,p95,p99,max,samples,window_start,window_end,source
```

| Metric | Unit | Used for |
|---|---|---|
| `cpu_cores_used` | cores | **Required.** vCPU sizing. Cores, not percent |
| `memory_used_gb` | gb | **Required.** Memory sizing |
| `iops` | iops | Reported only |
| `storage_used_gb` | gb | Reported only; cross-checks segment bytes |

Anything else is carried through and reported but not used for sizing.

## Rules the input must satisfy

- **Both required metrics present**, or the feed is refused
- **Window of at least 7 days.** Below that there is no distribution to take a
  percentile of, and a short weekday window misses month-end batch — which is
  when the database is under its real peak
- Valid ISO dates and numeric percentiles

A file that fails any of these is **refused, not ignored**: sizing falls back to
the capacity floor and the validation trail records why the feed was rejected.

## How sizing uses it

Sizing is on **p95 with 1.3× headroom**. Not `max`, which sizes for a single
outlier and over-provisions an Oracle licence; not `p50`, which under-provisions
by construction. The percentile and the headroom are both recorded in
`sizing.json` so the choice is auditable.

A separate `peak_headroom` check then compares the chosen class against the
observed **maximum** and warns if it sits below — acceptable when the peak is a
tolerable brief degradation, not acceptable when it lands on month-end.

## Use it

```
python -m sizing.run --utilization path/to/your_utilization.csv
```

With a usable feed, `utilization_evidence` moves from WARN to PASS and the
recommendation becomes load-derived instead of a capacity floor.
