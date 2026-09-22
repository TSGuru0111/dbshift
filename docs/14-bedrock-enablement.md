# Enabling Bedrock — what changes, phase by phase

> **Status on 2026-09-12.** `bedrock-runtime:InvokeModel` is blocked on this
> account by an AWS Marketplace subscription gap. Listing models works; every
> invoke fails. Every phase that has a model seam runs on a labelled stand-in
> or on rules alone, and says so. This document is the plan for the day the
> block lifts: what to do first, what each phase does differently, what stays
> exactly as it is, and how to prove it worked.

## The one principle that does not change

**AI proposes. Deterministic rules decide. Humans approve anything irreversible.**

Enabling Bedrock changes *who drafts*, never *who decides*. Every seam below
already exists and already expects a model-shaped answer. A model-authored
output passes the same gates a template or a static fixture does, is labelled
`source: bedrock` with its `model_id`, and writes one audit row per call.
Nothing in this plan gives a model a shortcut, and nothing lets it execute.

## Step 0 — unblock the account (an administrator, once)

**First, the actual blocker as of 2026-09-12:** every invoke now fails with
`INVALID_PAYMENT_INSTRUMENT: A valid payment instrument must be provided`. The
Marketplace subscription is being attempted and refused because the AWS account
has no valid payment method on file. An account administrator adds one in the
**Billing console**, waits about two minutes, and re-runs `bedrock.verify`.
Nothing below matters until that is done, and it may be all that is needed.

Then, if invoke still fails with the 2026-09-09 Marketplace-permission text,
from `05-aws-services.md`, either is sufficient:

1. **Enable model access in the Bedrock console** for `ap-south-1`. This
   performs the Marketplace subscription centrally. Preferred: the role then
   needs no Marketplace permission at all.
2. **Add `aws-marketplace:ViewSubscriptions` and `aws-marketplace:Subscribe`**
   to the `DBA_permissions` permission set, so the role can self-subscribe on
   first use.

Models to enable: the two tiers in `bedrock/models.json` — Claude Haiku 4.5
(fast) and Claude Sonnet 4.5 (reasoning), both via `global.*` inference
profiles because `ap-south-1` has no on-demand throughput for them under a bare
id. Optionally the APAC-scoped Sonnet 4 alternate for data-residency cases.
Claude Sonnet 5 is a separate entitlement gap on this account ("not available
for this account"); do not plan on it.

Wait about two minutes after the change, as the error message itself advises.

## Step 1 — prove it, then record it

```powershell
$env:AWS_PROFILE = 'dbshift-static'      # fresh session keys from the portal
python -m bedrock.verify                  # both tiers must print [OK]
python -m bedrock.verify --include-alternates
```

Only then edit `bedrock/models.json`: set `invoke_status` to `"OK"`, set
`verified: true` on each tier that passed, and put the date in `tested_on`.
**Never set `verified` from a report or a console screenshot** — only from a
passing run. The 2026-09-09 session saw invokes succeed for ten minutes and
then fail once the Marketplace state settled; re-run `bedrock.verify`
immediately before any demo that depends on a live model.

Budget alerts at $25 / $50 / $75 must exist first (`06-cost-model.md`). A full
run over the reference estate costs roughly $6–10 in inference.

## Step 2 — per phase

### Phase 1 · Discover — no change

No model. The SHA-256 per PL/SQL object is what lets every later phase skip
re-reading unchanged code, which is the single biggest lever on Bedrock cost.

### Phase 2 · Assess — no change

No model, by design. Severity comes from the rule. Do not add a model here; a
model that can change a verdict is the thing this project exists to prevent.

### Phase 3 · Size & Edition — switch the proposer, keep the validator

**Today:** `sizing/propose.py :: heuristic_proposal()` runs; `bedrock_proposal()`
raises `NotImplementedError`. `sizing.run --bedrock` exists and fails.

**Change:** implement `bedrock_proposal(facts, model_id)`:

- Build a prompt from the *same* `facts` dict the heuristic reads (segment bytes,
  feature usage, structural evidence, character set, utilization if present).
- Ask the **reasoning** tier for strict JSON: `edition`, `licence_model`,
  `instance_class`, `storage_gb`, `rationale`, `edition_forcing_features[]`,
  `confidence`.
- Validate the JSON (shape, known instance class, storage ≥ 20) before it
  becomes a proposal; an invalid answer falls back to the heuristic and is
  recorded as such in `source`.
- Return the same shape `heuristic_proposal()` returns, with
  `source: "bedrock"`, `model_id`, and token counts.

**Unchanged:** `sizing/validate.py` and `sizing/policy.py`. The seven checks
still run and still override. **Expect the override to fire more, not less**:
a model reading `DBA_FEATURE_USAGE_STATISTICS` will cite Multitenant as
edition-forcing exactly as the heuristic did, and the rules will dismiss it.
That recorded disagreement is the demo's best moment; keep it.

**Console:** `/api/size` gains a `proposer` choice (`heuristic` / `bedrock`),
defaulting to `heuristic` until `models.json` says `verified: true` for the
reasoning tier. The screen already shows `source`; it will read "bedrock".

### Phase 4 · Detect & Remediate — wire `bedrock_fix`, keep every gate

**Today:** `remediate/generate.py :: bedrock_fix()` raises
`GenerationUnavailable`. `model_mode="static"` serves
`bedrock/static/DBMIG_APP.json` (25 hand-written entries, labelled). The
console pins `model_mode="static"` at `web/server.py:691`.

**Change:** implement `bedrock_fix(finding, model_tier="reasoning")`:

- Prompt: the finding (rule id, title, rationale, owner, object, detail), the
  object's discovered structure (columns, constraints, indexes from the
  collector run), the prohibitions from `remediate/policy.py` quoted verbatim,
  and the allowed statement shapes.
