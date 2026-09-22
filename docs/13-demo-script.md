# Demo script — presenting DBShift AI

> **Purpose.** A talk track for driving the console in front of a client, written
> against what the code actually does as of 2026-09-12. Every number here is from
> a real run and is traceable to a phase doc. If a number changes, change it here.
>
> **The rule for this document: never claim a capability the console cannot show.**
> The most persuasive thing this project has is that its own tooling refuses to
> overstate itself. Do not undo that in the narration.

## The one-sentence pitch

> "This is an AI agent that migrates an Oracle database to AWS — and the reason
> you'd trust it with your production estate is that it is built to refuse."

Everything else in the demo is evidence for that sentence.

## The spine — say this early, return to it often

**AI proposes. Deterministic rules decide. Humans approve anything irreversible.**

Three sentences of elaboration, worth memorising:

- A model never executes SQL. It drafts; a rules engine decides; a named person
  approves.
- Where the model and the rules disagree, **the rules win and the disagreement is
  recorded as evidence**. Phase 3 is a live example of that happening.
- Every irreversible act — deploying, migrating, cutting over — needs a human
  whose identity comes from the AWS credentials, never from a typed-in name.

That is the differentiator. Not the migration itself; anybody can move bytes.

---

## Before you start — the five-minute pre-flight

Run these and confirm they are green **before** anyone is watching. A demo that
starts with a connection error never recovers.

| Check | Command / action | Why it bites |
|---|---|---|
| Oracle XE is up | Connect in the console's **Connect** screen | XE stops on reboot |
| Your public IP is admitted | `python -m validate.run` reachability line | The security group pins **one** `/32`, and home IPs change. This exact thing broke a run on 2026-09-12 |
| The RDS target is running | Console → Phase 6 → status | The kill switch stopped it; RDS also auto-restarts a stopped instance after 7 days |
| AWS keys are fresh | `aws sts get-caller-identity --profile dbshift-static` | Session tokens expire in hours |
| Records agree | Phase 6 or 7 step 1 | See "the run-id mismatch" below — it is currently **live** |

### Two known states you must decide how to handle

**1. The records must describe the target's run.** The target was built from
collector run `6e48d16a`. Every time Discover runs, the records on disk move to
a new run and Phase 9's certificate correctly **refuses** until assess, sizing,
remediate, convert and the gate are re-run with `--run 6e48d16a`. As of
2026-09-12 they are aligned and the estate is **cut over**: the certificate
reads ready with a named approval on record. If you run Discover during the
demo, expect the refusal afterwards — and use it, it is a strong moment (see
Phase 9). The earlier not-ready certificates are kept in `cutover/output/runs/`.

Do not re-run the phases live in front of a client to "fix" it.

**2. Bedrock invoke is blocked.** Model access fails on an AWS Marketplace
subscription gap (`docs/05-aws-services.md`). Phase 4 runs on labelled static
fixtures instead. **Say so plainly** — the script below gives you the words. A
client who later discovers you implied a live model was running will discount
everything else you showed.

---

## The rail — what the client is looking at

Ten items across the top. **Connect carries no number** because it is a
prerequisite, not a phase; numbering it would shift every phase by one and
contradict the architecture doc in front of the client.

```
·  Connect      1 Discover     2 Assess      3 Size & Edition   4 Remediate
4b Convert PL/SQL             5 Blocker gate 6 Provision        7 Migrate
8  Validate     9 Cutover
```

Phase 4b is optional on the rail — it is the heterogeneous path — and unlocks
with the assessment like Remediate does. Before the demo, start the local
PostgreSQL (`scripts\postgres-target\run_pg.ps1`, needs Docker Desktop) and
register it on the Convert screen, or the compile gate will honestly report
blocked.

Open with this framing, before clicking anything:

> "Ten stages, gated in order. Each one has to finish before the next opens. You
> are going to watch it stop itself twice — that is the part I most want you to
> see."

---

## Connect — 60 seconds

Click through the six-check preflight: reachability, auth, container, catalogue
access, row-data access, CDC readiness.

**Say:**

> "Six checks before we touch anything. Note the account — `dbmig_collector`,
> read-only. The first question every DBA asks is what this tool can change on
> their database. The answer is nothing: it holds `SELECT` and nothing else, and
> the password lives in process memory, never on disk."

**If asked about client networks:** the collector runs *locally* and pushes
outbound over HTTPS. Nothing in AWS connects inbound to the source. Security
teams refuse inbound firewall holes, so this is how real discovery tools work.

