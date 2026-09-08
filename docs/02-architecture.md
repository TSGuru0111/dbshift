# Architecture

## Ten phases

1. **Discover** — inventory objects, feature usage, utilization percentiles
2. **Assess** — ~45 rules produce findings; five sub-scores
3. **Size & Edition Decision** — SE2 license-included vs EE BYOL, instance
   class, storage. *Model proposes with rationale; rules engine validates and
   can override.* This is the only place a model makes a judgement call.
4. **Detect + Remediate** — root-cause each finding; four levels; max 3 retries
5. **BLOCKER GATE** — `has_critical_blockers` halts the run
6. **Provision** — CDK renders the RDS instance from the assessment record
7. **Migrate** — tables + PKs, then DMS full load, then CDC, then FKs,
   secondary indexes, code, triggers, jobs, grants
8. **Validate** — five levels; object-level reconciliation
9. **Cutover** — CDC lag check, readiness certificate, human approval
10. **Report** — object/table/column change log, scores, audit trail

*(Post-cutover optimization is v2.)*

## Why FKs and indexes are deferred until after the data load

Validating millions of foreign key references row-by-row during a bulk insert
is the single largest cause of a load that should take minutes taking hours.
Triggers are held back for the same reason plus correctness — a trigger firing
during a bulk load corrupts row counts and may write audit rows that validation
then flags as a mismatch.

## Remediation safety levels

| Level | Meaning | Example |
|---|---|---|
| L1 | Safe auto-fix, no prompt | Add missing index on an FK |
| L2 | Auto-generated, **requires approval** | Datatype precision change |
| L3 | Human authors the fix | Business logic change |
| L4 | Never auto-fixed | Anything dropping data |

The level is assigned **by rule, not by the model**. A model returning 99%
confidence on a business-logic rewrite still lands in L3. Every generated fix
carries rollback SQL or it is rejected at generation time.

## Five gates for generated SQL

`static check -> policy check -> syntax check -> dry run on rehearsal sandbox
-> approval -> production`

## Deliberately out of scope

Do **not** re-add these without an explicit decision:

- **Aurora PostgreSQL** — would reintroduce schema conversion
- **Amazon Redshift** — source is OLTP; nothing to route
- **Oracle Database@AWS** — no RAC/Exadata source to migrate
- **DMS Schema Conversion** — same engine both sides converts nothing
- **Bedrock Knowledge Base** — beta uses static prompt context instead

Keep `assessment.object_mapping` in the metadata model even though nothing maps
in a homogeneous migration. It costs nothing and is what allows a heterogeneous
target to be added later without reworking the schema.

## Known cost traps

- **OpenSearch Serverless** — Bedrock's Quick-create default provisions a
  collection with a 2-OCU floor (~$345/mo at zero traffic) that *survives
  deletion of the Knowledge Base*. Use pgvector on Aurora if a vector store is
  ever needed.
- **RDS auto-restart** — a stopped RDS instance restarts itself after 7 days.
- **CloudWatch Logs** — default retention is never-expire. Set 7 days at
  creation on every log group.
