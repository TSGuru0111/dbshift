# Phase 9 — Cutover

> **Latest update — 2026-09-12 (later): cut over.** With the local phases
> re-run for the target's own collector run (`6e48d16a`), the certificate met
> every requirement but the gate; the operator started the stopped instance,
> **accepted `OPS-001` and `OPS-002` by name** (`guru.ts@ganitinc.com`, 07:11 UTC,
> reason recorded in `cutover/output/approvals.jsonl`), and the one target-side
> step ran: `DBMS_SCHEDULER.ENABLE('DBMIG_APP.JOB_REFRESH_LOAN_SUMMARY')` —
> **applied 1, failed 0**, recorded in `cutover/output/cutovers.jsonl` as
> `cut_over`. The instance was stopped again immediately after. The
> declared gaps stand: no application is repointed, the database link is dead,
> the source profile was not copied, and the source remains authoritative.
> The earlier not-ready runs are kept in `cutover/output/runs/` — a demo can
> show the refusal and then the certificate.
>
> **Previous — 2026-09-12. Built; the certificate correctly refuses to
> issue.** Phase 9 produces a **readiness certificate**: eight requirements, each
> *met*, *waived* by a named person, *not applicable* with the reason, or *unmet*
> with what would clear it. Against the current estate it comes back **not
> ready**, for three honest reasons: the records on disk describe collector run
> `83eadb57` while the target was built from `6e48d16a`; the blocker gate blocks
> cutover on `OPS-001` and `OPS-002`; and the target is stopped. Offline
> self-test **39/39**. Nothing has been cut over.
>
> The architecture's **CDC lag check does not apply here and says so**: there is
> no replication to measure, because CDC is itself blocked. That makes this a
> full-outage cutover, which the certificate states as a consequence rather than
> scoring as a pass.

## Purpose

Phase 8 established that the target matches the source. Phase 9 answers the only
question left: *may we declare it live, and who says so?* It is the point where a
migration stops being reversible in practice, so the phase is built to refuse.

## What actually happens

`cutover/run.py :: certificate()` reads the four records, the provision plan and
the **instance's own `collector_run_id` tag**, then evaluates
`cutover/requirements.py :: build()`. Nothing is written to either database.

| # | Requirement | How it is decided |
|---|---|---|
| 1 | **The records describe the estate on the target** | All four records' `collector_run_id` must equal the tag on the instance |
| 2 | **Phase 8 validated this target** | A report for *that run*, status `validated`, **zero** mismatches and zero not-comparable, less than 24 hours old |
| 3 | **The blocker gate allows cutover** | `by_phase.cutover.blocked_by` is empty, or a named approval accepts exactly those blockers |
| 4 | **Change data capture has caught up** | *Not applicable* when CDC is blocked, with the outage-window consequence spelled out |
| 5 | **The target is running** | The instance is `available` |
| 6 | **Phase 4's cutover steps are known** | Every `target_runbook` step whose `applies_to_phase` is `cutover` |
| 7 | **What is knowingly unfinished is declared** | Phase 4's `post_cutover_note` artefacts, plus the dead database link, the profile left behind, and the applications this tool does not repoint |
| 8 | **There is a way back** | The source is untouched; the target can be destroyed by the kill switch |

`ready` is true only when every requirement is met, waived or not applicable.

**Approval** (`--approve`) takes the approver from the **AWS caller identity**,
never from a form, requires a reason of at least 15 characters, and refuses to
run at all while any requirement other than the gate is unmet — an approval may
accept known blockers, not paper over a broken precondition. It binds to one
collector run and does not carry to another.

**Execution** (`--execute --confirm <account>`) re-builds the certificate, then
requires `ready`, an approval on record, and the account id typed back. It runs
only statements beginning `BEGIN DBMS_SCHEDULER.` or `BEGIN DBMS_MVIEW.`;
anything else in the plan file is refused and recorded as a failure rather than
executed. On this estate that is exactly one statement: re-enabling
`DBMIG_APP.JOB_REFRESH_LOAN_SUMMARY`, which Phase 7 disabled before the load.