---

## Phase 1 · Discover — 90 seconds

Click **Discover**. Probes stream over SSE, one line each.

**Say while it runs:**

> "47 datasets, straight out of the Oracle data dictionary. Object census,
> columns down to precision and nullability, indexes, constraints, partitioning,
> the actual PL/SQL text of every procedure and package, privileges, and feature
> usage statistics. Under a minute for something nobody could review by hand."

**The one detail worth pausing on — segment sizes:**

> "It sizes the target from `DBA_SEGMENTS` — real physical bytes — not from row
> counts. Row counts lie about storage."

**And the hash, because it pays off later:**

> "Every PL/SQL object is stored with a SHA-256 of its text. That is what lets a
> re-run skip unchanged code instead of re-billing a model to read it again."

**Numbers to quote:** `DBMIG_APP` — 1.03 GB, 90 objects, 51 constraints.

---

## Phase 2 · Assess — 3 minutes, this is a big one

Click **Assess**. Rules stream by, then scores and grouped issues appear.

**Open with the architectural claim:**

> "50 rules, and every one of them is a **row in a table** — a SQL predicate, a
> severity, a remediation level. Not code. Adding rule 51 is inserting a row, and
> the whole catalogue diffs in a pull request. Nothing is judged at runtime, which
> is why the same database always scores identically and why a model cannot
> quietly change a verdict."

**Then the recall number — this is the single most persuasive artifact:**

> "We seeded this database with known defects on purpose, wrote the rules without
> looking at some of them, and then measured. On the reference estate: **7 of 7
> detectable defects found, severity exact on all 7**."

**Then the second-estate proof, which is stronger and most people skip:**

> "Scoring an engine on the data it was tuned on measures the tuning, not the
> engine. So we built a second estate — different domain, telecom billing, 5.67 GB,
> 32.7 million rows, 14 defects, and nothing in it was looked at while writing the
> rules. Result: **14 of 14, severity exact on all 14**, with no code changes —
> only environment variables. Six rules fired for the first time in the project's
> history."

**Be honest about what it cost, if the room is technical:**

> "That exercise found four real bugs one estate could never have revealed. All
> four were the same shape: correct on a database where every dataset is non-empty
> and every table is granted, wrong anywhere else. That is exactly the class of bug
> that would bite on your estate."

**Scoring, if asked:** penalty accrues per rule, not per finding, scaled
logarithmically by blast radius, then `score = 100 × e^(−penalty/60)`. Any
critical caps the overall at 60. Linear scoring was tried and abandoned because
it saturates — past ~100 penalty every busy category reads zero and a bad
category is indistinguishable from a catastrophic one.

**Current result:** 66 findings → 36 issues. Overall **50**, capped by 5 criticals.

### The honesty slide — use it if the room is skeptical

> "Two limits I'd rather you hear from me. Structural rules like 'no primary key'
> are true anywhere. Semantic ones are heuristics — one of ours has already
> misfired twice on money columns, so at a client those are presented as
> **hypotheses to confirm**, not findings. And recall only means something on an
> estate where we know the answer key; anywhere else it reports *not applicable*
> rather than a fake percentage."

That paragraph buys more credibility than any number in the deck.

---

## Phase 3 · Size & Edition — 3 minutes, the money phase

**Frame it before clicking:**

> "This is the only phase where a model makes a judgement call — and the entire
> design exists to bound it. Watch what happens to the model's answer."

Click through. Show the verdict: **Enterprise Edition, BYOL**, `db.t3.medium`,
20 GB gp3, AL32UTF8, **1 processor licence**.

**Then the override — this is the demo's best 30 seconds:**

> "The proposer read the feature-usage statistics and said: Enterprise Edition,
> because this estate uses Multitenant and Partitioning. The rules engine
> **overruled half of that**. Multitenant is dismissed — XE and RDS both run a
> single PDB, which every edition includes. The verdict still holds, because
> Partitioning forces Enterprise on its own. But the *justification* changed."

**Then land why that matters commercially:**

> "In a licence negotiation, the justification is what gets audited. A naive read
> hands you a bill for an option you do not owe. The disagreement between the model
> and the rules is not hidden — it is recorded as a first-class output."

**The OLA comparison, if they know what that is:**

