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

## Two targets, chosen by the client

**Decision 2026-09-14 — RDS for PostgreSQL is a supported target.** This
reverses the earlier position that PostgreSQL was a capability only. There are
now two paths, and Phase 3 makes the choice explicit:

| Path | Target | Kind |
|---|---|---|
| Homogeneous | Amazon RDS for Oracle | Built, proven, cut over 2026-09-12 |
| Heterogeneous | Amazon RDS for PostgreSQL | Phase 3 decides it; 4b converts the code; 6–9 in progress |

**Aurora is still out of scope, and RDS for PostgreSQL is not Aurora.** They are
different products. The heterogeneous path targets the plain managed instance.

The consequences run through every later phase, and none of them is optional:

- **Data movement changes engine.** Data Pump writes an Oracle-only format, so
  the heterogeneous path cannot use it. DMS is the only route, and a
  replication instance bills for as long as it runs.
- **Table, index and constraint DDL must be converted.** Today the report says
  truthfully that DBShift does not convert table DDL because DMS Schema
  Conversion does it. With a real PostgreSQL target, that is a gap to close.
- **Phase 4b must gain an apply path.** It compiles into a throwaway container
  and rolls everything back, which was right when there was no target. With one,
  applying converted code is a safety change that needs its own approval gate.
- **Validation becomes cross-engine.** Comparing Oracle to PostgreSQL is not
  comparing Oracle to Oracle: empty string versus NULL, date precision, number
  scale and identifier casing all differ, so checksums need normalisation.

## Deliberately out of scope

Do **not** re-add these without an explicit decision:

- **Aurora PostgreSQL / Aurora MySQL** — clustered products with their own
  sizing and failover model; RDS for PostgreSQL covers the heterogeneous path
- **Amazon Redshift** — source is OLTP; nothing to route. Redshift is for
  analytical workloads only, detected by star schemas, fact tables and ETL
  patterns — never recommended because a database is merely large
- **Oracle Database@AWS** — no RAC/Exadata source to migrate. An estate that
  depends on RAC blocks *both* supported paths, and `sizing/target.py` says so
- **SQL Server as a source** — one source engine
- **Bedrock Knowledge Base** — beta uses static prompt context instead

Keep `assessment.object_mapping` in the metadata model. On the heterogeneous
path it is no longer theoretical: it is where source-to-target object mapping
belongs.

## Known cost traps

- **OpenSearch Serverless** — Bedrock's Quick-create default provisions a
  collection with a 2-OCU floor (~$345/mo at zero traffic) that *survives
  deletion of the Knowledge Base*. Use pgvector on Aurora if a vector store is
  ever needed.
- **RDS auto-restart** — a stopped RDS instance restarts itself after 7 days.
- **CloudWatch Logs** — default retention is never-expire. Set 7 days at
  creation on every log group.