- Strict JSON: `sql`, `rollback_sql`, `explain`, `caveat`, `evidence`,
  `confidence`. Optionally `advice` + `artefact` for findings whose right answer
  is not SQL on the source (the `ADVICE_DRAFTED` path the static entries use).
- Validate before use: both statements non-empty (or advice only), the target
  object named, no prohibited pattern. An invalid answer is retried up to
  `policy.MAX_ATTEMPTS` (3, already recorded but unused) with the validator's
  reason appended; then the finding routes to a human with
  `model_output_invalid` as the reason.
- Return the template shape. **Then the five gates run unchanged**: static,
  policy, syntax, dry run on `DBMIG_REHEARSAL`, approval. A model-drafted
  `ALTER TABLE` is applied and rolled back on the copy like any other.
- One audit row per call to `remediate/output/agent_decisions.jsonl` (same
  shape as Phase 4b's), per the convention in `09-conventions.md`.

**What it moves, precisely (from `phase-04-remediate.md`):** 25 of the 61
findings routed to a human — the L2 findings with no template. It does **not**
touch the 25 L3 findings (`human_authored` by policy, returned before any
generator is tried), the 3 L4 decisions, or the 11 `DQ-009` findings (a
discovery gap: collect `CHAR_LENGTH` in the column probe and they become
templated fixes with no model at all — do that first, it is cheaper).

**Static fixtures:** keep the files. `model_mode` stays a three-way switch
(`off` / `static` / `live`); the CLI default moves to `live` only after
`bedrock.verify` passes, and the console pin changes from `"static"` to a
setting the operator flips on the Remediate screen, defaulting to `static`.
Never fall back from `live` to `static` silently: a run is one mode, and the
plan records which.

**Expected result on `DBMIG_APP`:** the same 2 template fixes, plus up to 25
model-drafted fixes or advice entries, every one gated. Compare the model's
answers with the static entries for the same findings — the two draft answers
the static authoring *withdrew* (`DQ-006` precision change, `DQ-008` column
exclusion) are exactly the mistakes a model may make; the policy gate catches
the first, a person must catch the second.

### Phase 4b · Convert PL/SQL — already wired; flip the mode

**Today:** `convert/model.py :: live_convert()` is complete: prompt from the
construct catalogue and the shadow column types, strict JSON, `validate_output()`
(unknown construct, missing construct, non-JSON, bad confidence all refused),
one audit row per call, tested with a stub client in `convert.selftest`. The
CLI default is `--model-mode static`; the console pins `static`.

**Change:** none in code. Run `python -m convert.run --model-mode live` and
flip the console pin the same way as Phase 4. The **parity gate** is the
control that matters here: a model that compiles but silently drops a
construct is refused with the construct named.

**Honest expectation:** on both existing estates the model tier has **nothing
to do** — every object is inside the rule tier's subset. To demonstrate a live
conversion, seed one object that is deliberately outside it (a `CONNECT BY`
hierarchy function, a `BULK COLLECT` loop, a `DECODE` expression, a
`DBMS_OUTPUT`-free `UTL_FILE` call). `scripts/oracle-source/` seeds defects on
purpose already; this is the same idea. Re-run discovery afterwards, since
Phases 6–9 bind to a collector run id.

### Phase 5 · Blocker gate — no change

No model, by design. Waivers stay human.

### Phase 6 · Provision — no change

No model, by design. Every property traces to a record.

### Phase 7 · Migrate — one seam, in the triage step

**Today:** step 10, *Triage the import log and repair* (`migrate/steps.py`),
classifies every Data Pump error by rule: repaired, expected, or
`left_for_a_person` with the full error text. On the first real run: 6
repaired, 13 expected, 0 for a person — the rules cover what this estate
produced.

**Change:** for each `left_for_a_person` entry, ask the reasoning tier to
**classify and draft**, never to run:

- Input: the error text, the object it names, the object's source DDL from
  discovery, and the repair rules that already exist (so it does not re-draft
  what a rule covers).
- Output, strict JSON: `classification` (`repairable` / `expected` / `needs_a_person`),
  `why`, and — only for `repairable` — a single `sql` statement and its
  `rollback_sql`.
- The draft goes through the **same allow-list** the runbook SQL does (only
  the shapes the step already executes: grants to discovered custom roles,
  `CREATE INDEX` matching source DDL, `ALTER … COMPILE`) and is shown on the
  step card as *drafted by the model, awaiting approval*. It is applied only
  on a named approval, like `RDS-004`'s resolution.

A different estate is where this earns its keep; on `DBMIG_APP` it will have
nothing to classify, which is the correct result.

### Phase 8 · Validate — no change

No model. Every statement is a `SELECT` and every verdict is a comparison.

### Phase 9 · Cutover — narration only, optional

The **fast** tier's purpose in `models.json` is narration of computed facts.
The one legitimate use here is a **cutover brief**: a page of prose that
restates the certificate — what is met, what is waived by whom, what is
declared unfinished, the outage consequence — for the people in the window.
It is generated *from* the certificate, labelled as model-written, and never
an input to any decision. Build it only if a client asks for it.

### Phase 10 · Report — narration only, optional

Same rule. An **executive summary** paragraph at the top of the migration
assessment report, generated by the fast tier from the report's own numbers,
labelled `Summary written by <model_id> from the figures below`. The figures
stay computed. The footer line "Nothing in this report was written by a model"
changes to name the one paragraph that was.

## Step 3 — cross-cutting changes

| Item | What to do |
|---|---|
| **Labelling** | Every model output carries `source: "bedrock"`, `model_id`, `input_tokens`, `output_tokens`. The console's `SOURCE_LABEL` maps already include `bedrock: 'Bedrock model'`; nothing to add |
| **Audit** | One JSON line per call: phase, object, input SHA-256, model id, tier, tokens, validator verdict and error. Phase 4b writes `convert/output/agent_decisions.jsonl` today; Phases 3, 4 and 7 write the same shape beside their plans. The metadata-repository table is `audit.agent_decision` (`09-conventions.md`) |
| **Cost control** | Skip unchanged objects by SHA-256 (already collected). Cap `max_tokens` per call (4,096 for conversion, 1,024 for fixes, 512 for narration). Temperature 0 for anything a gate will read |
| **Retry** | `MAX_ATTEMPTS = 3` in `remediate/policy.py`; the retry feeds the validator's reason back. Never retry a transport error that says *not entitled* (`ModelUnavailable`) |
| **Fallback** | None silent. A run declares its `model_mode` and the plan records `model_generation_enabled`. If the model is unavailable mid-run, the affected findings say `model_unavailable: …` and route to a person |
| **Console pins** | `web/server.py` lines 691 and 760 pin `model_mode="static"`. Replace with one server-side setting in `web/settings.py` (`model_mode`, default `static`), exposed as a switch on the Remediate and Convert screens, and refused as `live` unless `models.json` says the reasoning tier is verified |
| **Demo script** | `13-demo-script.md`: the Phase 4 "say the Bedrock thing early" paragraph and the Phase 4b "where is the AI" answer both change from *blocked, static stand-ins* to *live, gated, and here is the audit row*. The honesty table (Bedrock moves 25 of 61) stays true |
| **CLAUDE.md** | The Bedrock row in *Current state* flips from ⛔ to ✅ with the verify date; the "Bedrock does not enter until Phase 4" sentence stays true |

## Step 4 — prove it end to end

Run, in this order, and keep the outputs:

1. `python -m bedrock.verify` — 2/2.
2. `python -m convert.selftest` — 64/64 (the stub-client path is unchanged).
3. `python -m remediate.run --model-mode live` on `DBMIG_APP` with the
   rehearsal DSN set. Expect: `AUTO_APPLY 2`, and the 25 former
   `no_template_and_model_disabled` findings now `READY_TO_APPLY`, `BLOCKED`
   (approval only), `ADVICE_DRAFTED` or `REJECTED` — with **zero** still
   reading `model_disabled`. Every `REJECTED` must name the gate.
4. `python -m convert.run --model-mode live` on both estates. Expect the same
   6 + 6 rule conversions and `MODEL_REQUIRED 0`; then on the seeded hard
   object, one `bedrock`-sourced conversion that passes parity and compile.
5. `python -m sizing.run --bedrock`. Expect the same EE BYOL verdict, with the
   proposal's `source` reading `bedrock` and the `edition_rationale` override
   still firing.
6. Re-run `scripts/telco-source/run_phases.ps1` — the portability check. A
   phase that only works on `DBMIG_APP` with a model is a phase tuned to one
   estate.
7. Read every `agent_decisions.jsonl` line once. If any row has
   `validator_verdict: rejected`, the reason must be one a reader can act on.

Then, and only then, update the phase docs' *Latest update* blocks and the
demo script.

## What stays out, deliberately

- **No model in Phases 2, 5, 6 or 8.** These are the deterministic spine.
- **No Opus tier.** `models.json` records the decision; Sonnet 4.5 carries
  every reasoning task. Reaching for Opus is a cost decision to justify, not a
  default.
- **No Bedrock Knowledge Base** in the beta (`02-architecture.md`).
- **No model executes SQL, anywhere.** Step Functions owns execution in the
  target architecture; here, the gates and a named approval do.
