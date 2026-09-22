# Runbook — open Oracle 1521 to one AWS address

Two audiences. **Part 1 is for the network engineer** and needs router admin
access. **Part 2 is for Guru** on the laptop running Oracle. Part 1 must be done
first: the firewall rule in part 2 is pointless until traffic actually arrives.

Prepared 2026-09-18 from live AWS state. Every address below was read from the
account, not assumed.

---

## The problem in one paragraph

AWS Database Migration Service must open a TCP connection **into** the office
network to read an Oracle database. DMS is the client; there is no agent to
install on the Oracle side and no mode where Oracle dials out. A test from a
replication instance inside AWS on 2026-09-18 returned:

```
ORA-12170: TNS:Connect timeout occurred.  Read timed out
```

A timeout means the packet never reached a listener — so this is a
routing/forwarding change, not a listener or credential problem. The Oracle
listener is bound to all interfaces and a read-only account was verified able
to read 21,000,000 rows locally.

## The facts you need

| Thing | Value |
|---|---|
| Source address AWS will connect **from** | **`16.4.27.36`** (the only one) |
| Office public address AWS connects **to** | `14.97.44.14` |
| Laptop's LAN address | `192.168.1.190` |
| Router / default gateway | `192.168.1.1` |
| Port | `1521` (TCP) |
| Database account used | `DBMIG_COLLECTOR` — read-only |

---

# Part 1 — for the network engineer

## Step 1.1 — Reserve the laptop's LAN address

On the router at `192.168.1.1`, create a **DHCP reservation** binding the
laptop's MAC address to `192.168.1.190`.

*Why:* the port forward in step 1.2 targets a fixed address. If DHCP moves the
laptop to another address the forward silently points at nothing, and the
failure looks identical to no forward at all.

To get the MAC address, Guru runs:

```powershell
Get-NetAdapter -Name 'WiFi' | Select-Object -ExpandProperty MacAddress
```

## Step 1.2 — Forward TCP 1521, restricted to one source

On the same router, add a port-forwarding (sometimes "virtual server" or
"NAT/PAT") rule:

| Field | Value |
|---|---|
| Protocol | TCP |
| External / WAN port | 1521 |
| Internal / LAN host | `192.168.1.190` |
| Internal port | 1521 |
| **Source / remote address** | **`16.4.27.36/32`** |

**The source restriction is the important part.** Without it, an Oracle
listener is exposed to the entire internet for as long as the rule exists.
With it, only that single AWS instance can reach it.

If the router cannot restrict a forward by source address, say so rather than
creating an open rule — Guru will arrange an alternative. An unrestricted
Oracle port on a business connection is not an acceptable trade for a demo.

## Step 1.3 — Confirm from outside, not inside

From a device **off** the office network — a phone on mobile data with WiFi
disabled is fine:

```
telnet 14.97.44.14 1521
```

Expect the connection to be **refused or to time out** at this stage, because
step 2.1 has not happened yet. That is correct. The useful signal is whether the
behaviour *changes* after part 2.

> **Do not test by connecting to `14.97.44.14` from inside the office
> network.** That requires the router to loop the packet back to itself (NAT
> hairpinning) and most routers silently drop it, so a failure tells you
> nothing. This misled the earlier analysis once.

## Step 1.4 — Tell Guru when 1.1 and 1.2 are done

Nothing further is needed from you. **Please also confirm:**

1. Is `14.97.44.14` **contractually static**, or dynamic? It changed from
   `183.82.28.170` within one working session on 2026-09-18. If it is dynamic,
   the AWS endpoint has to be edited each time it rotates, and a long data load
   will fail partway through.
2. Is a source-restricted forward to a workstation acceptable under the
   security policy, temporarily?

## Step 1.5 — Remove the rule afterwards

When Guru says the migration test is finished, **delete the forwarding rule and
the reservation**. Neither should outlive the exercise.

---

# Part 2 — for Guru, on the laptop

Do these **after** the engineer confirms part 1. Steps 2.1 and 2.2 are yours;
from 2.3 onwards, ask Claude in the project session.

