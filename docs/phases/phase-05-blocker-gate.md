# Phase 5 — Blocker gate

> **Latest update — 2026-09-10.** Phase built. The gate halts the run when any
> critical finding is open, but it also reports **which downstream phases each
> blocker actually stands in front of**, because a binary halt sends people to
> fix the wrong thing. On the current estate it returns **HALT**, with
> `provision` clear and `migrate_cdc`, `validate` and `cutover` blocked. Waivers
> exist and require a named approver plus a real reason.

## Purpose

The last purely deterministic step before anything costs money or touches a
target. Nothing downstream runs while a critical finding is open.

It is deliberately the smallest module in the project. A gate that is hard to
read is a gate nobody trusts, and this one decides whether the rest of the
pipeline is allowed to happen at all.

## What actually happens

`blocker/gate.py :: evaluate(assessment, remediation, waivers)`

1. **Collect the criticals.** Every finding in the assessment with
   `severity == "CRITICAL"`. Nothing is recomputed — the severity came from the
   rule that fired, back in Phase 2, and is not re-judged here.
2. **Index drafted fixes.** If a remediation plan is supplied, note which rules
   already have a fix drafted, so a blocker can say a remedy exists.
3. **Validate waivers.** Each waiver needs `rule_id`, `approved_by` and a
   `reason` of at least 15 characters. Failures are recorded in
   `rejected_waivers` and the blocker stays live. A waiver is never silently
   dropped.
4. **Group by rule and attach blast radius.** For each distinct critical rule,
   `blocker/policy.py :: blast_radius()` says which downstream phases it blocks,
   why, and what clears it. **A rule with no recorded radius blocks
   everything** — an unrecognised critical must never be assumed harmless.
5. **Roll up per phase.** For each of `provision`, `migrate_full_load`,
   `migrate_cdc`, `validate`, `cutover`, list the unwaived blockers naming it.
6. **Verdict.** `HALT` if any unwaived blocker remains, `PROCEED_WITH_WAIVERS`
   if all criticals are waived, `PROCEED` if there were none.

The CLI exits **non-zero on HALT**, so a pipeline stops here without having to
parse the JSON.

## Inputs / Outputs

| | |
|---|---|
| Input | `assess/output/assessment.json` (required) |
| Input | `remediate/output/remediation_plan.json` (optional — enriches blockers) |
| Input | `--waivers` JSON list (optional) |
| Output | `blocker/output/gate_decision.json` |
| Exit code | `1` on HALT, `0` otherwise |

## Design decisions

**A binary halt is correct but blunt.** `NOARCHIVELOG` does not stop you
provisioning an instance or running a full-load migration — it stops change data
capture, and therefore a low-downtime cutover. Reporting only "halted" sends
people to fix the wrong thing, so the gate reports both: the overall halt, and
the per-phase picture underneath it.

**An unknown critical blocks everything.** `policy.BLOCKS` maps rules to their
blast radius. Anything absent falls through to `UNKNOWN_BLOCKS_EVERYTHING`, which
blocks all five downstream phases and says so. Failing safe matters more here
than being precise.

**Waivers exist, and they are the honest part.** A client may knowingly accept a
full-outage cutover rather than enable ARCHIVELOG. Refusing to model that would
just push the override into a spreadsheet nobody audits. What a waiver must never
be is anonymous — hence the named approver and the substantive reason, both
recorded in the output.

**Severity is not re-judged.** The gate reads the severity the rule assigned. It
has no opinion of its own, and no model is involved anywhere in this phase.

## Known limits

- **Waivers are per-rule, not per-object.** Waiving `DQ-001` waives it for every
  table missing a primary key, not one of them. Per-object waivers would be more
  precise and are not built.
- **No expiry.** A waiver granted today holds indefinitely. Real governance would
  time-box it.
- **The blast-radius map is hand-maintained.** Four rules are mapped. Every other
  critical rule blocks everything until someone records what it really affects.
- **The gate does not check whether a drafted fix was applied** — only that one
  exists. Applying fixes is not built (see Phase 4).

## How to run it

```bash
python -m blocker.run
python -m blocker.run --waivers waivers.json
```

Waiver file shape:

```json
[{"rule_id": "OPS-001",
  "approved_by": "someone@example.com",
  "reason": "Client accepts a full-outage cutover; the restart ARCHIVELOG needs is not scheduled before the pilot."}]
```

Not yet exposed in the console.

## Current result on the reference estate

**Verdict: HALT** — 4 critical rules open across 5 findings.

| Rule | Blocks | Clears when |
|---|---|---|
| `DQ-001` ×2 | `migrate_cdc` | a PK or unique index is added, or the table leaves CDC |
| `OPS-001` | `migrate_cdc`, `cutover` | source switched to ARCHIVELOG (needs a restart) |
| `OPS-002` | `migrate_cdc`, `cutover` | supplemental logging enabled on the source |
| `RDS-004` | `migrate_full_load`, `validate` | data source moves to S3, or the table is excluded |

`provision` is **clear** — nothing here blocks standing up the target. The two
`OPS` findings are the interesting ones: together they mean this estate cannot do
change data capture at all, so a low-downtime cutover is impossible until the
source is changed. That is a source-side database restart, not something the
pipeline can fix for you.

## Change log

**2026-09-10 — built.** Phase created: `blocker/policy.py` (blast radius, waiver
validation), `blocker/gate.py` (evaluation), `blocker/run.py` (CLI). Verified
against the reference estate: HALT with `provision` clear; a well-formed waiver
accepted and recorded; a short-reason waiver and an unnamed waiver both refused.
