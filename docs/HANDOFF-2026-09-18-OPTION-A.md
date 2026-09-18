# Handoff — finish Phases 6, 7, 8 via a router port forward (option A)

Paste everything below the line into a new Claude Code session in `dbshift/`.
Written to be read cold: assume no memory of the sessions that produced it.

---

## The decision, and why the earlier handoff is wrong

**Do option A: a port forward on the office router, so AWS DMS reads the Oracle
source on this laptop directly.**

This supersedes `docs/HANDOFF-2026-09-18.md`, which planned an EC2 Oracle
source. That plan is dead: **`ec2:RunInstances` is explicitly denied on this
account** — verified across four instance types and three regions with real
per-region AMI ids, all returning `UnauthorizedOperation`, "explicit deny in an
identity-based policy". An explicit `Deny` cannot be overridden by any `Allow`,
so no tagging, renaming or retry gets around it. It was already recorded as
constraint 1 in `docs/05-aws-services.md`, and `provision/policy.py:33` depends
on it — the RDS endpoint is public precisely because no bastion host is
possible.

The user has since chosen A over the remaining alternatives. Do not re-open the
decision or re-probe EC2.

> **A trap worth knowing.** `RunInstances` with an AMI id from another region
> returns `InvalidAMIID.NotFound`, and `StartInstances` with a malformed
> instance id returns `InvalidInstanceID.Malformed`. **Neither is an
> authorization result** — AWS validates arguments before evaluating IAM, so a
> bad argument hides a deny behind a validation error and makes EC2 look
> available. When testing any permission, use a real resource id.

## What option A requires, and who does each part

DMS runs inside AWS and is the **client**: it opens the connection to the
source and pulls rows. There is no agent for the source side. So the Oracle
listener must be reachable from AWS.

| # | Step | Who | Status now |
|---|---|---|---|
| 1 | DHCP reservation for the laptop at `192.168.1.190` | network admin | not done |
| 2 | Port forward `TCP 1521` → `192.168.1.190:1521` on the router at `192.168.1.1` | network admin | **not done — this is the blocker** |
| 3 | Windows firewall rule allowing 1521 inbound from the AWS address | user (see below) | **none exists** |
| 4 | Everything else | already built | ✅ |

Steps 1 and 2 need router admin access. **An assistant cannot do them.**

### Step 3 — the firewall rule

Do **not** add a blanket rule. A previous session's attempt at
`-Profile Any` with no source restriction was correctly refused as a security
weakening: it exposes an Oracle listener to the whole internet.

Restrict it to the replication instance's public address, which only exists
once the DMS instance has been created. So the order is: create the DMS
instance (phase 7 `--execute`), read its address, then add the rule, then start
the task. The user runs this themselves:

```powershell
New-NetFirewallRule -DisplayName "Oracle 1521 from AWS DMS" `
  -Direction Inbound -Protocol TCP -LocalPort 1521 `
  -RemoteAddress <DMS public address> -Action Allow
```

**Remove it when the load finishes.** `Remove-NetFirewallRule -DisplayName
"Oracle 1521 from AWS DMS"`.

### What will still fail, and to say so plainly

The office public IP is **dynamic**: it was `183.82.28.170` (ACT Fibernet) and
changed to `14.97.44.14` inside one working session; the current address has no
reverse DNS. If it rotates mid-load the DMS task fails partway through and the
source endpoint has to be edited by hand.

Tell the user this before starting a long load, and prefer the small data scale
below so a rotation is unlikely to land inside the window.

## State of the world right now

**Nothing is running in AWS.** Confirm with `python -m killswitch` — exit code
`0` and `ours still billing: 0` is clean. It was verified clean at
2026-09-18T07:2xZ.

**`provision/output/deployed.json` is stale** — it points at an RDS endpoint
that has been deleted. Phase 7 reads the target from that file, so **Phase 6
must be deployed again before Phase 7 can run**; the fresh deploy rewrites it.

**Credentials.** Session keys in the `dbshift-static` profile expire every few
hours — they expired three times in one session. `aws sso login --profile
dbshift-sso` is configured (account 280646578374, role `DBA_permissions`) and
has **never been logged into**. Doing that once removes the problem. The kill
switch reads the *named profile*, not environment variables, so exporting fresh
keys alone does not fix it.

**Local databases, both reachable and free:**
- Oracle XE on Windows, `localhost:1521/XEPDB1`, listener bound to `:::1521`
  (all interfaces — the listener is not the problem). Holds `DBMIG_APP`,
  `DBMIG_REHEARSAL`, `DBMIG_TELCO`.
- PostgreSQL 16 in Docker, container `dbshift-pg`, `localhost:5432/dbshift`,
  user `dbshift`, password `dbshift-local-only`. Carries `dbmig_telco` (11
  tables) and `dbmig_app` (9), all empty.

**Records aligned** on collector run `a62c7f5f-49c9-43d4-8983-4b30d6204204`,
estate `DBMIG_TELCO`. Phase 6 refuses to run if the four upstream records
disagree — keep that run id.

**Network, measured:** public `14.97.44.14`, laptop `192.168.1.190/24`, gateway
`192.168.1.1`, no firewall rule for 1521.

## The data-volume decision

`DBMIG_TELCO` on the local Oracle is **33 million rows / 5.7 GB**. Phase 8's
level 4 checksum reads **every row on both sides**.

| Scale | DMS load | Level 4 checksum |
|---|---|---|
| Full 33M / 5.7 GB | 45–90 min | 1–3 h |
| ~5M | ~15 min | a few min |
| ~500k | ~2 min | seconds |

