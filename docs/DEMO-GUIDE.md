# DBShift — how every phase works

A demo guide: what each phase does, how the code does it, and what to say when
someone asks a hard question. Written against the code as it stands on
2026-09-15, verified by a full ten-phase run on DBMIG_TELCO against a live RDS
PostgreSQL target in account 280646578374.

For a one-page version, see [DEMO-CARD.md](DEMO-CARD.md).

---

## The one idea

> **AI proposes. Deterministic rules decide. A person approves anything irreversible.**

Everything below is a consequence of that sentence. If you remember nothing else,
this is the sentence that sells the product, and every phase is built to make it
literally true rather than a slogan.

Three properties fall out of it, and they are what make the tool different from a
migration report:

| Property | What it means in the code |
|---|---|
| **Read-only until a person says otherwise** | Phases 1–5 open no write transaction anywhere. Phase 4b writes to PostgreSQL inside a transaction it then rolls back. |
| **Nothing is inferred silently** | Every finding cites the catalogue view it came from. Where evidence is missing, the phase says "not comparable" rather than guessing. |
| **A refusal explains itself** | Every blocked path names what blocked it and what would clear it. |

**Scale:** ~18,000 lines of Python across 14 modules, plus a single-file console
(`web/static/index.html`) and a FastAPI server (`web/server.py`).

---

## The shape of a run

```
      Oracle source (read-only)              AWS (account 280646578374)
             │                                        │
   1 Discover│── catalogue → SQLite mirror            │
   2 Assess  │── 51 rules over the mirror             │
   4b Convert│── PL/SQL → PL/pgSQL, compiled on PG    │
   3 Target  │── client picks the engine              │
   4 Remediate── a fix per finding (model + rules)    │
   5 Gate    │── may this proceed, and what exactly   │
             │                                        │
   6 Provision──────────────────────────────────────→ CloudFormation → RDS
   7 Migrate ──────────────────────────────────────→ AWS DMS
   8 Validate│←───── five levels, both sides ────────→ RDS
   9 Cutover │── nine requirements → certificate      │
  10 Report  │── built from this run's records only   │
```

**Why 4b runs before 3.** Phase 3 asks "how much work is the PostgreSQL path?"
The honest answer comes from compiling the stored code, not from estimating it.
So 4b compiles first and 3 consumes a measured number. The rail displays them in
label order; the *evidence* order is what matters.

---

## Phase 1 — Discover

**`collector/`** · 6 files, ~730 lines

### What it does
Connects to Oracle as a read-only account and copies the catalogue — not the
data — into a local SQLite file.

### How the code works
`collector/probes.py` holds a list of named probes, each a SELECT against a
`DBA_*` or `V$` view. `collector/run.py` executes them, and `collector/writer.py`
writes each result set to SQLite plus a JSON manifest carrying a run id.

`_resolve_owners()` cross-checks the configured schema list against what the
database actually has, using Oracle's own `ORACLE_MAINTAINED='N'` flag:

> *"Anything marked 'N' that is not in the configured list gets reported rather
> than silently included or silently dropped — schema drift should be visible,
> not inferred."*

### Say this
"Read-only, catalogue only. No table data leaves the source. Everything
downstream reads the mirror, not your production database — so the rest of the
run costs your Oracle box nothing."

### If they ask
- **"What account does it need?"** A read-only login with `SELECT` on the
  catalogue. Nothing more.
- **"How long on a real estate?"** Minutes; it scales with object count, not
  data volume. Telco takes ~30 seconds.
- **"Does it re-run?"** Yes, each run gets its own id. `collector/verify.py`
  compares two runs *of the same estate* to show drift.

---

## Phase 2 — Assess

**`assess/`** · 6 files, ~1,800 lines · **51 rules**

### What it does
Runs 51 deterministic rules over the mirror and produces findings.

