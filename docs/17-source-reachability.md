# Making the on-premises Oracle reachable from AWS DMS

**For: whoever administers the office network.**
**Asked because:** AWS Database Migration Service must open a TCP connection to
an Oracle listener that currently sits on a laptop behind NAT. Everything else
in the migration is built and verified; this is the one requirement that is a
network change rather than a software one.

---

## What is being asked for, in one line

Allow **inbound TCP 1521** from an AWS replication instance in
`ap-south-1` to an Oracle listener on the office LAN, and give that listener an
address AWS can route to.

## Why DMS cannot use an outbound connection

DMS is a managed service that runs inside AWS. It is the **client**: it opens
the connection to the source database and pulls rows. There is no agent to
install on the source side and no mode in which the source dials out to DMS.

So the source must be reachable from the internet, or from a private link into
the AWS VPC. This is AWS's design, not a configuration we have got wrong.

```
   AWS (ap-south-1, vpc-05f9bf94bf057b67e, 172.31.0.0/16)
   ┌──────────────────────────────┐
   │  DMS replication instance    │  ── reads ──►  Oracle 1521   (THIS IS THE PROBLEM)
   │  (dms.t3.small)              │  ── writes ─►  RDS PostgreSQL 5432  (works today)
   └──────────────────────────────┘
```

The **write** side already works: the target is an RDS instance inside the same
VPC, and a DMS task connected to it successfully on 2026-09-14. Only the
**read** side is blocked.

## Proof that inbound is closed, from outside the network

**Measured 2026-09-18 by a DMS replication instance running inside AWS**, which
is the exact client that will have to connect. Not inference:

```
Endpoint: dbshift-source-dbmig-telco  ->  14.97.44.14:1521
Status:   failed
Error:    ORA-12170: TNS:Connect timeout occurred   OCI connection failure.
          Additional info: Read timed out
```

The **target** endpoint (`dbshift-target-dbmig-telco`, the RDS instance) tested
`active` in the same run, so DMS itself was working correctly. Only the read
side failed.

`ORA-12170` is a **timeout**: the packet never reached a listener. That
distinguishes it from the two failures that would mean something else, and the
distinction is what tells you where to look:

| Error | Meaning |
|---|---|
| `ORA-12170` (what we got) | nothing answered — no route or no port forward |
| `ORA-12541` | reached the host, nothing listening on 1521 |
| `ORA-01017` | **connected**, credentials rejected — the network is fine |

So the work needed is a routing/forwarding change, not a listener or credential
change. The Oracle listener is bound to `:::1521` (all interfaces) and a
read-only account (`DBMIG_COLLECTOR`) was verified able to read all 21,000,000
CDR rows locally.

> **Do not test this from inside the network.** `Test-NetConnection` against
> your own public address requires the router to loop the packet back (NAT
> hairpinning), which most consumer routers silently drop — so a failure proves
> nothing about the outside world. It misled this analysis once. Use
> `canyouseeme.org` on port 1521, a phone on mobile data with WiFi off, or a
> DMS endpoint test.

## The current state, measured

| | Value | Problem |
|---|---|---|
| Public address seen from the internet | `14.97.44.14` | not the database host; it is the ISP gateway |
| Laptop's LAN address | `192.168.1.190/24` | private, not routable from outside |
| Default gateway | `192.168.1.1` | the router that holds the public address |
| Oracle listener | bound to `:::1521` (all interfaces) | fine — the listener itself is not the problem |
| Windows firewall | enabled on Domain, Private and Public profiles, no rule for 1521 | blocks inbound even if routed |

**The public address is also not stable.** It was `183.82.28.170` (ACT
Fibernet) and changed to `14.97.44.14` within the same working session, and
`14.97.44.14` has no reverse DNS record. That matters for option B below.

## Three ways to fix it

### Option A — Port forward on the router

Cheapest and quickest. Suitable for a demo or a time-boxed rehearsal.

**On the router at `192.168.1.1`:**

1. Reserve `192.168.1.190` for the laptop in DHCP, so the address does not move.
2. Forward `TCP 1521` → `192.168.1.190:1521`.
3. If the router supports it, restrict the source to the AWS NAT/EIP address the
   replication instance uses, rather than `0.0.0.0/0`. We can supply that
   address once the instance exists.

**On the laptop (Windows):**

```powershell
New-NetFirewallRule -DisplayName "Oracle 1521 from AWS DMS" `
  -Direction Inbound -Protocol TCP -LocalPort 1521 `
  -RemoteAddress <the AWS address> -Action Allow
```

**What to be aware of:**

- This exposes an Oracle listener to the internet for as long as the rule
  exists. Restrict by source address, and remove the rule afterwards.
- The public address is dynamic. If it changes mid-run the DMS task fails
  partway through, and the endpoint has to be edited.
- The account DMS uses should be a dedicated read-only user on the source
  schema, not a DBA account.

### Option B — Site-to-Site VPN

The option we would normally recommend, and the one **we do not recommend
here**.

A Site-to-Site VPN terminates IPsec on a *Customer Gateway* — a device
configured with **one fixed public IP address**. Requirements:

1. A **static** public IP. The current address is dynamic and changed inside one
   session, so the tunnel would drop each time it rotates and stay down until
   someone edits the AWS configuration by hand.