## Step 2.1 — Allow 1521 inbound, from that one address only

In an **Administrator** PowerShell:

```powershell
New-NetFirewallRule -DisplayName "Oracle 1521 from AWS DMS" `
  -Direction Inbound -Protocol TCP -LocalPort 1521 `
  -RemoteAddress 16.4.27.36 -Action Allow
```

`-RemoteAddress 16.4.27.36` is what keeps this narrow. **Do not** substitute
`-Profile Any` with no remote address — that opens the port to everything that
can reach the machine, and an earlier attempt at exactly that was correctly
refused as a security weakening.

Verify it exists:

```powershell
Get-NetFirewallRule -DisplayName "Oracle 1521 from AWS DMS" |
  Select-Object DisplayName, Enabled, Direction, Action
```

## Step 2.2 — Check the Oracle listener is still up

```powershell
Get-NetTCPConnection -LocalPort 1521 -State Listen
```

Expect `LocalAddress` of `::` or `0.0.0.0` — meaning all interfaces. If it
shows only `127.0.0.1`, the listener is loopback-only and `listener.ora` needs
changing; tell Claude and it will look.

## Step 2.3 — Ask Claude to re-test the endpoint

Say: **"the port forward is done, re-test the DMS source endpoint"**.

It runs one AWS call, takes about 60 seconds, creates nothing and costs
nothing. Three possible answers:

| Result | Meaning | Next |
|---|---|---|
| `successful` | The network is open | go to 2.4 |
| `ORA-12170` again | Still timing out — the forward is not reaching the laptop, or the firewall rule did not apply | re-check 1.2 and 2.1 |
| `ORA-01017` | **Connected**, credentials rejected | the network is fixed; only the password is wrong, which Claude can correct |

That third outcome is good news dressed as a failure — it proves the packet
arrived.

## Step 2.4 — Decide the data volume before loading

The source holds **33 million rows / 5.7 GB**, with 21 million in `CDR` alone.
Phase 8's checksum then reads **every row on both sides**.

| Scale | DMS load | Phase 8 checksum |
|---|---|---|
| Full 33M | 45–90 min | 1–3 hours |
| ~5M | ~15 min | a few minutes |
| ~500k | ~2 min | seconds |

Two reasons to prefer a smaller load for a demo: a checksum match proves the
cross-engine comparison works at **any** volume, and a shorter window means
less chance the public IP rotates mid-load and breaks the task.

## Step 2.5 — Run the load and the validation

Ask Claude to run the full load, then Phase 4c's post-load statements (keys,
checks and indexes, deliberately deferred until the rows are in), then Phase 8.
It has the commands.

## Step 2.6 — Clean up, in this order

1. **Ask Claude to tear down AWS** — the RDS instance, the DMS replication
   instance and both endpoints. They bill about **$0.14/hour** combined for as
   long as they exist, whether a task is running or not.
2. **Remove your firewall rule:**
   ```powershell
   Remove-NetFirewallRule -DisplayName "Oracle 1521 from AWS DMS"
   ```
3. **Tell the engineer to remove the forward and the reservation** (step 1.5).

---

## What is already done, so nobody redoes it

- The RDS PostgreSQL target is deployed, verified, and carries the converted
  schema: 11 tables, 10 primary keys, 7 foreign keys, 22 indexes, 5 sequences.
- Its security group admits **only** `14.97.44.14/32` on 5432 — proven by a
  successful login from the laptop.
- The DMS replication instance, source endpoint and target endpoint all exist.
  The **target** endpoint tests `active`. Only the source fails.
- Phase 7's preflight passes 5/5, including checks that the target has the
  right tables and that they are empty.

The single outstanding item is the port forward in step 1.2.

## If the IP turns out to be dynamic

Then a port forward is a short-lived fix and a Site-to-Site VPN is not viable
either — a VPN Customer Gateway needs one fixed address. The remaining option
is moving the Oracle source into AWS, which removes the network from the
question entirely. `docs/17-source-reachability.md` covers that, including why
EC2 specifically is not available on this account.