### How the code works
`assess/rules.json` holds the rules as data, not code. Each must carry
`rule_id`, `category`, `severity`, `remediation_level`, `title`, `rationale`,
`sql`. `assess/engine.py` validates every rule at load time and **refuses to
start** on a duplicate id, an unknown severity, or a missing field.

Categories: `rds_compatibility`, `data_quality`, `performance`, `security`,
`operational_risk`. Severities: CRITICAL → INFO. Remediation levels: L1–L4.

A rule is just SQL over the mirror. `RDS-001` is a good one to show — it finds
objects named with reserved words, and its `rationale` explains the real cost:

> *"A reserved word used as an identifier must be double-quoted everywhere
> forever. Any tool, ORM or ad-hoc query that omits the quotes fails at runtime
> rather than at migration time."*

`assess/scoring.py` grades the run against `answer_key.json` — a known set of
seeded defects — producing the **recall 100%** figure on screen.

### Say this
"51 rules, and they're data not code — adding a rule is editing JSON. Every
finding cites the catalogue view it came from, so you can argue with it. And we
score ourselves: recall 100% means we found every defect we deliberately planted."

### If they ask
- **"Can we add our own rules?"** Yes — one JSON object. The loader enforces the
  schema so a malformed rule fails loudly at startup.
- **"Why 99 findings from 33 issues?"** An issue is a rule; a finding is a rule
  firing on one object.
- **"Is this AI?"** No. Phase 2 is entirely deterministic. The model appears in
  phase 4.

---

## Phase 4b — Convert PL/SQL

**`convert/`** · 18 files, ~3,960 lines · **the technical centrepiece**

### What it does
Rewrites Oracle PL/SQL as PostgreSQL PL/pgSQL, then **compiles each object on a
real PostgreSQL** and rolls it back.

### How the code works
`convert/rules.py` does deterministic translation. Two details worth naming:

- `_qualify_percent_type()` resolves `%TYPE` references, which PostgreSQL only
  resolves via `search_path`.
- Every converted function gets `SET search_path = {owner}, pg_temp` at function
  level — so it resolves correctly no matter who calls it.

`convert/target.py` creates a **shadow schema** (`dbshift_shadow_`) so a compile
can never collide with applied objects.

Then **five gates** in `convert/gates.py`, in order:

| Gate | Question | Code |
|---|---|---|
| **static** | Does it create what it claims? | `static_check()` |
| **policy** | Only CREATEs — no privilege or data statements | `policy_check()` |
| **parity** | Do the constructs survive? | `parity_check()` |
| **compile** | Does PostgreSQL accept it? | `compile_check()` |
| **approval** | Has a named human approved it? | `approval()` |

The compile gate runs real DDL inside a transaction, captures the SQLSTATE on
failure, and rolls back. `convert/apply.py` is the *only* writer, and it accepts
exactly one status: `APPLIABLE = {"APPROVED"}`.

### Say this
"This is where most tools hand you a report. We rewrite the code and **compile it
on a real PostgreSQL**, then roll it back. Six of eight compiled. One is broken
on the source today — we won't migrate broken code. Nothing was applied."

### If they ask
- **"How do you know the conversion is right?"** We don't claim it's right — we
  claim it *compiles*, which is more than a report gives you, and then a human
  approves it. The approval gate cannot be skipped.
- **"What about the two that didn't?"** One has compile errors on the source
  today. One is a package spec absorbed into its body. Both are stated, not
  hidden.
- **"Does it touch our database?"** The compile runs on a scratch PostgreSQL in
  a rolled-back transaction. The Oracle source is never written to at all.

---

## Phase 3 — Target & Sizing

**`sizing/`** · 10 files, ~1,840 lines · **the commercial moment**

### What it does
The **client chooses**: keep Oracle on RDS for Oracle, or move to RDS for
PostgreSQL and end the licence.

### How the code works
`sizing/target.py` assesses both paths from the estate's own evidence, splitting
findings two ways:

