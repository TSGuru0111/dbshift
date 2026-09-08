# DBShift AI

AWS-native, human-in-the-loop AI agent for migrating on-premises Oracle to
Amazon RDS for Oracle. Company accelerator project.

**Start here:** `CLAUDE.md` for the map, then `docs/00-README.md` for the
documentation index.

## Layout

```
CLAUDE.md                  Read automatically by Claude Code. The map.
docs/                      All reference material, numbered in reading order
scripts/oracle-source/     SQL that builds the synthetic source estate
data/                      Golden Data Pump dump (gitignored)
collector/                 Python discovery collector — next to build
infra/                     CDK / CloudFormation — later
```

## Status

Source estate built and verified: 1.03 GB, 90 objects, 8 seeded defects,
golden snapshot exported. Discovery collector is the next task — see
`docs/08-next-tasks.md`.
