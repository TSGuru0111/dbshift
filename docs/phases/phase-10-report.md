# Phase 10 — Report

> **Latest update — 2026-09-12 (built).** One self-contained HTML page that
> follows the structure of the two documents an AWS migration usually produces
> — the **SCT assessment report** (what converts automatically, action items by
> complexity) and the **DMS pre-migration assessment** (whether the tables can
> replicate) — generated entirely from DBShift's own records. On `DBMIG_APP`
> run `ee35e2bf`: stored code **6 of 8 converted automatically (75%)**, 1
> needs a person, 1 broken on the source; **38 action items** (2 simple, 16
> medium, 17 complex, 3 decisions); **12 DMS checks**, 4 fail, 6 warn, 2
> informational; full load and CDC both blocked, by name. In the console it is
> **stage 10 on the rail** — four tiles, the replication consequence, what each
> later phase has recorded, and the page itself embedded — as well as the
> header **Report ↗** link; the CLI is `python -m report.run`. Nothing in it is
> written by a model, and it says so in its footer.

## Purpose

Answer the question a client asks after seeing AWS's tools: *where is the DMS
and SCT report?* They expect two shapes — a conversion assessment with
percentages and action items, and a replication readiness check. DBShift
already had every number; this phase presents them in those shapes, traced to
the record each came from, without pretending the AWS tools produced them.

## What actually happens

`report/build.py :: build(...)` takes the records and returns plain data;
`report/render.py :: render()` turns it into HTML. Nothing is recomputed:

| Section | Built from |
|---|---|
| **At a glance** | Phase 2 scores, Phase 5 verdict, Phase 4b summary, action-item counts |
| **Target decision** | Phase 3 `decision` — edition, licence model, instance, storage, licence count, what forced the edition |
| **Schema conversion — code objects** | Phase 4b entries per type: converted automatically (rule, compiled), converted with the model tier, needs the model tier, needs a person, broken on the source, folded into a body. Percentage = automatic ÷ (total − specifications) |
| **Schema conversion — storage objects** | Phase 1 `objects` per type, with the count of Phase 2 findings against each type. States plainly that DBShift does not convert table DDL |
| **Action items** | One per Phase 2 issue, complexity = its remediation level (`L1` simple, `L2` medium, `L3` complex, `L4` decision), plus the conversion's own: definer-rights routines, and each object the rules declined |
| **DMS pre-migration assessment** | Twelve checks in DMS's vocabulary, each mapped to the assessment rules that decide it (`report/build.py :: DMS_CHECKS`). A CRITICAL rule firing is *fail*, any other *warning*, none *pass*. Sequences are counted from discovery because DMS never migrates them. **Blocks** comes from the Phase 5 per-phase view |
| **Replication path** | Phase 5 `by_phase`: full load and CDC, with what blocks each and the consequence. Notes that Phase 7 used Data Pump when a migration record exists |
| **Where the migration stands** | Gate verdict, provision render, validation status, cutover certificate — with a warning when they describe a different run |
| **Measured recall** | Phase 2 answer key, only when applicable |

## Inputs / Outputs

| | |
|---|---|
| Input | `assess/output/assessment.json` (required) |
| Input | `convert/output/conversion_plan.json`, `sizing/output/sizing.json`, `blocker/output/gate_decision.json`, `provision/output/provision_plan.json`, `validate/output/validation_report.json`, `cutover/output/certificate.json`, `migrate/output/migration_run.json` — each optional; a missing one leaves its section out or says so |
| Input | the collector run's `objects.json` |
| Output | `report/output/migration_report.html`, `report/output/migration_report.json` |

## Design decisions

**Mirror the structure, not the authorship.** A reader who knows SCT's report
finds "converted automatically", "action items" and complexity tiers; a reader
who knows DMS finds "tables without a primary key", "supplemental logging",
"LOB mode". The page states, at the top and in the footer, that it is built
from DBShift's records and is not produced by those tools.

**Complexity is the remediation level.** SCT grades actions simple, medium and
complex by its own judgement. Here the grade is the level the assessment rule
carries, so the report cannot rank an item differently from the plan.

**Storage-object DDL is declared not converted.** DBShift converts stored code
(Phase 4b), not tables and indexes — on a heterogeneous path DMS Schema
Conversion does that. Claiming a conversion percentage for tables would be
claiming work that never happened.

**Rebuilt on every request in the console.** The route reads the records the
console holds and renders; there is no cached report to drift.

## Known limits

- **No effort estimate.** SCT does not give hours either; neither does this.
- **The DMS check list is twelve items**, the ones the assessment rules can
  decide. DMS's own assessment has more, some of which need a running task.
- **The conversion percentage counts objects, not lines.** A one-line trigger
  and a thousand-line package each count once, as in SCT.
- **Not printed to PDF.** The page has print styles; producing the PDF is the
  browser's job.

## How to run it

```powershell
python -m report.run                 # report/output/migration_report.html
```

Console: **Phase 10 · Report** on the rail, unlocked by the assessment like
Remediate and Gate. **Build report** fetches `/api/report.json` for the four
tiles and notes, and embeds `/api/report` beneath them; **Open in a new tab**
gives the printable page. The header **Report ↗** link jumps to the stage once
it is reachable, and opens the page directly before that. A note flags when the
validation or cutover records on disk describe a different collector run than
the one on screen, because the report will not silently mix runs.

## Change log

**2026-09-12 — console stage.** The report was reachable only from a header
link, and the page navigation still said "Phase 10 (Report) is not built", so
the user could not find it. Added stage `report` (n 10) to the rail, a
`view-report` screen, and `/api/report.json` (the same builder as `/api/report`,
so tiles and page cannot disagree). The stage unlocks with the assessment in
both the live and the reload path. Verified in headless Edge via Playwright
(21/21), including phone width and reload.

**2026-09-12 — built.** `report/build.py`, `report/render.py`, `report/run.py`;
`/api/report` in the console with a header link. First run reported as
summarised at the top. The `DMS_CHECKS` map and the complexity mapping are the
two places a reviewer should look when a figure seems wrong: everything else
is a count over a record.