> "This reproduces the outputs of an AWS Optimization and Licensing Assessment in
> hours instead of weeks. It is not a replacement for one — there is no OLA API,
> so the integration is a documented CSV handoff."

**The utilization point — do this if you have the CSV:**

Upload the example 21-day feed. The answer moves to `db.m5.2xlarge` and **4
licences**.

> "Without measured load this is a **capacity floor, not a recommendation**, and
> the tool says so as a warning rather than pretending. With three weeks of real
> utilization, the same estate needs four licences instead of one. Capacity-only
> sizing understated the licence exposure fourfold. Sizing is p95 with 1.3×
> headroom — not max, which sizes for one outlier and over-buys an Oracle licence;
> not median, which under-provisions by construction."

**No prices, and say why:**

> "You will notice no dollar figure on the licence. Rates depend on region, term,
> edition and licence model, and a wrong number quoted to a client is worse than
> none. Licence *counts* are computed; licence *cost* needs a rate file you supply."

**Flag the example data honestly:** the 21-day feed is marked
`example_not_real_data` and is never used unless explicitly uploaded.

---

## Phase 4 · Remediate — 4 minutes, and the Bedrock conversation

**This is the phase you were most unsure about. Here is the framing that works.**

Open before clicking:

> "Generating SQL is easy. Proving it is safe to run on someone's production
> database is the entire problem. So nothing in this phase applies anything — it
> produces a plan you can read, and it proves what it *would* apply."

### Say the Bedrock thing early, in your own words

> "Full disclosure on this screen: our Bedrock model access is blocked on an AWS
> Marketplace subscription gap at the account level. So what you're seeing here is
> **hand-written static output, labelled as such** — every entry says
> `static_fixture`, model id `null`, and the plan reports
> `model_generation_enabled: false`. It exists so the pipeline runs end to end and
> every downstream phase is built against real shapes. If you ask the screen
> whether an AI wrote a recommendation, it gives you the true answer."

Then immediately turn it into a strength:

> "And this is worth more than a live model would be right now, because it proves
> the seam is real. When Bedrock is wired, a model-authored fix gets **no
> shortcut** — it returns the same shape a template does and passes exactly the
> same five gates."

### The four routes

> "Every finding is routed by the remediation level the *rule* assigned back in
> Phase 2 — not re-judged here. Level 1 auto-applies. Level 2 needs approval.
> Level 3 a human authors. Level 4 is never fixed by a tool because it is a
> decision, not a statement."

### The five gates — walk them

| Gate | The line to say |
|---|---|
| **static** | "One statement, names its target, carries a rollback — or it is rejected at generation time, not at apply time when it is too late to ask." |
| **policy** | "The prohibitions. Never drops a production object, never deletes rows, never alters privileges, never rewrites business logic." |
| **syntax** | "Offline only, and here is why that is interesting: Oracle executes DDL at parse time. Calling `DBMS_SQL.PARSE` on a `CREATE INDEX` would *create the index*. So real parsing happens on the rehearsal copy." |
| **dry run** | "It applies the fix to a restored copy of the database and rolls it back. Both must succeed." |
| **approval** | "A named human, for anything above level 1." |

### The dry-run demo — the strongest concrete evidence in this phase

> "Two fixes passed all five gates against a real restored copy — 85 objects, a
> gigabyte of data. `DQ-007` applied in 5.7 seconds and rolled back in 1.2.
> `PERF-001` applied in 65 milliseconds, rolled back in 110. Afterwards we
> confirmed the source still holds all 90 objects and its statistics still date
> from before the run. **The production database was never touched.**"

**The remap detail, if they are technical:**

> "Every fix is rewritten off the source schema before it touches the copy, and
> then re-checked that the source schema name is gone entirely. A half-remapped
> statement would run against production, so a partial rewrite is a hard error,
> not a best effort."

### The honest breakdown — do not skip this

If you show one table in this phase, show this one:

| Level | Count | Reason | Would Bedrock help? |
|---|---|---|---|
| L2 | 25 | no template, model disabled | **Yes** |
| L2 | 11 | template declined (`DQ-009`) | No — a discovery gap |
| L3 | 25 | human-authored by policy | No — policy, not availability |
| L4 | 3 | a decision, not a statement | No |

> "61 findings route to a human. It would be easy to imply a model fixes all 61.
> It would move **25**. Twenty-five more are reserved for a human by policy and
> wiring a model does not touch them. Eleven are a *discovery* gap, not a model
> gap — we don't collect the column's declared length, so a template can't safely
> guess it and a model would be missing the same fact. Collecting one more column
> converts all eleven deterministically, with no model and no AWS cost. That is
> the cheapest remaining win in the project."