2. Admin access to the router to configure IPsec (pre-shared keys, phase 1/2
   parameters, and either BGP or a static route for `192.168.1.0/24`).
3. The router advertising the laptop's subnet into the tunnel.
4. The same Windows firewall rule as option A.

If the office genuinely has a static IP on business broadband and the router
supports IPsec, this is the better long-term answer: no inbound port is exposed
to the internet, and the traffic is encrypted. Please confirm **(a)** the IP is
contractually static and **(b)** IPsec termination is permitted by policy,
before we build the AWS side — a VPN connection bills while it exists.

### Option C — Put the source in AWS

No network change at all. A host in the same VPC as the target runs the Oracle
source; DMS reaches both endpoints privately.

This is what we would do for a demo, because it removes the network from the
question entirely. It does not help a real migration, where the source is by
definition on the client's premises — but for proving the pipeline it is the
fastest honest path.

> ⚠️ **EC2 cannot be that host on this account. Measured 2026-09-18.**
>
> `ec2:RunInstances` fails with **`UnauthorizedOperation` — "explicit deny in an
> identity-based policy"**, for `t3.medium`, `t3.small`, `t3.micro` and
> `t2.micro` alike. The instance type is irrelevant; the deny is on
> `arn:aws:ec2:ap-south-1:280646578374:instance/*`. `ec2:CreateKeyPair` is
> denied as well.
>
> This is **not** a missing grant that tagging, renaming or retrying gets
> around — an explicit `Deny` in an identity-based policy cannot be overridden
> by any `Allow`. It is the same constraint already recorded as constraint 1 in
> `docs/05-aws-services.md`, which also rules out a bastion host. Changing it
> requires an AWS account administrator to amend the `DBA_permissions`
> permission set.
>
> **What is still open**, probed the same day, creating nothing:
>
> | Action | Result | Meaning |
> |---|---|---|
> | `ec2:RunInstances` | `UnauthorizedOperation` (explicit deny) | EC2 host impossible |
> | `ec2:CreateKeyPair` | `UnauthorizedOperation` | no new SSH key |
> | `ec2:CreateSecurityGroup` | `DryRunOperation` | allowed |
> | `ecs:CreateCluster` | succeeded, then deleted | allowed |
> | `ecs:RunTask` | `ClientException: TaskDefinition not found`, against a **real** cluster | authorized past IAM |
> | `lightsail:GetInstances` | `AccessDeniedException` | Lightsail not an alternative |
> | `iam:SimulatePrincipalPolicy` | `AccessDenied` | permissions cannot be enumerated ahead of use |
>
> **Two AWS error codes look like permission and are not.** Both were hit while
> checking this, and each briefly suggested EC2 was available when it was not:
>
> - `RunInstances` with an AMI id from another region returns
>   `InvalidAMIID.NotFound`, and `StartInstances` with a made-up instance id
>   returns `InvalidInstanceID.Malformed`. **Neither is an authorization
>   result** — AWS validates the argument before evaluating IAM, so a bad
>   argument hides the deny behind a validation error.
> - Re-probed with a **real** AMI resolved per region and with the **real**
>   instance ids on the account, every call returned `UnauthorizedOperation`.
>
> So the deny is **account-wide, not region-scoped**: `ap-south-1`, `us-east-1`
> and `ap-southeast-1` each refuse `RunInstances` with a valid AMI. Four
> stopped instances belonging to other teams exist on the account and cannot be
> started by this role either. When testing a permission, **use a real resource
> id** — a validation error is not an `Allow`.
>
> So **ECS Fargate is the remaining in-VPC option**: it runs the same Oracle XE
> container the EC2 plan wanted, with an ENI in the VPC that DMS can reach on
> 1521, and it needs no `RunInstances`. The one untested prerequisite is
> `iam:CreateRole` for a `dbshift-`prefixed task execution role — IAM roles are
> scoped to `dbshift-*` on this account, so it is expected to work, but it
> could not be confirmed without creating a role.
>
> Fargate has no persistent local disk, so the seed is re-run on each task
> start unless the data sits on an EFS volume.

## What this does *not* fix

A real client migration has the same requirement at **their** site, with their
own answers about static addressing, firewall policy and whether a database may
accept an inbound connection from AWS at all. That is why the migration's Phase
1 states network reachability as a prerequisite rather than something the
tooling solves.

What is already proven, and does not depend on any of this:

- the target RDS instance is created, verified and carries the converted schema
- the DMS task definition, table mappings and task settings are generated and
  validated
- the DMS **target** endpoint connects
- the preflight refuses to start a load when the target lacks the tables or
  already holds rows

Only the source connection is outstanding.

## What we need back

1. Is the office public IP **contractually static**? (It changed during
   testing, which suggests not.)
2. Is a **port forward** to a workstation acceptable under the security policy,
   temporarily and restricted by source address?
3. Is **IPsec termination** on the office router permitted, and who administers
   that router?
4. If neither is acceptable, confirm option C for the demo and we will document
   the network requirement as a client prerequisite.

---

*Prepared 2026-09-18. Measurements taken from the machine running the Oracle
source; AWS details from account 280646578374, region ap-south-1.*