- **`PG_BLOCKING_FEATURES`** — the path is *refused*. RAC, Label Security,
  Database Vault, Advanced Queuing. Each carries a reason:
  > *"RAC is a shared-storage clustering architecture. PostgreSQL has no
  > equivalent... an estate depending on RAC belongs on Oracle Database@AWS,
  > which is out of scope."*
- **`PG_EFFORT_FEATURES`** — work with a known shape. Partitioning, Advanced
  Compression. Weighted, not fatal.

`choose()` refuses a blocked path outright. `_recommend()` is deliberately
conservative: where stored code is unmeasured it returns **no recommendation**
with "insufficient evidence" rather than a confident guess.

Sizing itself is a **capacity floor**, and the UI says so — without a utilization
feed there is no history to take a percentile of.

### Say this
"The client chooses. And notice the ordering: the effort figure comes from 4b,
which *measured* it by compiling. Not an estimate. A blocker refuses the path
outright; effort is work with a known shape. We don't blur those together."

### If they ask
- **"What if we have RAC?"** PostgreSQL is refused, and it says why. That's a
  real answer, not a warning buried on page 40.
- **"Is this sizing trustworthy?"** It's a floor, and the screen says so. Feed it
  an AWS OLA or Migration Evaluator export and it becomes load-derived.
- **"Can we change our mind?"** Yes — and any sizing already on screen is marked
  "path changed — re-run" rather than left looking current.

---

## Phase 4 — Remediate

**`remediate/`** · 8 files, ~1,070 lines · **the AI moment**

### What it does
Proposes a fix for every finding. Templates where there's one right answer; the
model where judgement is needed.

### How the code works
`remediate/generate.py` has three modes — `off`, `static`, `live`. In `live`,
`bedrock_fix()` calls Bedrock and returns the same shape as a template: `sql`,
`rollback_sql`, `explain`, `caveat`.

The important part: **`model_mode` only chooses where candidate text comes from.
The gates decide status regardless.** A model-authored fix goes through exactly
the same screens as a templated one.

`GenerationUnavailable` is raised when the model declines with empty SQL — **a
correct answer**, routed to a person. You can see these in the output:

> *"model_unavailable: the model found no safe SQL fix: DQ-011 is an
> informational notice that the profiling run sampled only ~4.76% of the
> estimated 21 million rows... There is no data defect to fix."*

Every entry carries `source` (`bedrock`, `template`, `human_authored`,
`never_fix`) and `model_id`.

### Say this
"The model drafted about 18 of these. Now look at the statuses — blocked,
rejected. **None applied.** A model proposes; it never approves. And where it had
nothing safe to say, it said so instead of inventing SQL."

### If they ask
- **"Which model?"** `claude-sonnet-4-6` on Bedrock, both tiers, ap-south-1.
  Named on screen.
- **"What if the model is wrong?"** Then it's blocked or rejected like any other
  proposal. The gates are deterministic even though the model isn't.
- **"Does the count change between runs?"** Yes — 17 or 18. The model is
  non-deterministic; the gates are not. What never changes is that none are
  auto-applied.

---

## Phase 5 — Blocker gate

**`blocker/`** · 4 files, ~300 lines · **small, and the most important**

### What it does
The last deterministic step before anything costs money or touches a target.

### How the code works
The whole design is in the docstring of `blocker/policy.py`:

> *"A binary halt is correct but blunt. `NOARCHIVELOG` does not stop you
> provisioning an instance or running a full-load migration — it stops change
> data capture, and therefore a low-downtime cutover. Saying 'halted' without
> saying *what* is halted sends people to fix the wrong thing."*

So `BLOCKS` maps each rule to its **blast radius** across five downstream phases:
`provision`, `migrate_full_load`, `migrate_cdc`, `validate`, `cutover`.

On telco: OPS-001 (no ARCHIVELOG) and OPS-002 (no supplemental logging) block
`migrate_cdc` and `cutover` — while provision, full load and validate stay clear.

**A rule absent from `BLOCKS` blocks everything** — an unrecognised critical is
never quietly assumed harmless.

