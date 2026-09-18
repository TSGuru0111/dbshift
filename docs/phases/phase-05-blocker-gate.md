# Phase 5 — Blocker gate

> **Latest update — 2026-09-17.** **The gate now runs over AWS SCT's action
> items, split by where the work belongs.** `blocker/sct_gate.py` +
> `sct_run.py`. SCT is the assessment a client reads since 2026-09-17, so it is
> what the gate judges; `gate.py` is unchanged and still runs on the 50-rule
> findings.
>
> **The split is the point.** A single "halted" list sends people to fix the
> wrong thing. On `DBMIG_APP` the gate now reports 2 items to fix in the source,
> 3 the target absorbs, 2 needing a decision and 5 needing a person — and only
> **2 of 12** halt anything. A gate that halted on all 12 would be ignored.
>
> **CDC readiness does not come from SCT**, which never reads redo. It comes
> from the Connect preflight's own reading of `v$database`, interpreted by
> `collector.mode.readiness` — the same function the mode picker uses, so the
> two cannot drift. That replaces the rules engine's `OPS-001`/`OPS-002` with
> better evidence: measured, not inferred from a rule.
>
> Self-test `blocker.selftest_sct` **70/70**.
>
> Earlier — 2026-09-16.** **The gate now knows which migration it is
> judging.** `blocker/policy.py` already recorded that `OPS-001` blocks only
> `migrate_cdc` and `cutover` — but nothing ever told it those phases were not
> happening, so a full-load migration still halted on the CDC prerequisites. The
> gate reads the mode declared in Phase 1: on a full-load run `migrate_cdc`
> leaves the downstream list and is reported `not_in_scope` rather than `clear`
> (the difference between "nothing blocks it" and "it is not happening"), and a
> blocker whose whole blast radius is out of scope is listed in
> `out_of_scope_blockers` rather than halting the run. Every summary now names
> the migration it judged. On `DBMIG_APP`, a full load goes from **HALT on 4** to
> **HALT on 1** — `RDS-004`, which blocks either way — and `cutover` turns clear.
>
> Earlier — **2026-09-10.** Phase built **and added to the console**,
> including granting and revoking waivers from each blocker. The console rail was
> relabelled to architecture phase numbers at the same time, so this is
> **Phase 5** on screen as well as in the docs. The gate
> halts the run when any critical finding is open, but it also reports **which
> downstream phases each blocker actually stands in front of**, because a binary
> halt sends people to fix the wrong thing. On the current estate it returns
> **HALT**, with `provision` clear and `migrate_cdc`, `validate` and `cutover`
> blocked. Waivers require a named approver plus a real reason.

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

**Console: Phase 5 - Blocker gate.** Waivers are granted and revoked per blocker there, with
validation shared from `blocker/policy.py` so the two cannot drift on what counts
as a real waiver.

The gate is fast and deterministic, so the console calls it as a plain request
rather than streaming a progress ticker. There is nothing to watch, and faking
progress for a five-millisecond operation would be theatre.

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

**2026-09-16 — the gate honours the declared migration mode.** `evaluate()` reads
`migration_mode` from the assessment and filters `policy.DOWNSTREAM`: on a
full-load run `migrate_cdc` is reported `not_in_scope` with the reason, which is
a different statement from `clear`. A blocker whose entire blast radius is out of
scope goes to `out_of_scope_blockers` instead of halting the run, and is still
listed — it would block a CDC migration and the record has to keep saying so.
Every summary now opens by naming the migration judged, so a verdict can never be
read against the wrong one. `by_phase` is built in `DOWNSTREAM` order rather than
scope order, so the output still reads as the pipeline.

The blast radii in `policy.BLOCKS` were already right; this is the half that was
missing. Nothing in the policy changed.

**2026-09-10 — console stage added.** Stage 6: verdict banner, per-phase table,
expandable blockers, and waiver grant/revoke. Found and fixed a routing bug while
testing it — `DELETE /api/waivers/...` returned 404 because an earlier catch-all
`/api/{kind}/{identifier}` route matched first and answered from the wrong
handler. Replaced with explicit paths.

