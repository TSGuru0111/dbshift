# Phase 2 — Assess

> **Latest update — 2026-09-10.** **Validated against a second, independently
> seeded estate: `DBMIG_TELCO`, 5.67 GB, 14 defects, 14/14 recall with severity
> exact on all 14.** Six rules fired for the first time ever (PERF-004, PERF-008,
> DQ-005, DQ-008, DQ-009, OPS-004). Getting there exposed two real defects: five
> rules crashed with `no such column` because empty datasets produced
> column-less tables, and `_report` crashed formatting a `None` recall. Both
> fixed — see the change log. The answer key and reference schema are now
> overridable (`DBSHIFT_ANSWER_KEY`, `DBSHIFT_REFERENCE_SCHEMA`), defaulting to
> `DBMIG_APP`, so a second estate needs no code change.
>
> Recall is still only measured against the estate the defects were seeded into.
> On any other database the answer key returns `applicable: false` with a reason
> instead of 0%, which would have read as a broken engine. Rules can also be
> switched off and custom rules added from the console; the CLI ignores those and
> always runs the shipped catalogue.

## Purpose

Turn the inventory into findings, scored. Every rule is a row in a table with a
SQL predicate, a severity and a remediation level. **Nothing is judged at
runtime** — which is why the same estate always scores identically, and why a
model cannot quietly change a verdict.

## What actually happens

1. **Load.** `assess/loader.py :: load_run()` reads the run's JSON into SQLite —
   one table per dataset, named as the future `source_inventory.*` tables. Then:
   - **24 indexes** on the join keys the catalogue actually uses, plus `ANALYZE`.
     Without them the `NOT EXISTS` and self-join rules are quadratic.
   - **Three analysis views** — `v_user_tables`, `v_user_objects`,
     `v_user_columns` — which centralise "what counts as a user object". Skipping
     them is how you get hundreds of false positives on `DR$` internals.
2. **Install the rules.** `engine.install_rules()` writes the catalogue into a
   `rules` table. Adding rule 51 is inserting a row.
3. **Evaluate.** For each rule, run its predicate; every returned row becomes a
   finding carrying **that rule's** severity and remediation level. A rule that
   errors is recorded and the run continues.
4. **Group.** `engine.group_findings()` collapses findings to one issue per rule
   with an occurrence count. At 90 objects one-per-object is readable; at 10,000
   it is 7,500 rows nobody triages.
5. **Score.** Five category scores plus an overall, and the answer-key comparison.

### Scoring

Penalty accrues **per rule, not per finding**, scaled logarithmically by how many
objects the rule hit — a rule firing on 340 tables is one issue with a wide blast
radius, not 340 issues. The score then decays exponentially:

```
score = 100 × e^(−penalty / 60)
```

Overall is the mean of the five, and **any critical finding caps it at 60**.

## Inputs / Outputs

| | |
|---|---|
| Input | A collector run directory |
| Input | `assess/rules.json` — 50 rules |
| Input | `assess/answer_key.json` — the 8 seeded defects |
| Output | `assess/output/assessment.json` — scores, issues, findings |
| Output | `assess/output/assessment.sqlite` — the loaded estate |
| Output | `assess/output/report.html` via `assess.report` |

## Design decisions

**Rules are data, not code.** A SQL predicate, a severity, a level, a rationale.
Extending the engine is inserting a row, and the whole catalogue is diffable.

**Severity comes from the rule.** Never inferred, never model-decided. This is
what makes the score reproducible.

**SQLite stands in for Aurora.** It gives real SQL locally with no service, and
the predicates port to the metadata repository later with little more than a
dialect change. It is the piece that makes Discover→Assess demonstrable with no
AWS account.

**Linear scoring was tried and abandoned.** Subtracting severity weights from 100
saturates: past ~100 penalty every busy category reads 0, making a bad category
indistinguishable from a catastrophic one.

**Unmapped findings are not called false positives.** Most describe the estate
accurately. A real false-positive rate needs human triage and is not claimed.

## Known limits

- **Structural rules port anywhere; semantic ones do not.** "No primary key" is
  true everywhere. `DQ-002`'s near-unique heuristic already misfired twice on
  money columns. At a client, semantic rules should be presented as **hypotheses
  to confirm**, not findings.
- **Recall only means something on the reference estate.** Elsewhere it reports
  `applicable: false`.
