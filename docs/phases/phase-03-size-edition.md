# Phase 3 — Size & Edition decision

> **Latest update — 2026-09-10.** `sizing.run.main` split into `execute()`,
> shared with the console, which now carries this as stage 4 including the
> utilization upload. Earlier: measured utilization can be supplied as a CSV,
> turning the capacity floor into a load-derived recommendation.

## Purpose

Decide what to buy: edition, licence model, instance class, storage. **This is
the only phase where a model makes a judgement call**, and the whole design
exists to bound it — the proposer suggests, the rules engine decides.

## What actually happens

`sizing/run.py :: execute()`

1. **Load** the collector run into SQLite (same loader as Phase 2).
2. **Extract facts** — `sizing/facts.py`: segment bytes, feature usage,
   structural evidence (partitioned tables, bitmap indexes, compressed tables),
   character set, log mode, and whether any usable utilization exists.
3. **Propose** — `sizing/propose.py`. Reads raw facts and suggests an edition,
   instance class and storage with a rationale. **Deliberately naive about
   licensing nuance**: it reports what the evidence appears to say. Two
   implementations, `heuristic` (runs today) and `bedrock` (wired, unreachable).
   Whichever ran is recorded in `source`.
4. **Validate** — `sizing/validate.py`. Seven checks, each `PASS` / `OVERRIDE` /
   `WARN`. **Where they disagree the rules win and the disagreement is recorded**
   as a first-class output.

### The edition verdict

`sizing/policy.py` decides from **two independent sources**, because either alone
misleads: feature-usage statistics can be sampled before a feature was exercised,
and a structure can exist unused. Either is sufficient to force EE.

Features are split into **hard** (unavailable on SE2 at any price — partitioning,
TDE, Advanced Compression, Diagnostic/Tuning Pack…) and **contextual** (need a
threshold before they force anything). Multitenant is the one that matters: XE
and RDS both run a container database with a single PDB, which every edition
includes.

## Inputs / Outputs

| | |
|---|---|
| Input | A collector run directory |
| Input | Optional utilization CSV (`--utilization`, or upload in the console) |
| Output | `sizing/output/sizing.json` — facts, proposal, decision |

## Design decisions

**Proposer and validator are separate on purpose.** A proposal is a suggestion;
only the rules engine produces a decision. That split is the architectural claim
of the whole project, and this is where it is demonstrated.

**The override is real, not staged.** The proposer reads the feature-usage view
and cites Multitenant and Partitioning as forcing EE. The rules engine overrules
the Multitenant half — the verdict holds because Partitioning forces EE alone,
but the *justification* changes, and in a licence negotiation the justification is
what gets audited. A naive read hands a client a bill for an option they do not owe.

**No prices.** An hourly rate depends on region, term, edition and licence model,
and a wrong number quoted to a client is worse than none. Licence **counts** are
derivable and computed; licence **cost** is left to a rate file the user supplies.

**Sizing is p95 with 1.3× headroom** when measured load exists. Not `max`, which
sizes for one outlier and over-provisions an Oracle licence; not `p50`, which
under-provisions by construction. Both values are recorded in the output.

**A utilization feed is refused, never ignored.** Missing metrics, a window under
seven days, or bad values fall back to the capacity floor and the validation trail
records why — so a rejected feed cannot pass for an absent one.

## Known limits

- **Without a feed this is a capacity floor, not a recommendation.** A catalogue
  scan has no history to take a percentile of. The output says so as a WARN.
- **AWS OLA has no API.** The integration is a documented CSV handoff, not a call.
- **Bedrock proposer is not wired.** InvokeModel is blocked on this account.
- **Memory sizing has no good signal on XE** — `sga_target` and `memory_target`
  are 0, and `cpu_count` reports the host's, not the database's.

## How to run it

```bash
python -m sizing.run
python -m sizing.run --utilization path/to/feed.csv
```

Console: stage 4, with CSV upload.

## Current result on the reference estate

**Enterprise Edition, BYOL** — `db.t3.medium` (2 vCPU / 4 GiB), 20 GB gp3,
AL32UTF8, **1 processor licence**. Forced by `Partitioning (user)`. Multitenant
detected and dismissed. 1 override, 2 warnings.

With the example 21-day feed it becomes `db.m5.2xlarge` and **4 licences** —
capacity-only sizing understated the licence exposure fourfold. That example is
marked `example_not_real_data` and is not used unless explicitly uploaded.

## Change log

**2026-09-10** — `main()` split into `execute()`; console stage added with
utilization upload.

**2026-09-09** — Utilization hook: CSV contract, p95 + 1.3× headroom, a
`peak_headroom` check against the observed maximum, refusal of short windows.

**2026-09-09 (earlier)** — Built. A bug caught in its own validation: the first
utilization check passed on `0 live metrics OR 15 AWR snapshots`, so the tool
would have silently claimed measured headroom it did not have. It now requires
live metrics **and** a day of history.