**2026-09-10 — built.** Phase created: `blocker/policy.py` (blast radius, waiver
validation), `blocker/gate.py` (evaluation), `blocker/run.py` (CLI). Verified
against the reference estate: HALT with `provision` clear; a well-formed waiver
accepted and recorded; a short-reason waiver and an unnamed waiver both refused.

## The gate over AWS SCT's action items

```powershell
.\.venv\Scripts\python.exe -m blocker.sct_run            # the verdict, split by where
.\.venv\Scripts\python.exe -m blocker.sct_run --compare  # ...beside the rules gate
.\.venv\Scripts\python.exe -m blocker.selftest_sct       # 70/70, offline
```

### What halts, and what is only work

An action item halts a phase when **both** are true: `sct/route.py` says it
blocks that phase, *and* the phase is in scope for the declared migration.
Everything else is reported as work.

On `DBMIG_APP`, full load + CDC:

| | Blocks | Why |
|---|---|---|
| **SCT 5200** external tables | `migrate_full_load`, `migrate_cdc` | RDS has no filesystem. The same blocker the rules engine raises as `RDS-004` — and SCT finds more occurrences |
| **SCT 5659** no primary key | `migrate_cdc` | CDC cannot apply updates row by row. Same as `DQ-001` |
| **CDC readiness** | `migrate_cdc`, `cutover` | `NOARCHIVELOG`, supplemental logging `NO` |

The other 10 items are work: real, not going away, and not stopping a phase.
The summary says so explicitly — *"10 action item(s) remain as work rather than
blockers — they do not stop a phase, and they do not go away"* — because
"PROCEED" must never read as "nothing to do".

### Where CDC readiness comes from, and why

SCT assesses schema and stored-code conversion. It never reads
`v$database.log_mode`. So the requirement the rules engine raised as `OPS-001`
and `OPS-002` cannot come from SCT — and does not need to: the **Connect
preflight already measures it**, and `collector/mode.py` stores the verdict in
the discovery manifest as `migration_mode.cdc_readiness`.

The gate reads that stored reading. It does not re-derive it: a second copy
would be free to drift from the one the mode picker shows the operator.

**Absent evidence reports blocked, never clear.** A gate that says "ready"
because it failed to look is worse than no gate, and the self-test asserts it
for both `{}` and `None`.

### Three bugs found by running it

1. **The facts key never existed.** I read `manifest["facts"]`, so the gate
   reported *"log mode is unknown"* about a source it already knew to be
   `NOARCHIVELOG` — a gate inventing an absence of evidence. It now reads the
   stored `cdc_readiness`.
2. **`manifest["schemas"]` is a dict, not a list** — `{configured, present,
   missing, discovered_not_configured}`. Iterating it yielded key names, so no
   estate ever matched and every run claimed no discovery existed. It now
   matches on `present`.
3. **The comparison compared different estates.** `--compare` printed "the two
   gates AGREE" for an SCT report on `DBMIG_TELCO` beside a rules assessment on
   `DBMIG_APP`. That is the same mistake `collector.verify` was fixed for on
   2026-09-14 — comparing two runs *whatever estate they collected*. It now
   prints which schemas each side read and refuses to compare verdicts across
   different ones.

One test bug too: the waiver fixture used `accepted_by` where the field is
`approved_by`, and the gate correctly rejected it. The validation working, not
a defect — the test was fixed, and a token-reason case added.

### The before/after, same estate

```
rules engine (DBMIG_APP)   HALT — RDS-004 blocks migrate_full_load, validate
AWS SCT      (DBMIG_APP)   HALT — 5200, 5659, CDC readiness
                                  blocks migrate_full_load, migrate_cdc, cutover
```

Both halt. The SCT gate blocks **more phases on more evidence**, and says who
must fix each one. `validate` is no longer blocked because SCT attributes the
external-table problem to the load rather than to validation — a difference
worth knowing before this gate is trusted at a client, and visible rather than
buried.