That answer — knowing precisely which of your problems AI does *not* solve — is
the most senior-sounding thing you can say in the whole demo.

### If asked "what about the two critical OPS findings?"

> "Those are source-side. `ALTER DATABASE ARCHIVELOG` needs a database restart.
> That is a maintenance window and a customer decision, not something an agent
> applies at 2am."

---

## Phase 4b · Convert PL/SQL — 4 minutes, and the answer to "what about stored procedures"

**Built 2026-09-12. This is the phase that answers the heterogeneous question
honestly, so put it right after Phase 4.**

**Frame it against the industry first:**

> "When the target is not Oracle, the schema converts by tool — DMS Schema
> Conversion handles the large majority of DDL. The part that does not is the
> stored code: procedures, functions, packages, triggers. AWS's own Oracle
> Modernization Accelerator lists PL/SQL as manual. That is the part we built."

**Register the PostgreSQL target, then click Convert.** The ticker shows every
object being routed, then one compile event.

**The routing — say it while it runs:**

> "Every object is scanned against a catalogue of sixty Oracle constructs, each
> tagged with who handles it. A construct with exactly one correct translation
> goes to a **rule**. A construct that needs judgement goes to the **model
> tier**. A construct PostgreSQL cannot express goes to a **person**. And
> anything that is already broken on the source is not converted at all —
> converting a compile error faithfully produces a compile error."

**The result on the reference estate:** 9 objects. **6 compiled and rolled
back** on PostgreSQL 16. 1 needs a person (an XML-schema generated type). 1 is
broken on the source. 1 is a package specification folded into its body.

**Open the package body card. This is the best 60 seconds in the phase:**

> "An Oracle package became two PostgreSQL routines, and look at *what changed
> and why*. `NVL` became `COALESCE`. `SYSDATE` became a second-precision
> timestamp — because Oracle dates have second precision, and a microsecond
> value would never equal a stored one. And `SELECT INTO` became `SELECT INTO
> STRICT`."

Then the point that separates this from a text translator:

> "Without `STRICT`, PostgreSQL sets the variable to NULL when no row is found
> instead of raising. Every Oracle exception handler in the code becomes dead
> code, and it still compiles. A model gets that wrong. A regex gets that
> wrong. This is a rule, so it is never wrong."

**Open the trigger card:**

> "A PostgreSQL trigger that does not `RETURN NEW` silently cancels the row
> change. No error. The converter adds the return and records why."

**The one thing it refuses to translate — show the amber row:**

