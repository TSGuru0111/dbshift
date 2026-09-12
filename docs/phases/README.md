# Phase documentation

One file per phase. Each one explains, in detail, **what actually happens** when
that phase runs — not what it is supposed to do one day.

## The working rule

**Before changing a phase, read its file. After changing a phase, update it.**

That is the whole convention, and it only works if it is done every time. A file
that is one change out of date is worse than no file, because it will be trusted.

When you make a change:

1. **Read the phase file first.** It records why the code is shaped the way it
   is. Several decisions here look arbitrary until you know what went wrong the
   first time — the silent seeding failure, the leaked SQLite handle, the
   hardcoded schema list. Re-deriving those costs more than reading.
2. **Make the change.**
3. **Update two sections**: `Latest update` at the top, and add a dated row to
   `Change log` at the bottom. Say what changed and *why*, not just what.
4. **If the change alters behaviour**, update `What actually happens` too. That
   section must describe the code as it is now.

## Structure of each file

| Section | What belongs in it |
|---|---|
| **Latest update** | Date, what changed, and what a reader most needs to know now |
| **Purpose** | What this phase is for, in two or three sentences |
| **What actually happens** | The real sequence, step by step, naming files and functions |
| **Inputs / Outputs** | What it consumes and what it produces, with paths |
| **Design decisions** | The non-obvious choices, each with its reason |
| **Known limits** | What it does *not* do, and what would be wrong to assume |
| **How to run it** | CLI and console |
| **Change log** | Dated entries, newest first |

## The phases

| # | Phase | State | File |
|---|---|---|---|
| 1 | Discover | ✅ built | [phase-01-discover.md](phase-01-discover.md) |
| 2 | Assess | ✅ built | [phase-02-assess.md](phase-02-assess.md) |
| 3 | Size & Edition | ✅ built | [phase-03-size-edition.md](phase-03-size-edition.md) |
| 4 | Detect & Remediate | ◐ plans only | [phase-04-remediate.md](phase-04-remediate.md) |
| 5 | Blocker gate | ✅ built | [phase-05-blocker-gate.md](phase-05-blocker-gate.md) |
| 6 | Provision | ✅ built, deployed and verified 2026-09-11 | [phase-06-provision.md](phase-06-provision.md) |
| 7 | Migrate | ✅ built and run 2026-09-11 — full load, 11/11 tables match | [phase-07-migrate.md](phase-07-migrate.md) |
| 8 | Validate | ◐ built; first run pending | [phase-08-validate.md](phase-08-validate.md) |
| 9 | Cutover | ⬜ not started | — |
| 10 | Report | ◐ partial — the HTML report covers 1–3 | — |

`docs/02-architecture.md` holds the cross-phase design and the scope decisions.
These files hold the detail of each phase in isolation.
