# DBShift AI — Project Context

> This file is read automatically by Claude Code at the start of every session.
> Keep it short and current. Detail lives in `docs/`; this is the map.

## What this project is

An AWS-native, human-in-the-loop AI agent that migrates an on-premises Oracle
database to Amazon RDS for Oracle. Built as a company accelerator — the
deliverable is a demonstrable capability, not a one-off migration.

**Scope is deliberately narrow.** One source engine, one target, ten phases.
Aurora PostgreSQL, Redshift and Oracle Database@AWS are explicitly out of scope.
See `docs/02-architecture.md` for why, and do not re-add them.

## Current state

| Item | Status |
|---|---|
| Source Oracle estate | ✅ Built, 1.03 GB, 90 objects, verified |
| Seeded defects | ✅ 8 defects in place, documented |
| Golden snapshot | ✅ `data/dbmig_golden.dmp` |
| Discovery collector | ⬜ **Next task** |
| Assessment engine | ⬜ Not started |
| Everything AWS-side | ⬜ Not started — no AWS account yet |

## Where things are

- `docs/` — all reference material, numbered in reading order
- `scripts/oracle-source/` — the SQL that builds the source estate
- `data/` — golden dump (gitignored if large)
- `collector/` — Python discovery collector (to be built)
- `infra/` — CDK / CloudFormation (later)

## Working agreements

- **Read `docs/04-defects.md` before touching assessment logic.** It is the
  answer key: 8 known defects with expected severities. Any assessment work is
  measured against it.
- **Don't widen scope.** If a change implies a second migration target, a new
  AWS service, or a new phase, flag it rather than building it.
- **Verify, don't assume.** This project has already lost time to silent
  failures (partial script runs, wrong container, disabled substitution
  variables). Prefer a check query over an assumption.
- Costs matter — this runs on ~$100 of AWS credit. See `docs/06-cost-model.md`
  before provisioning anything.