Each entry carries `clears_when`, e.g. *"ALTER DATABASE ADD SUPPLEMENTAL LOG DATA
on the source."*

### Say this
"Verdict per phase, not one blanket refusal. CDC and cutover are blocked;
provision, full load and validate are clear. And each blocker says exactly what
clears it. A waiver is possible — but it needs a named person and a real reason,
and both are recorded."

### If they ask
- **"Can we override it?"** Yes, with a named approver and a reason. Never
  anonymously.
- **"Why is supplemental logging critical?"** Without it, redo omits the column
  data DMS needs. *"CDC starts and then applies incomplete changes silently,
  which is worse than not running."*

---

## Phase 6 — Provision

**`provision/`** · 10 files, ~1,690 lines

### What it does
Renders a CloudFormation template for the chosen engine and runs read-only
preflight checks. **Rendering is free; deploying is a separate explicit yes.**

### How the code works
`provision/policy.py` holds the guardrails as constants — `PG_TARGET_MAJOR = "16"`,
`PG_PORT = 5432`, `REQUIRED_TAGS = {"Purpose": "DMA"}`, Single-AZ, 1-day backups,
`DeletionPolicy: Delete`, an 8-hour TTL, master password in SSM SecureString.

Each guardrail has a stated reason — Multi-AZ off because *"a standby doubles the
instance bill"*; deletion protection off because *"the kill switch must be able to
remove it"*.

`provision/render.py` branches on engine, so `listener_port` follows the engine
rather than defaulting to Oracle's 1521.

`provision/preflight.py` runs read-only AWS checks: identity, engine version,
network, quota, stack-name availability, budget, operator IP, template validity.

The **provenance table** is the screen to show: every value in the template says
which phase decided it and why.

### Say this
"Renders and checks — it doesn't deploy. Every value says which phase decided it.
Multi-AZ is off and it tells you why. The instance carries an 8-hour TTL because
a target outliving a working day is a leak."

### If they ask
- **"What does it cost?"** Priced from the AWS public price list at render time.
  db.t3.small in Mumbai: $0.053/hour, about $0.51 for an eight-hour day.
- **"Is it safe on a shared account?"** Everything is tagged `Purpose=DMA` and
  named `dbshift-*`. The kill switch only removes what matches.
- **"Where's the password?"** SSM SecureString. Never in the template, a file, or
  the repo.

---

## Phase 7 — Migrate

**`dms/`** · 8 files, ~2,000 lines · **95 selftest checks**

### What it does
Plans an AWS DMS migration, and names what DMS **leaves behind**.

### How the code works
**Why DMS and not Data Pump:** Data Pump writes an Oracle-only format. The
PostgreSQL path *must* use DMS. This isn't preference, it's a format constraint.

`dms/mappings.py` builds table selection, excluding internals, materialized
views, external tables and queue tables, plus three `convert-lowercase`
transformation rules.

`dms/preflight.py::cdc_possible()` checks archivelog, supplemental logging and
keys before promising CDC.

`dms/residue.py` is the honest part — everything DMS does not finish, routed
three ways:

| Tier | Meaning |
|---|---|
| **RULE** | A deterministic rewrite handles it |
| **MODEL** | Needs judgement → the model tier |
| **PERSON** | *"no target equivalent; someone decides what happens instead"* |

Sequences, views, materialized views, external tables, stored code. Items the
model answers become `MODEL_CONVERTED` with a `model_id` — **and nothing is
applied**.

### Say this
"DMS, not Data Pump — Data Pump writes an Oracle-only format, so it can't serve
the PostgreSQL path. And notice it names what DMS *leaves behind* rather than
pretending the job is done. Sequences, views, stored code — each routed to a
rule, the model, or a person."

### If they ask
- **"Does DMS move everything?"** No, and that's the point of the residue list.
  Most tools go quiet here.
- **"Will CDC work?"** Not on this estate — blocked by OPS-001/OPS-002, which
  phase 5 already said.

---

## Phase 8 — Validate

