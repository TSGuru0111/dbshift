# The MySQL source estate on EC2

> **Live since 2026-09-29.** `dbshift-source-mysql`, `i-0bd32544c1577b5ed`,
> t3.medium, ap-south-1a, in the same VPC and subnet as `dbshift-source-oracle`.
> **AWS SCT has assessed it for real** — 10 action items / 34 occurrences, with
> `3.108.190.1:3306` recorded in SCT's own CSV as the server it read.

## What exists

| | |
|---|---|
| Instance | `i-0bd32544c1577b5ed` — t3.medium, 30 GB gp3 |
| Network | vpc-05f9bf94bf057b67e, subnet-0d4e43b6200c74ce1 (ap-south-1a) |
| Security group | `sg-0067f7243b4a0dce7` — 3306 and 22 from the operator `/32` only |
| Key pair | `dbshift-source-key` (`provision/output/dbshift-source-key.pem`) |
| MySQL | **8.0.46**, community edition, from `mysql80-community` |
| Estate | `dbmig_mysql_app`, `dbmig_mysql_rpt`, `dbmig_rehearsal` |
| Tags | `Purpose=DMA`, `project=dbshift` — so the kill switch owns it |

**It bills while running (~$0.05/hr).** Stop it when idle:
`aws ec2 stop-instances --instance-ids i-0bd32544c1577b5ed`. The public IP changes
on restart unless an Elastic IP is attached; nothing here depends on it being
stable, but the security-group rule and any SCT cache directory are keyed to it.

## Rebuilding it

```bash
# 1. the instance, from the corrected cloud-init
aws ec2 run-instances --image-id <al2023-x86_64> --instance-type t3.medium \
  --key-name dbshift-source-key --security-group-ids sg-0067f7243b4a0dce7 \
  --subnet-id subnet-0d4e43b6200c74ce1 --associate-public-ip-address \
  --user-data file://scripts/mysql-source/cloud-init.yaml \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=dbshift-source-mysql},{Key=Purpose,Value=DMA},{Key=project,Value=dbshift}]'

# 2. the estate -- runs ON THE HOST, so MySQL's generated root password never
#    leaves it
scp -i provision/output/dbshift-source-key.pem -r scripts/mysql-source ec2-user@<ip>:~/
ssh -i provision/output/dbshift-source-key.pem ec2-user@<ip> \
  'sudo bash ~/mysql-source/ec2_bootstrap.sh "<password>"'

# 3. prove the defects landed, from the operator machine
DBSHIFT_MYSQL_PASSWORD='<password>' python scripts/mysql-source/verify_defects.py \
  --dsn <ip>:3306/dbmig_mysql_app --user dbmig_collector      # must print 12/12
```

**Resolve the AMI with `ec2 describe-images`, not SSM.** The public parameter
`/aws/service/ami-amazon-linux-latest/...` returns `ParameterNotFound` on this
account, and the empty value it yields produces `InvalidAMIID.Malformed` — a
validation error impersonating a permission result, which is the trap
`docs/17-source-reachability.md` documents.

## Four things this cost, each measured

**1. MySQL 8.4 installed when 8.0 was asked for — and it broke the config.**

The repo ids are `mysql-8.4-lts-community` and `mysql-tools-8.4-lts-community`,
**not** `mysql84-lts-community`. A wrong id makes
`dnf config-manager --disable` a silent no-op, so 8.4.11 installed anyway.

That mattered for more than the version: **MySQL 8.4's packaged `/etc/my.cnf` has
no `!includedir /etc/my.cnf.d`**, so `/etc/my.cnf.d/dbshift.cnf` sat on disk and
was never read — leaving `ONLY_FULL_GROUP_BY` in `sql_mode`, which breaks SCT.
A wrong repo id would have surfaced as an abandoned SCT assessment twenty minutes
later.

**8.0.46's packaged my.cnf lacks the includedir too**, so `cloud-init.yaml` now
appends it and then *asserts* the resulting `sql_mode` before declaring readiness.

**2. `ONLY_FULL_GROUP_BY` must be off, and that is SCT's fault, not ours.**

SCT 1.0.677's own `load-partitions-by-schema` query is invalid under it:

```
Expression #1 of ORDER BY clause is not in SELECT list, references column
'information_schema.PARTITIONS.TABLE_SCHEMA' which is not in SELECT list;
this is incompatible with DISTINCT
```

SCT retries three times, then abandons with *"Metadata loading was interrupted
because of data fetching issues"*. It is in MySQL 8's **default** `sql_mode`, so
**a stock MySQL 8 cannot be assessed until it is relaxed** — a prerequisite a
client's DBA must action on the source server.

**3. SCT wants `SELECT` and `SHOW VIEW` at server scope.**

`MYSQL Server : [SELECT, SHOW VIEW]`, granted `ON *.*` — not the schema scope the
collector is satisfied by. `01_setup_admin.sql` grants both and verifies the
server-scope pair.

**4. The SCT cache key had to learn about hosts.**

The same estate on a local Docker MySQL and on this host produced the same
`(engine, schemas, target)` key, so **the EC2 run served the container's cached
report**. It looked right — identical action items, because it is the same estate
— which is exactly what made it dangerous. A report is evidence about a *server*.

`sct/runner.py` now includes a host tag, with `localhost`/`127.0.0.1` collapsing
to `local` so a local re-run still hits its cache, and Oracle's historical key
unchanged so the five real 25-minute assessments on disk are still found.

## Where SCT runs, and why it is still local

SCT 1.0.677 runs on the **operator's machine** and reaches this host over the
internet through the `/32` rule. `docs/16-sct-runner.md` states the position:
**local for the demo, EC2 for a customer, never Lambda.**

For a client engagement SCT should run on a Windows EC2 instance in the VPC, so
the credentials and the metadata never leave it. Nothing in the code assumes
otherwise — `sct/hosts/local.py` is one host implementation behind an interface —
but that instance does not exist yet, and pretending the demo needs it would have
cost an hour to prove something `docs/16` already commits to.

**The MySQL password crosses the internet on this path.** Acceptable for a seeded
demo estate whose passwords are disposable; not acceptable for client data.