> "Oracle runs stored code with the definer's rights by default. PostgreSQL
> runs it with the caller's. The faithful translation is to add `SECURITY
> DEFINER` — and that widens privileges on your target. So it is *not* added.
> It is recorded on every routine as 'kept for a person', and the policy gate
> refuses `SECURITY DEFINER` outright, whoever wrote it. Rules over the model."

**The gates — same story as Phase 4, one addition:**

> "Static, policy, parity, compile, approval. **Parity** is the one Phase 4
> does not have: every construct found in the source must be accounted for —
> translated, with its PostgreSQL form actually present in the output, or
> declared untranslated with a reason. No Oracle syntax may survive. It exists
> because the failure that hurts is a conversion that compiles and quietly
> dropped behaviour."

**The compile gate, and why it is real here:**

> "PostgreSQL DDL is transactional. Oracle's is not. So this gate creates every
> converted object for real, inside one transaction, and rolls it back. The
> target is left exactly as it was found. It also says exactly what that
> proves: the body parsed and every declaration resolved. It does not say the
> SQL inside will run — that is planned at first call, and the gate is honest
> about it."

**The second-estate proof, again:**

> "Same code, telco estate, no changes: 8 objects, 6 compiled, 1 broken on the
> source, 1 spec absorbed."

**If asked "so where is the AI in this?"**

> "On these two estates, nowhere — and I would rather tell you that than
> pretend. Every construct present fell inside the deterministic tier. The
> model tier is wired, validated with strict JSON, audited per call, and tested
> with a stub; it is for `CONNECT BY`, `DECODE`, `ROWNUM`, cursors, bulk
> operations, dynamic SQL and format models. When your estate has those, that is
> where the model earns its place — behind the same five gates."

**Say what is not built:** nothing applies the DDL; there is no PostgreSQL
target provisioned; the rule subset is twenty-one constructs and says so.

---

## Phase 5 · Blocker gate — 3 minutes

**This is the phase whose purpose was least clear. Here is the correct framing.**

The wrong framing — the one to avoid — is *"the gate tells the client the
migration is impossible."* It does not. Use this instead:

> "A binary halt is correct but useless. If I just tell you 'halted', you go and
> fix the wrong thing. So this gate reports two things: the overall verdict, and
> underneath it, **which specific downstream phases each blocker actually stands
> in front of**."

Show the per-phase table. Then the payoff:

> "Verdict is HALT. But look — **provision is clear**. Nothing here stops us
> standing up the target, and nothing stops a full-load migration. What is
> blocked is change data capture, validation of one table, and cutover."

**Then the real finding, which is a genuinely valuable consulting output:**

> "The two `OPS` findings together mean this estate **cannot do change data
> capture at all** — it is in `NOARCHIVELOG` with no supplemental logging. So a
> low-downtime cutover is impossible until the source is changed, and that needs a
> restart. We are telling you that *now*, in the assessment, rather than at 3am on
> cutover weekend when the CDC task fails to start."

That is the answer to "why does this belong so early." Catching it in assessment
is the whole point.

### The waiver mechanism

Grant one live if the moment is right. It needs a **named approver** and a real
reason of at least 15 characters.

> "A client may knowingly accept a full-outage cutover rather than enable
> archivelog. Refusing to model that would just push the override into a
> spreadsheet nobody audits. What a waiver must never be is anonymous — so it
> takes a named approver and a substantive reason, both recorded in the output.
> A waiver is never silently dropped; a malformed one is recorded as rejected and
> the blocker stays live."

**Limits, if pressed:** waivers are per-rule, not per-object, and they do not
expire. Real governance would time-box them. Four rules have a hand-maintained
blast radius; **every other critical blocks everything** until someone records
what it really affects, because an unrecognised critical must never be assumed
harmless.

---

## Phase 6 · Provision — 3 minutes

**The architectural line first:**

> "The point is not that it creates an RDS instance. The point is that for every
> single property on that instance, it can tell you which record it came from."

Show the **provenance table**. Walk two or three rows out loud:

> "Engine and licence model — from the Phase 3 edition decision, Enterprise forced
> by Partitioning. Instance class — Phase 3's capacity floor. Character set —
> Phase 3, cross-checked against the source NLS *and* Phase 4's advice, and a
> mismatch is a render error, because character set cannot be changed after the
> instance is created."

**Then the split that keeps it safe:**

> "Rendering and checking are free and happen every run. Deploying is a separate,
> deliberate act. This is the first phase that can cost money, so it is built to
> refuse: it stops before creating anything unless the account id matches, the
> operator types back the exact hourly rate just computed from AWS's public price
> list, and — while the gate says HALT — gives a named acknowledgement with a
> reason."

**Show the cost tiles.** $0.098/hour, plus the *if forgotten for 30 days* tile.

> "That fourth tile exists because the most expensive thing in a proof of concept
> is the instance somebody forgot about."

**The pricing trap — a good detail if the room is technical:**

> "Prices come from AWS's public offer file, never from memory, and the lookup
> refuses to estimate if more than one product matches. That caught a real trap on
> its first run: **RDS Custom** lists the same instance at the identical $0.098 an
> hour under a different product. It is excluded by deployment model, not by price."

**Then the real deploy, which you can show as history:**

> "Stack complete, instance available. **23 minutes end to end** — every supporting
> resource inside the first minute, the database itself the other 22."

Open the events panel and the **live CloudFormation template read back from the
stack** — not the local render, which may since have changed.

**No AI in this phase, and say why if asked:**

> "Deliberately none. This is pure, auditable rendering from records. Putting a
> model in here would undercut the entire claim — that every property traces to
> evidence rather than to somebody's judgement at deploy time."

**Does it change with sizing?** Yes — that is exactly what the provenance table
shows. A different sizing verdict renders a different instance class, storage and
licence model.

**Show the kill switch briefly.** It lists what is billing, marked ours or not
ours, and needs the account id typed to destroy.

---

## Phase 7 · Migrate — 4 minutes, and the DMS question

### The eleven steps

> "Eleven steps in order, and every one shows what it does, **who decided it**,
> what it changes, whether it can be undone, and every command as it runs with
> passwords masked. The console is not a progress bar — it is the backend's own
> account of what it did."

Point at the `decided_by` labels: **rule**, **gate**, **phase4_advice** (which
says *static fixture, not model output* right on the card), **approval**,
**orchestrator**.

### Why Data Pump here — and the DMS/SCT answer

**This will be asked. Have the answer ready, and be precise about scale.**

> "For this migration we use Data Pump over S3, not DMS. This is Oracle to Oracle
> — homogeneous. There is no schema conversion to do; the bytes are already in the
> right dialect. At about a gigabyte it loads in minutes, needs no replication
> instance to pay for, and reuses the S3 path Phase 6 already built. Data Pump also
> loads in dependency order — tables, rows, then indexes and constraints, then
> code — so foreign keys aren't validated row by row during the load."

**Then pre-empt the real question, because it is the right one:**

> "That answer changes with scale and with target. At a terabyte, or where you
> need the source live during the load, DMS is the correct tool — full load plus
> change data capture, with the replication instance that implies. And the moment
> the target stops being Oracle, you need Schema Conversion as well. Our DMS
> settings are already carried in the Phase 4 plan and listed as *not applicable*
> for this run rather than deleted, so that path is scoped, not hand-waved."

### The heterogeneous / PL/SQL conversation

If they raise Oracle → PostgreSQL, stored procedures, or AWS's own accelerator:

> "Worth being precise here. AWS publishes an Oracle Modernization Accelerator for
> heterogeneous migration to PostgreSQL and MySQL. Schema Conversion handles the
> large majority of DDL automatically, and their accelerator uses Bedrock to
> convert the objects that fail. But read their scope carefully: it **explicitly
> does not convert PL/SQL** — stored procedures, functions and packages are listed
> as manual work."

Then place your project against it:

> "That is the honest state of the industry, and it is exactly where the hard part
> is. It is also why our discovery collects the full text of every procedure,
> package and trigger with a content hash — that is the input a conversion agent
> needs, and the hash is what stops you paying a model to re-read code that has not
> changed. Our current scope is deliberately homogeneous, one engine, one target.
> Heterogeneous PL/SQL conversion is the next capability, and we are not going to
> claim it before it exists."

**Do not** claim the current system does schema conversion. It does not, and it
does not need to for an Oracle-to-Oracle move.

### The repair step — the best story in this phase

> "The import completed with 17 errors. Now — 'completed with errors' is not an
> outcome. Data Pump reports a failure and carries on. So the next step reads the
> import log and classifies **every single entry** by rule: repaired where the fix
> is certain, expected where failing is correct, or handed to a person otherwise."

**The result:** 6 repaired, 13 expected and explained, **0 left for a person**.
11 of 11 comparable tables match exactly on rows.

**The best individual detail:**

> "One failure was the Oracle Text index. The dump carries 21c's internal index
> call, and 19c rejects it — that is the version downgrade Phase 6 warned about,
> showing up for real. It was rebuilt natively from the source DDL in 26 seconds.
> Twelve of the thirteen 'expected' failures were grants to our own read-only
> discovery account, which has no business existing on the target."

**Where AI goes next, said honestly:**

> "This triage step is where a reasoning model adds the most in the final version:
> classifying what these rules do not recognise and drafting a fix for a person to
> approve. Today the rules cover what this estate produced; a different estate will
> produce failures they do not recognise, and those go to a person — which is the
> correct behaviour, not a gap."

### The honesty moment on row counts

> "The first version of the count compared error strings too, so a table missing on
> *both* sides counted as 'matching'. The honest figure fell from 18 of 21 to 11 of
> 11. A count the read-only account could not take is now reported as **not
> comparable**, never as a match."

---

## Phase 8 · Validate — 3 minutes. Your strongest phase, and no AI needed

**Lead with the question:**

> "Phase 7 moved the data and counted rows. Equal row counts prove very little — a
> column can hold different values, a constraint can arrive unvalidated, an index
> can be missing, a sequence can be set to reissue numbers already used. So Phase 8
> answers the harder question: *is this the same database?*"

**Five levels.** Objects → structure → row counts → data content → behaviour.

**The headline result, all real:**

> "50 named objects present. 72 columns matching on type, length, precision, scale
> and nullability. 40 constraints, 13 indexes. 11 of 11 tables matching on exact
> row counts. And a checksum of **every row** matching on all eleven tables — 5.4
> million rows a side, in about 28 seconds. Zero mismatches."

**Level 5 is the one to dwell on:**

> "Behaviour means behaviour. The text index is asked to actually answer a search,
> not merely to exist. The external table is *read*, not merely present. The
> materialized view's freshness is checked, sequences are checked for position,
> grants are checked."

**Six expected differences, each traced.** Say what that means:

> "An expected difference must name its cause — a decision recorded earlier, a
> genuine 21c-versus-19c difference, or an account that only exists on the source.
> Anything that cannot be traced to one of those three is a **mismatch**, not a
> shrug."

**Read-only, and provably so:** every statement is a `SELECT`. Phase 8 can run
against a production target without an approval.

### The two stories that sell this phase

**First — the validation that validated nothing:**

> "On its first run this reported `validated` and exited zero, having compared
> **nothing** — every connection to the target had failed, and the verdict only
> looked at mismatches, so 'no comparison possible' passed. We fixed it three ways:
> anything not comparable now yields `incomplete` and a non-zero exit; levels 3 and
> 4 refuse to read '0 of 0' as a pass; and a reachability check now runs first and
> says in one line that your IP has changed, instead of eleven connection errors."

Then the point:

> "I'm telling you about our bug because that is the failure mode that matters in
> validation tooling. A validator that cannot fail is not a validator."

**Second — the vacuous check that found a real defect:**

> "The text-index check searched for the word 'the', got zero matches on both
> sides, and called that a match. Fixing it to take a word from the actual data
> uncovered a genuine defect **in the source database**: that index holds zero
> tokens while reporting itself as valid and indexed. Application searches on the
> source have been silently returning nothing. The migrated target does not share
> the defect — Phase 7 rebuilt that index, and it holds 13,896 tokens."

> "So the migration tool found a production bug that predates the migration. That
> is worth more to the customer than the migration."

**Note:** you can turn the checksum off (`--no-checksum`), and the report says
what was and was not compared.

---

## Phase 9 · Cutover — 3 minutes, and it ends on a refusal

**Set it up before you click:**

> "Phase 8 established the target matches the source. Phase 9 answers the only
> question left: *may we declare it live, and who says so?* This is where a
> migration stops being reversible in practice — so the phase is built to refuse."

**Eight requirements.** Each is *met*, *waived* by a named person, *not
applicable* with the reason, or *unmet* with what would clear it.

**Two ways to play this screen. Both are true; pick by the room.**

*The refusal.* Open one of the earlier certificates in `cutover/output/runs/`
(or run Discover first, which moves the records on):

> "It came back **not ready**, for three honest reasons: the records described a
> newer discovery run than the one the target was built from, the gate blocked
> cutover on the two archivelog findings, and the target was stopped. Issuing a
> certificate anyway would mean certifying a database against findings nobody
> produced for it. I would rather show you a tool that refuses than a demo that
> always ends green."

*The cutover.* Rebuild the certificate as it stands:

> "Every requirement is met, waived by a named person, or not applicable with
> the reason. The two archivelog findings were **accepted, by name, with a
> reason** — that is what made this a full-outage cutover, and it is on the
> record. The one target-side step Phase 4 set aside ran: applied 1, failed 0.
> And the declared gaps are still declared: no application is repointed, the
> source is authoritative until someone moves them."

(Do not quote run ids from memory. Read them off the requirement row.)

### The CDC requirement — the best single example of design honesty

> "Requirement four is change-data-capture lag. It reports **not applicable** —
> and it names why: there is no replication to measure, because CDC is itself
> blocked upstream. It then states the consequence rather than scoring a pass:
> this is a full-outage cutover, and everything written to the source after the
> export is not on the target."

> "A lag check that quietly passed because there was no lag would be the same
> class of bug as the validation that validated nothing. We had that bug once. We
> are not shipping it twice."

### Approval and execution, if asked

- The approver comes from **the AWS caller identity**, never a form field. "A name
  typed into a browser is not an approval."
- An approval binds to one collector run and does not carry to another.
- Execution runs **only** statements beginning `BEGIN DBMS_SCHEDULER.` or
  `BEGIN DBMS_MVIEW.`. Anything else in the plan file is refused and recorded.
  "Otherwise that JSON file becomes a remote code path into a production database."
- Requirement 8 is *there is a way back*: the source is untouched, and the target
  can be destroyed by the kill switch.

**Declared honestly:** applications are not repointed. No DNS, no connection
strings, no credentials. The certificate says so rather than implying otherwise.

---

## Closing — 60 seconds

> "Nine phases, and you watched it stop itself twice — once at the blocker gate,
> once at the cutover certificate. Both times for a reason it could name, with the
> specific thing that would clear it."

> "The measured claim is this: 7 of 7 seeded defects on the estate the rules were
> written against, and **14 of 14 on a second estate the rules had never seen**,
> with no code changes. 5.4 million rows checksummed with zero mismatches. Every
> property on the target traceable to the record it came from."

> "What is not built, so you hear it from me: the model seam runs on labelled
> static output until our Bedrock access clears; PL/SQL conversion is built and
> compiles for real, but nothing provisions a PostgreSQL target or applies the
> converted code; and applications are not repointed at cutover."

---

## Questions you will get, with answers

**"Is the AI actually doing anything, or is this just rules?"**
> "Today, in this demo, one phase has a model seam and it is running on labelled
> static output — I won't pretend otherwise. But be careful what you wish for
> here: the reason this is trustworthy is that AI is bounded to *proposing*. The
> sizing phase shows a proposal being overruled by the rules engine, and that is
> the design working. Where a model earns its place is drafting fixes for 25
> specific findings, and classifying import failures the rules don't recognise.
> Both are wired and gated; neither is claimed as running."

**"What happens on a 1 TB database?"**
> "Data Pump stops being the right answer and DMS becomes it — full load plus
> change data capture, with a replication instance. Discovery and assessment are
> unaffected; they read the catalogue, not the data. The sizing phase gets *more*
> accurate with more real utilization. The honest change is that cutover strategy
> becomes the whole conversation, because a full-outage window on a terabyte is
> usually unacceptable — which makes those two archivelog findings the first thing
> we'd fix, not a footnote."

**"Where is the DMS and SCT report?"**
> "It is the last stage on the rail — **10 · Report** — and also the **Report**
> link in the header. One page in the two shapes you know:
> the SCT assessment report — what share of the stored code converted
> automatically, action items by complexity — and the DMS pre-migration
> assessment — primary keys, archivelog, supplemental logging, LOBs, unsupported
> types, what DMS will not migrate, and what each failure blocks. On this
> estate: 75% of the stored code converted automatically, 38 action items, 12
> DMS checks with 4 failing, and both replication paths blocked with the
> blocking rule named. Every number is read from a record; the page says at the
> top and bottom that AWS's tools did not produce it."

If they press on tables: "Table DDL is not converted here — on a heterogeneous
path DMS Schema Conversion does that. The report says so rather than quoting a
percentage for work that did not happen."

**"How is this different from AWS's own accelerator?"**
> "Different problem, one deliberate overlap. Theirs targets heterogeneous —
> Oracle to PostgreSQL or MySQL — and is strongest at schema DDL and
> application SQL conversion. Ours is homogeneous, and the weight is on
> assessment, licensing, safety gating and validation. The overlap is the part
> their documentation lists as manual: PL/SQL. We convert it, gate it five ways,
> and compile it for real. What we do not do yet is provision the PostgreSQL
> target or apply the result — that is a separate decision."

**"Could this run against our estate tomorrow?"**
> "Discovery and assessment, yes — they need a read-only account and no AWS at
> all, and we proved portability by running the whole thing against a second
> estate with no code changes. Everything from provisioning onward needs your AWS
> account and your approvals. The one thing I'd want first is a utilization feed,
> because without measured load the sizing is a capacity floor, not a
> recommendation."

**"What is it wrong about?"**
> "Semantic rules are heuristics and should be treated as hypotheses — one has
> misfired twice on money columns. Recall is only meaningful where we know the
> answer key. The blast-radius map is hand-maintained for four rules; every other
> critical blocks everything by default, which is safe but blunt. And waivers
> don't expire, which real governance would fix."

---

## Change log

**2026-09-12 — created.** Written against phase docs as of this date: Phase 8
validated with zero mismatches, Phase 9 certificate refusing on a run-id
mismatch (target `6e48d16a`; records on disk were `ee35e2bf` at time of
writing, having moved on from the `83eadb57` the Phase 9 doc names), Phase 4 on
static fixtures with Bedrock invoke blocked. Includes the DMS/SCT and AWS Oracle Modernization Accelerator
positioning, which is a scope boundary rather than a built capability — re-check
before repeating it.