**`validate/`** · 6 files, ~1,520 lines

### What it does
Five levels of comparison between source and target. **Every statement is a
SELECT.**

### How the code works
`validate/levels.py::LEVELS`:

| # | Level | Question |
|---|---|---|
| 1 | Objects | *"Everything that should exist, does — by name, not by count."* |
| 2 | Structure | Columns, constraints, indexes |
| 3 | Row counts | Exact counts, table by table |
| 4 | Data content | *"A checksum of every row, so equal counts cannot hide changed values."* |
| 5 | Behaviour | What only shows up when the database is used |

**Cross-engine comparison** (`validate/crossengine.py`) is the clever part. Raw
bytes would report failure on every numeric column of a *perfect* migration. So
each row is reduced to a **canonical text form** on both sides — trailing zeros
stripped, CHAR padding trimmed, timestamps in UTC, Oracle's empty-string-is-NULL
given the same marker as a PostgreSQL NULL — and a shared MD5 computed over that.

Note: PostgreSQL uses `trim_scale(...)::numeric::text`, **not** a `to_char`
format mask, which left a trailing dot.

Levels 2 and 5 **do not run** cross-engine, and the screen says why: column types
are deliberately different (NUMBER → NUMERIC), and invalid objects and grants are
Oracle catalogue concepts.

Verdicts are `match`, `expected_difference`, `mismatch`, `not_comparable`. **Only
mismatches fail.** `not_comparable` is never counted as a pass.

### Say this
"Five levels, source against target, every statement a SELECT. The subtle part is
cross-engine comparison — Oracle and PostgreSQL don't store the same value the
same way, so we compare a canonical text form. Otherwise a perfect migration
would show failures on every numeric column and people would stop reading."

### If they ask
- **"Why does it say mismatch?"** Because the target is empty — DMS can't reach
  an on-premises Oracle from AWS. **The system refused to certify a migration
  that hasn't happened.** That's the behaviour you're buying.
- **"What's `unreachable` vs `mismatch`?"** *"`unreachable` means I couldn't
  look. `mismatch` means I looked, and here's what's wrong."* Never conflated.

---

## Phase 9 — Cutover

**`cutover/`** · 4 files, ~780 lines · **your closing argument**

### What it does
Builds a readiness certificate of nine requirements. Then a named approval. Then
the few target-side steps phase 4 set aside.

### How the code works
`cutover/requirements.py` builds nine checks, each `met`, `waived`,
`not_applicable`, or `unmet` — **and an unmet requirement says what would clear
it**.

Three modes in `cutover/run.py`: certificate (read-only, default), `--approve`
(records a named approval), `--execute --confirm <account>` (the only writing
mode).

The approver is taken from the **AWS identity**, never typed into the browser —
`get_caller_identity()`. The screen says so explicitly.

On telco, three are unmet:

1. **Records describe a different run** — the target was built from collector run
   `29c6de79`, this session's records are newer. *"Certifying against another
   run's findings would certify a database nobody assessed."*
2. **Validation ended `mismatch`** — *"A cutover on an incomplete validation is a
   guess."*
3. **Gate blocks on OPS-001, OPS-002.**

And **"There is a way back"** is met: the source is untouched and authoritative,
the target can be destroyed with one kill-switch command.

### Say this
"Nine requirements. It refuses — and every unmet line says exactly what would
clear it. That's the difference between a tool that blocks you and one that tells
you what to do next. And the approver is never typed into a form — it comes from
your AWS identity."

### If they ask
- **"Can we force it?"** Yes, with a named approval recorded against your AWS
  identity. An approval names one collector run and does not carry to another.
- **"What if it goes wrong?"** The source is never written to. Rolling back is
  repointing applications at it.

---

## Phase 10 — Report

**`report/`** · 4 files, ~680 lines

### What it does
An SCT-style assessment built from **this run's records only**.

### How the code works
`report/build.py` loads each phase's output JSON and renders one HTML page. The
server builds it on every request from its own records, so the tiles and the page
cannot disagree.