- **Seeded defect 7 is not in the database.** The seed script appends `CHR(146)`;
  on AL32UTF8 that byte is dropped during concatenation, so the UPDATE changed
  nothing. It is excluded from the recall denominator rather than counted as a
  miss. See [`../04-defects.md`](../04-defects.md).
- **Data-quality rules stay quiet without row-read grants.** `DQ-010` says so
  explicitly rather than letting silence look like a clean bill of health.

## How to run it

```bash
python -m assess.run
python -m assess.report
```

Console: **Phase 2 - Assess**, with rule-by-rule progress.

## Current result on the reference estate

50 rules → **66 findings, 36 issues**. Overall **50**, capped by 5 criticals.
Recall **7 of 7 detectable, severity exact on all 7**.

## Change log

**2026-09-10** — Answer key gated on the reference schema. Rule toggles and
custom rules from the console. `assess.report` made tolerant of a
console-produced `assessment.json`, which omitted `source` and `assessed_at_utc`.

**2026-09-10 (console)** — **The console reported no recall at all on
`DBMIG_TELCO`** — `0/0`, `applicable: false` — while the CLI reported 14/14 on
the same findings. Scores, findings and rule count matched exactly; only recall
differed.

Cause: `DBSHIFT_ANSWER_KEY` / `DBSHIFT_REFERENCE_SCHEMA` are read at import.
That fits the CLI, which is launched per estate with its environment set, but a
long-running console serves whichever estate the operator connects to and cannot
re-read an env var it inherited at start-up. It therefore loaded the default
`DBMIG_APP` key and correctly declared it inapplicable.

The graceful-degradation path was working exactly as designed; the console simply
had no way to select a different key. `scoring.KNOWN_ANSWER_KEYS` now maps schema
to key and `resolve_answer_key(owners)` picks by the owners present in the
findings. An explicit `DBSHIFT_ANSWER_KEY` still wins, so the CLI can pin a key
that is not in the registry. **Add a row to that dict whenever an estate gains a
seeded-defect key.**

Worth noting for what it says about the design: everything except recall agreed
to the digit across two independent execution paths.

**2026-09-10 (later)** — **Scored against a second, independently-seeded estate
(`DBMIG_TELCO`, 5.67 GB, 14 defects): 14/14 recall, 14/14 severity exact.** Six
rules fired for the first time in this project's history — PERF-004, PERF-008,
DQ-005, DQ-008, DQ-009, OPS-004 — because no `DBMIG_APP` defect exercised them.

Two engine defects were found doing it:

- **Five rules died with `no such column`.** `loader.py` infers SQLite columns
  from the data rows, so a dataset with no rows produced a table holding only
  `collector_run_id`, and every rule referencing a real column failed to parse.
  `EMPTY_DATASET_COLUMNS` existed for precisely this — the 2026-09-08 entry below
  records the same bug — but it is a hand-maintained list that covered 5 of the
  datasets that can be empty. An estate with no scheduler jobs, queues, XML
  schemas or extra role grants has four more, and OPS-005, RDS-006, RDS-007,
  RDS-009 and SEC-004 all failed. **The list is no longer the primary mechanism:**
  the collector now records the columns its query returned (phase 1), and the
  loader prefers those. `EMPTY_DATASET_COLUMNS` remains only as a fallback for
  runs collected before that existed.
- **`assess/run.py` crashed formatting the answer-key block** whenever the key
  did not apply — `score_against_answer_key` correctly returns
  `applicable: false` with `recall: None` on a non-reference estate, but
  `_report` formatted it as a percentage regardless.

`REFERENCE_SCHEMA` and the answer-key path are now overridable via
`DBSHIFT_REFERENCE_SCHEMA` and `DBSHIFT_ANSWER_KEY`, defaulting to the shipped
`DBMIG_APP` key so existing behaviour is unchanged. They must be set together.

**2026-09-09** — Findings grouped into issues (68 → 36). 24 SQLite indexes on
rule join keys. `loader.load_run` was leaking its connection — invisible in a CLI
that exits, but in a long-running server it kept the file locked and the next
run's `unlink()` failed on Windows.

**2026-09-08** — Built. 49 rules, 100% recall. Scoring rebuilt from linear to
log-volume plus exponential decay. Three rule bugs fixed: a sequence `MAXVALUE`
of 28 nines overflowed SQLite's integer, `limit` is reserved in SQLite, and empty
datasets produced tables with no columns so rules could not parse.
