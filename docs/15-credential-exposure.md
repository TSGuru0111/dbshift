# Credential exposure — found 2026-09-16

> **Status: open.** The passwords below are published on a public GitHub
> repository and should be treated as disclosed until they are rotated.

## What was found

While storing the estate credentials in a local gitignored file, the same
passwords turned up in the committed tree — and the repository is public.

```
$ curl -s https://api.github.com/repos/TSGuru0111/dbshift | jq .visibility
"public"

$ curl -s https://raw.githubusercontent.com/TSGuru0111/dbshift/main/scripts/telco-source/run_phases.ps1 | grep PASSWORD
$env:DBSHIFT_COLLECTOR_PASSWORD = 'DbMig2026C***'   # redacted here; the real value is in the public file
```

That is a direct anonymous fetch — no token, no clone. Confirmed by retrieval,
not inferred from the local tree.

## Where they are, on `origin/main`

| File | Line | Secret |
|---|---|---|
| `docs/03-source-estate.md` | 37 | `DbMig2026C***` (redacted) |
| `docs/12-telco-estate.md` | 201 | `DbMig2026C***` (redacted) |
| `scripts/oracle-source/01_setup_admin.sql` | 75 | `DbMig2026C***` (redacted) |
| `scripts/telco-source/run_phases.ps1` | 25 | `DbMig2026C***` (redacted) |
| `scripts/oracle-source/06_create_rehearsal.sql` | 34, 115 | `DbMig2026R***` (redacted) |

Both accounts are on `localhost:1521/XEPDB1`:

- `dbmig_collector` — read-only (`SELECT_CATALOG_ROLE` + `CREATE SESSION`, plus
  the explicit row-data grants). Sees all three schemas: `DBMIG_APP` (21 tables),
  `DBMIG_TELCO` (13), `DBMIG_REHEARSAL` (20).
- `dbmig_rehearsal` — **writable**, owns the 20-table rehearsal copy. This is the
  account Phase 4 applies and rolls back fixes with.

## How much this actually matters

**More than "it is only localhost".** That was the first assumption and it is
wrong. The listener is bound to `0.0.0.0`, not `127.0.0.1`:

```
$ netstat -ano | grep :1521
  TCP    0.0.0.0:1521    0.0.0.0:0    LISTENING    14548
```

Connecting with the published password over the **LAN address** rather than
localhost succeeds:

```
dsn=192.168.1.190:1521/XEPDB1  ->  connected as DBMIG_COLLECTOR
```

So anyone on this network — office wifi, a guest VLAN, a shared workspace — can
use a password published on the public internet to read the estate. The two
halves of the problem are individually mild and jointly not: a published
password plus a listener that answers the network.

It is still not reachable *from* the internet — which is why Phase 7's DMS source
endpoint failed with `ORA-12170`; AWS could not reach it either. The attacker has
to be on the same network. That is a much lower bar than the same machine.

**And three reasons it matters beyond this box:**

1. **The pattern ships with the accelerator.** This is a demonstrable capability
   intended for client estates. The same `01_setup_admin.sql` run against a
   reachable database creates a known-password account on a real system.
2. **`dbmig_rehearsal` can write.** A read-only leak is a disclosure problem; a
   writable one is an integrity problem.
3. **A client may read this repo.** A migration tool that commits database
   passwords undercuts the argument that it handles their estate carefully —
   independent of whether the leak is exploitable.

## What to do

**1. Rotate both passwords.** They are burned; git history keeps them even after
the files change. New values go in `credentials.local.ps1` (gitignored) and
nowhere else.

```sql
ALTER USER dbmig_collector IDENTIFIED BY "<new>";
ALTER USER dbmig_rehearsal IDENTIFIED BY "<new>";
```

**2. Take the literals out of the tracked files.** The setup SQL should read a
substitution variable rather than carry a default; the docs and the PowerShell
should point at `credentials.local.ps1`. Rewriting history (`git filter-repo`) is
optional and does not substitute for rotation — forks and caches keep the old
blobs regardless.

**3. Decide whether the repository should be public at all.** It carries the
account number, the region, the estate layout and the permission model. None of
that is secret on its own, and all of it is reconnaissance.

**4. Bind the listener to localhost, or firewall 1521.** Independent of the
rotation: nothing in this project needs the database to answer the network. Phase
7 already proved AWS cannot reach it, and every local phase connects over
`localhost`. Closing this alone downgrades the leak from "usable by the network"
to "usable by this machine".

**5. Consider a scan for anything else.** This was found by accident while
looking for somewhere to put a credentials file. `DBSHIFT_PG_PASSWORD`'s default
(`dbshift-local-only`) is committed too — harmless by design and named to say so,
but worth confirming nothing else is.

## What is already right

- `.gitignore` has a credentials block (`credentials*`, `*secrets*`, `*.pem`,
  `.env`) — `credentials.local.ps1` is covered by it and is invisible to git.
- The console holds the collector password in process memory only.
- The RDS master password goes to SSM Parameter Store as a `SecureString` and
  never appears in a template or the repo — `provision/policy.py` is explicit
  about it.

The handling of *generated* secrets is careful. The gap is the *seeded* ones,
which were written as fixtures and never revisited as credentials.