## Inputs / Outputs

| | |
|---|---|
| Input | the four records, `provision/output/provision_plan.json`, `validate/output/validation_report.json` |
| Input | the target's `collector_run_id` tag — the authority on which estate is there |
| Output | `cutover/output/certificate.json`, plus `output/runs/<when>-<ready\|not-ready>.json` |
| Output | `cutover/output/approvals.jsonl`, `cutover/output/cutovers.jsonl` (append-only) |
| Exit code | `0` only when the certificate is ready |

## Design decisions

**The certificate refuses on a run-id mismatch, and that is the current state.**
The records describe `83eadb57`; the target holds `6e48d16a`. Issuing anyway
would certify a database against findings nobody produced for it. Clearing it
means re-running assess, remediate and the gate for the target's run.

**Not applicable must say why.** A CDC lag check that quietly passed because
there is no CDC would be the same class of bug as Phase 8's "validated" over
eleven failed connections. It reports *not applicable*, names the blockers, and
states the consequence: everything written to the source after the export is not
on the target.

**The verdict is not read from a status string alone.** Requirement 2 re-checks
the mismatch and not-comparable counts even though `validated` already implies
both are zero — because on 2026-09-12 that exact string was wrong.

**Approval names a person, from the identity.** Consistent with Phase 6's deploy.
A name typed into a browser form is not an approval.

**Only scheduler and refresh calls may execute.** The steps come from a JSON file
on disk. Executing arbitrary SQL from it would make that file a remote-code path
into a production database.

**Declared gaps are read from Phase 4, not restated here.** The
`post_cutover_note` artefacts are carried onto the certificate verbatim, so the
two cannot drift.

## Known limits

- **Nothing has been cut over.** Every path except certificate-building is
  unexercised against the live target.
- **Applications are not repointed.** No DNS, no connection strings, no
  credentials. The certificate says so rather than implying otherwise.
- **No outage-window enforcement.** The certificate states that a full-outage
  cutover needs one; it cannot tell whether the source is still taking writes.
- **`RDS-016` does not fire on this run.** The rule postdates the assessment the
  target was built from, so the empty source text index is not listed among the
  declared gaps here. It will be on any run assessed after 2026-09-12.

## How to run it

```powershell
python -m cutover.run                                    # build the certificate (read-only)
python -m cutover.run --no-aws                           # build it without AWS
python -m cutover.run --approve "full-outage window agreed with the owner"
python -m cutover.run --execute --confirm <account-id>   # run the cutover steps
python -m cutover.selftest                               # 39 offline checks
```

**Console: Phase 9 · Cutover**, which opens once a validation comes back
`validated`. One row per requirement, the approval box only when the gate blocks,
and the execute box only when the certificate is otherwise ready.

## Change log

**2026-09-12 (later) — cut over.** Requirement 1 was cleared exactly as this
file prescribed: `assess.run --run 6e48d16a`, then sizing, remediate (dry-run
gate live on `DBMIG_REHEARSAL`, 2 fixes `AUTO_APPLY`), convert, blocker and
report for that run. `--approve` refused nothing (every other requirement was
met) and recorded the approver from the caller identity; `--execute --confirm
106325261146` ran the single allow-listed statement and wrote the cutover
record. Total billable window: the instance ran about 25 minutes. Nothing on
the source changed.

**2026-09-12 — built.** `cutover/requirements.py` (the eight requirements),
`cutover/run.py` (certificate, approval, execution, CLI),
`cutover/selftest.py` (39 checks), console stage 9, three server endpoints.
The self-test covers each way the certificate could lie: another run's records,
a stale validation, a report whose status contradicts its own counts, a blocked
gate with no approval, an approval borrowed from another estate, and a
non-scheduler statement in the plan file.