A previous session recorded the user leaning to **~500k**. Confirm before
loading. Two reasons it is the better choice here: a checksum match proves the
cross-engine canonical form at any volume, and a shorter window means less
chance the dynamic IP rotates mid-load.

Unlike the EC2 plan, **no re-seeding is needed** — the local Oracle already
holds the data. To load less, narrow the table list or use DMS row filters
rather than re-generating the source.

## The sequence

```powershell
# 0  credentials once
aws sso login --profile dbshift-sso

# 6  provision the target (planning free; deploy bills ~$0.106/h)
python -m provision.run                     # expect 12/12 PASS, ready: True
python -m provision.deploy --confirm 280646578374 --accept-hourly 0.106 `
    --profile dbshift-static --timeout-minutes 25
python -m provision.verify                  # expect every check PASS

# 4c  the schema must exist before the load: DMS creates nothing
#     (TargetTablePrepMode is DO_NOTHING)
python -m convert.ddl_apply_run --preflight
python -m convert.ddl_apply_run --apply --approved-by <name>

# 7  plan against the live target, then execute
python -m dms.run --pg-dsn <rds-endpoint>:5432/dbshift --pg-user dbshiftadm
#   expect 5/5 PASS including target_has_tables and target_empty
python -m dms.run --migration-type full-load --execute --confirm 280646578374 `
    --source-host 14.97.44.14 --source-port 1521 --source-user DBMIG_TELCO
#   $env:DBSHIFT_SOURCE_OWNER_PASSWORD and $env:DBSHIFT_PG_PASSWORD must be set
#   --source-host must be the PUBLIC address; dms.run refuses localhost outright

# 4c  post-load: keys, checks, indexes, deferred until the rows are in
python -m convert.ddl_apply_run --apply --post-load --approved-by <name>

# 8  cross-engine validation
python -m validate.run --target-engine postgresql

# tear down, always
python -m killswitch --destroy --confirm 280646578374
```

`--source-host` takes the **public** address. `dms.run` refuses `localhost`
with an explanation, because inside AWS that means the replication instance
itself.

## Known gaps worth closing as you go

- **`appsql/` has no `run.py`** — Phase 4d is console-only. Its `plan.py` has no
  offline self-test either (the other four modules have 89, 67, 78, 42).
- **No phase docs** for 4d at all; 4c's console screen is undocumented.
- **`expires-at` is written but nothing enforces it.** An instance ran 3 days
  past its TTL before anyone noticed. `killswitch` already exits `2` when
  something of ours is billing, specifically so a scheduled check can alarm —
  nobody scheduled it.
- **`convert/ddl_apply_run.py` exists** (added after the first handoff) with
  `--preflight`, `--apply`, `--post-load`, `--approved-by`. Verified against
  the local PostgreSQL, including all three refusal paths.

## Things that will bite

- **Python does not hot-reload.** A console server started before an endpoint
  existed returns `Not Found` for it forever. Restart `:8765` after any
  `web/server.py` change. 4c and 4d now detect a 404 and print the command.
- **Read SSM via boto3, not the AWS CLI.** `aws ssm get-parameter` returned
  `ParameterNotFound` repeatedly for a parameter that exists — a quoting
  artefact. `provision/verify.py` reads it correctly.
- **Secrets Manager is denied entirely** to `DBA_permissions`, including
  `ListSecrets`. The RDS master password lives in SSM Parameter Store as a
  SecureString; that is the only option today.
- **Delete the stack, not just the RDS instance.** The stack owns five
  resources — security group, subnet group, S3 exchange bucket, IAM role.
- **Do not add a blanket firewall rule.** Restrict by source address, and
  remove it afterwards.

## Already proven — do not re-prove

- Phase 6 deploys, verifies against the live instance, and `prepared_artefacts`
  confirms 4b/4c/4d output describes the same estate. It **fails** on a mix,
  which caught real `DBMIG_TELCO`-vs-`DBMIG_APP` drift.
- 4c's schema applies to a real RDS target: 19 pre-load + 41 post-load
  statements, one transaction each. Verified as 11 tables, 10 primary keys,
  1 unique, 7 foreign keys, 22 indexes, 5 sequences.
- Phase 7's preflight passes **5/5 against a live RDS target**, including
  `target_has_tables` (DMS creates nothing, so a missing table fails the load
  per table while the instance bills) and `target_empty`.
- The DMS **target** endpoint connects — proven 2026-09-14. Only the source is
  blocked, and `ORA-12170` on the source is the expected failure until the port
  forward exists.
- Phase 8 runs all five levels against a local target via `--target-dsn`.
  Levels 3 and 4 correctly report `??` (not comparable) with no rows — never a
  pass.
- Phase 4d converts 18 application SQL statements with a shadow-schema parse
  gate built from 4c's own DDL; 4 have no correct automatic rewrite and say
  which construct and why.

Self-tests green: `appsql` 89 + 67 + 78, `convert.selftest_project` 42,
`convert.selftest_ddl_apply` 45, `provision.selftest_prepared` 50,
`sizing.selftest_propose_target` 73, `dms.selftest` 108,
`validate.selftest_crossengine` 32, `provision.selftest` all passed.

## Read before changing anything

`CLAUDE.md` for the phase table and the decisions behind it, then the relevant
`docs/phases/phase-0*.md`. The convention is **read the phase file first,
update it after**. `docs/17-source-reachability.md` holds the network analysis
addressed to an administrator, including the EC2 evidence table — hand that to
whoever owns the router.
