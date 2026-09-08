# Project brief

## What

**DBShift AI** — an AI agent that discovers, assesses, remediates, migrates and
validates an on-premises Oracle database into Amazon RDS for Oracle, with a
human approval gate before anything irreversible.

## Why it exists

It is a **company accelerator**: a demonstrable capability shown to prospective
clients to prove we can run Oracle-to-AWS migrations. It is not a migration
being performed for a real customer. That framing drives several decisions —
notably that the synthetic source estate is *designed to contain problems*, so
the agent visibly finds them.

## What makes it more than a script

A homogeneous Oracle-to-Oracle migration needs no schema conversion and no
data-type mapping. The bytes move themselves. The value is in everything
around that:

1. **Discovery** — inventory ~90 objects nobody could review by hand
2. **Assessment** — ~45 deterministic rules producing scored findings
3. **Licensing & TCO engine** — SE2 vs EE BYOL, core counts, 3-year TCO.
   This reproduces the outputs of an AWS OLA engagement in hours, not weeks.
4. **Remediation** — four safety levels, generated SQL that must pass five
   gates before it touches anything
5. **Validation** — five levels from row counts to business-rule checks

## Architectural spine

**AI proposes. Deterministic rules decide. Humans approve anything irreversible.**

Bedrock never executes SQL directly. Step Functions owns execution. Where the
model and the rules engine disagree, the rules win and the disagreement is
logged as evidence.

## Success criterion

A measured recall figure against `04-defects.md` — e.g. "detected 7 of 8 seeded
defects, 1 false positive". That number is the most persuasive artifact this
project can produce.