It reads `target_engine` throughout — early versions hardcoded "Amazon RDS for
Oracle" and "Data Pump moved this estate", which was wrong the moment PostgreSQL
became selectable.

### Say this
"Built from this run only. Nothing in it is written by hand. It knows the target
is PostgreSQL and that DMS is the data path, because it reads the decisions the
phases actually made."

---

## Safety rails (worth a slide of their own)

| Rail | Where |
|---|---|
| **Read-only until approved** | Phases 1–5 open no write transaction |
| **Compile, then roll back** | `convert/gates.py::compile_check` |
| **One writer** | `convert/apply.py`, `APPLIABLE = {"APPROVED"}` |
| **Tagged** | `Purpose=DMA` on every AWS resource |
| **TTL** | 8 hours; *"a target outliving a working day is a leak"* |
| **Kill switch** | `killswitch/` removes only `dbshift*` / tagged resources |
| **No secrets in the browser** | Approver from `get_caller_identity()`; master password in SSM |
| **Source never written** | Rollback is repointing applications |

---

## Running the demo

```powershell
# 1. Console (leave running)
cd "C:\...\dbshift"
$env:AWS_PROFILE='dbshift-bedrock'; $env:AWS_DEFAULT_REGION='ap-south-1'
$env:DBSHIFT_COLLECTOR_PASSWORD='...'; $env:DBSHIFT_PG_PASSWORD='dbshift-local-only'
.\.venv\Scripts\python.exe -m uvicorn web.server:app --host 127.0.0.1 --port 8765

# 2. The narrated walkthrough (visible browser, paced captions)
cd scripts\console-test
$env:DBSHIFT_URL='http://127.0.0.1:8765'
node drive_live_demo.js          # --fast halves pauses; DEMO_PAUSE=6000 slows
```

`node` is not a `.ps1`, so the execution policy does not block it.

### Before you present

- [ ] **Check your IP.** The security group admits specific /32s. A network
      change makes phase 8 report `unreachable`. Fix from the presentation
      network:
      ```powershell
      aws ec2 authorize-security-group-ingress --group-id sg-03c28c88be67a8c9e `
        --protocol tcp --port 5432 --cidr "$(curl -s https://checkip.amazonaws.com)/32" --region ap-south-1
      ```
- [ ] **Use port 8765.** A stale console on another port will 404 on
      `/api/engine` and the PostgreSQL button will look broken.
- [ ] Local Oracle (1521) and PostgreSQL (5432) running.
- [ ] `python -m bedrock.verify --quiet` → 2/2 tiers.

### Expected results

| Phase | Expected |
|---|---|
| 1 | ~30–40s, run id |
| 2 | 99 findings / 33 issues, recall 100% |
| 4b | 8 objects, **6 compiled and rolled back** |
| 3 | PostgreSQL chosen and sized |
| 4 | 99 planned, ~17–18 model-drafted, **none applied** |
| 5 | **Halt**, per-phase verdicts |
| 6 | Stack rendered with provenance |
| 7 | DMS planned, residue named |
| 8 | **mismatch** — correct |
| 9 | **not ready**, three unmet, each with a remedy |
| 10 | Report built |

---

## The three hard questions

**"Why is validation failing?"** — *the best question you'll get.*
"Because the target is empty. DMS can't reach an on-premises Oracle from AWS. The
system refused to certify a migration that hasn't happened. That's the product."

**"So the AI isn't doing much?"**
"The AI drafted 18 fixes and converted the residue. Then deterministic gates
rejected or blocked every one of them. That's deliberate. An AI that could
approve its own SQL against your production database is a liability, not a
feature."

**"How is this different from AWS SCT and DMS?"**
"SCT gives you a report. We compile the converted code on a real PostgreSQL
before it counts. DMS moves data and goes quiet about what it left behind — we
enumerate the residue and route each item. And neither one refuses to cut over
when the evidence isn't there. That refusal is the whole product."
