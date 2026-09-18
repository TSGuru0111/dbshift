# The AWS SCT runner — running the real tool

> **Latest update — 2026-09-17 (later).** **SCT 1.0.677 is installed and runs**,
> and the console has the panel: target dropdown, streaming progress, sortable
> action items, and SCT's own PDF/CSV served verbatim. All three prerequisites
> report `[ok]`. Self-test `sct.selftest` **65/65**; browser drive
> `drive_sct.js` **32/32** in headless Edge.
>
> **A real SCT report exists.** The grant
> (`scripts/oracle-source/08_grant_sct_dictionary.sql`) was applied on
> 2026-09-17 and SCT then ran end to end: **33 occurrences → 12 action items**
> on `DBMIG_APP`, and **75 → 14** across all three schemas from the console.
> PDF and CSV verified **byte-identical** to SCT's own files. The parser now
> reads SCT's real 17-column format with **zero unmapped columns**; the first
> run corrected four wrong guesses, which is exactly what it was for.

## Why this exists alongside `assess/`

`assess/` is this project's own 50-rule engine. `sct/` drives **AWS Schema
Conversion Tool** and presents what SCT itself reports.

Neither replaces the other, and the split is not duplication:

| | Covers | Read by |
|---|---|---|
| `assess/` | OPS, SEC, DQ, PERF, RDS findings — NOARCHIVELOG, supplemental logging, missing primary keys, grants | Phases 3, 5, 7 |
| `sct/` | schema and stored-code conversion against a chosen target, graded by the work a person must do | the client |

**SCT does not look at what Phase 5's gate needs.** Deleting `assess/` to
"just use SCT" would break the gate, sizing and the DMS preflight, all of which
read `assessment.json`. The console shows both.

## The host question, settled

SCT is a **stateful desktop Java application** driven by a batch CLI over
`.scts` scenario files. There is no SCT API and no `boto3` client. So the only
real question is *where the process runs*, and **reachability decides it, not
capability**: SCT must open a TCP connection to the source listener.

| Host | When | Cost |
|---|---|---|
| `sct.hosts.local` | the source is this machine's Oracle XE | **$0** |
| `sct.hosts.ec2` | a customer's Oracle, reached over **their** existing Direct Connect or VPN | ~$0.09–0.17/hr running, EBS only when stopped |

### Why not Lambda

This was asked directly and is worth keeping written down, because it looks like
it should work — the two things people expect to block it do not:

- ✅ **Image size fits.** Container images get 10 GB; SCT + Corretto 11 + JDBC is
  1–2 GB. The 250 MB zip limit does not apply.
- ✅ **No GUI needed.** SCT's batch CLI is genuinely headless.

What actually blocks it:

- ❌ **Read-only filesystem.** Lambda container images must run on a read-only
  filesystem apart from `/tmp`; SCT writes inside its own install directory.
  Copying the install into `/tmp` on every cold start means reverse-engineering
  a closed-source Java app's write paths, and it breaks on the next SCT version.
- ❌ **The 15-minute ceiling is hard.** A real customer estate assesses for
  hours. Lambda kills the process with **no partial report**. It would work on
  the 90-object demo estate and fail on the first real one.
- ❌ **Redistribution.** Baking AWS's licensed binary into an ECR image is a
  licence question, not a technical one. On EC2 the customer installs SCT
  themselves, in their own account, under their own acceptance of the terms.
- ❌ **It cannot reach a local Oracle at all.** A Lambda runs inside AWS; the XE
  listener is behind this machine's NAT. The only ways across are a VPN
  (~$36/month) or exposing port 1521 to the internet — which, on a listener
  bound to `0.0.0.0` with passwords already public on GitHub
  (`docs/15-credential-exposure.md`), is not an option.

The honest place for Lambda is **orchestration**, not execution: short,
stateless work (trigger, poll, parse, notify) with EC2 running SCT underneath.
That is a separate module if it is ever wanted, not a third host.

## Prerequisites — three manual installs

None of these can be automated away. `python -m sct.run --check` reports each
one and how to fix it.

| | What | How |
|---|---|---|
| 1 | **A JVM** — normally none needed: SCT **ships Corretto 17** and its own wrapper uses it. A standalone 17 or 11 is only a fallback | `winget install Amazon.Corretto.11.JDK` (needs admin), or the portable zip from `corretto.aws` (no admin) |
| 2 | **AWS SCT** | `https://s3.amazonaws.com/publicsctdownload/Windows/aws-schema-conversion-tool-1.0.latest.zip` — 1.16 GB |
| 3 | **Oracle JDBC driver** | `ojdbc8.jar`, Maven Central: `com/oracle/database/jdbc/ojdbc8` |

The published CLI reference says to install Corretto 11. That is what the GUI installer wants; it is **not** what the batch wrapper invokes — see the change log below.

### What this machine has

All three installed to `%LOCALAPPDATA%\dbshift-tools\` with **no elevation** —
the MSIs need admin, the portable zip and `msiexec /a` do not:

```
sct\PFiles64\AWS Schema Conversion Tool\    SCT 1.0.677 (msiexec /a, 354 files)
  app\AWSSchemaConversionToolBatch.jar       the batch CLI
  runtime\bin\java.exe                       Corretto 17.0.15 -- SCT's own, preferred
jdk11.0.32_10\                               Corretto 11.0.32.1 (fallback, unused)
jdbc\ojdbc8.jar                              21.11.0.0.0
```

`python -m sct.run --check` reports all three `[ok]`.

The JDBC driver was **verified against the live database**, not just downloaded:

```
DRIVER  : 21.11.0.0.0
PRODUCT : Oracle Database 21c Express Edition Release 21.0.0.0.0
RESULT  : CONNECTED
```

Paths are overridable, so a customer install anywhere needs no code change:
`DBSHIFT_SCT_HOME`, `DBSHIFT_SCT_JAVA_HOME`, `DBSHIFT_ORACLE_JDBC_JAR`.

## The target dropdown

SCT assesses **one target platform at a time** — it is a project setting, not a
report filter. So selecting a target means *running SCT again for that target*,
and results are cached per `(collector_run, target)`.

| Target | Scope |
|---|---|
| Amazon RDS for Oracle | in scope — homogeneous |
| Amazon RDS for PostgreSQL | in scope — the Phase 4b/4c/7 path, **default** |
| Amazon Aurora PostgreSQL | **out of scope**, listed for comparison |
| Amazon Redshift | **out of scope**, listed for comparison |

The out-of-scope targets are **listed and marked, never hidden**. A dropdown
that silently omitted them would look like the tool could not do it, and a
client will ask about both. But they carry no `engine`, which is what stops a
selected target leaking into Phase 3 as a migration path — asserted by the
self-test, not just intended.

## What actually happens

1. **`--check`** finds the SCT batch jar, then the JVM (SCT's own runtime first,
   by *asking the JVM its version* rather than parsing its path), then the JDBC
   driver. SCT is located first because its bundled runtime is the one to prefer
   and cannot be looked for until the install directory is known.
2. **`--plan`** shows the target, schemas, host, prerequisites and the scenario's
   commands. Free, and **needs no password**.
3. **`scenario.build_script()`** generates the `.scts` in SCT's own format:
   `SetGlobalSettings` (register the JDBC driver, set the log folder) →
   `CreateProject` → `CreateFilter` (scope to the schemas under migration) →
   `AddSource` → `AddServerMapping` (onto the **virtual** target — this one
   string is what the dropdown changes) → `CreateReport` → `SaveReportPDF` →
   `SaveReportCSV` → `SaveProject`.
4. **The host runs it.**
   `java <11 JVM flags> -jar AWSSchemaConversionToolBatch.jar -type scts -script <file>`,
   stdout streamed line by line. The flags and the argument form come from SCT's
   own `RunSCTBatch.cmd`, not from the documentation.
5. **Success is checked against artefacts, not the exit code** — SCT returns 0
   with a failed command and an empty report.
6. **`parse.py`** reads SCT's CSV into the console's existing issue shape.
7. **The PDF and CSV are served verbatim.**

### The password

SCT's batch CLI takes the source password **only inside the scenario file**, so
it must touch disk. Handled rather than hidden:

- written to a per-run directory under `sct/output/` (gitignored),
- deleted in a `finally` — it does not outlive the run, whatever happened,
- the **record stores the redacted scenario**, so a run is reproducible without
  the secret travelling with it.

`sct.selftest` asserts the password is absent from the record and from every
refusal payload.

### Severity is derived, and says so

SCT grades by **complexity** — the work a person must do. `assess/` grades by
**severity** — how dangerous it is. Different axes; collapsing one into the
other misreports both.

So a parsed row carries **both**: SCT's complexity verbatim, plus a severity
derived from it *for sorting only*, flagged `severity_is_derived: true` with the
mapping in `parse.COMPLEXITY_TO_SEVERITY`. That is DBShift ordering SCT's
buckets, not SCT assigning severity.

### Column names are read, not assumed

SCT's CSV header has varied across versions. `parse.py` matches headers
case-insensitively against known aliases and records anything it could not place
in `unmapped_columns` rather than dropping it. A future SCT column is then
**visible** instead of silently lost.

## Provenance — the direction is opposite to `report/export.py`

`report/export.py` exists to say **"not generated by AWS SCT"**: its numbers are
DBShift's, shaped like SCT's, and its `PROVENANCE` constant says so on every
page and sheet.

This path is the **opposite claim**. The PDF and CSV *are* AWS SCT's own output,
served byte for byte. Re-rendering them would forfeit the only thing this path
has that `report/export.py` does not.

Mixing the two claims in either direction is the failure worth testing, so
`sct.selftest` asserts that `report/export.py` still disclaims SCT **and** that
the SCT path never imports that disclaimer.

## How to run it

```powershell
. .\credentials.local.ps1
.\.venv\Scripts\python.exe -m sct.run --check                     # prerequisites
.\.venv\Scripts\python.exe -m sct.run --list-targets              # the dropdown
.\.venv\Scripts\python.exe -m sct.run --plan                      # free, no password
.\.venv\Scripts\python.exe -m sct.run --target rds-postgresql     # run SCT
.\.venv\Scripts\python.exe -m sct.run --target rds-oracle --force # re-run, no cache
.\.venv\Scripts\python.exe -m sct.selftest                        # 58/58, offline
```

## The console

**Phase 2 · Assess**, below the 50-rule result and separated by a rule, because
the two are different claims on one screen and the layout should say so.

- **Target dropdown.** Out-of-scope targets are listed and **marked** in the
  option text; selecting one shows an amber note saying SCT will assess it for
  comparison and no later phase will accept it.
- **Prerequisite panel**, shown only when something is missing, naming which of
  the three and how to fix it.
- **Streaming progress.** SCT's own log is filtered to command boundaries —
  it prints its entire settings block at startup, which would bury them.
- **Sortable action items**, the same interaction as the rules table: click a
  heading to sort, a row for SCT's recommendation. Complexity is shown with
  SCT's own meaning in the tooltip.
- **PDF / Excel / CSV**, each carrying the selected target so the file matches
  what is on screen.

Endpoints: `/api/sct/targets`, `/api/sct/preflight`, `/api/sct/assess` (SSE),
`/api/sct/assessment`, `/api/sct/report.pdf`, `/api/sct/report.xlsx`,
`/api/sct/report.csv`. All refuse with 409 before a run.

## Known limits

- **`SELECT ANY DICTIONARY` is required**, and it is wider than
  `SELECT_CATALOG_ROLE`: it reaches SYS tables including `USER$` (password
  hashes) and `LINK$` (database link credentials). Granted here; at a client it
  belongs in a change request. A DBA who refuses it is refusing SCT, which is a
  legitimate position — `assess/` runs on `SELECT_CATALOG_ROLE` alone.
- **A run takes about five minutes on the 90-object demo estate** — roughly
  2.5 of them inside `CreateReport` — and SCT reports no progress percentage,
  so the console streams command boundaries rather than a bar.
- **`sct.hosts.ec2` is not built.** The template a customer's cloud team would
  review does not exist yet; `provision/render.py` is the pattern to follow, and
  `killswitch/` must learn `dbshift-sct-runner`.
- **Never run against `DBMIG_TELCO`.** A second estate is the only thing that
  catches a whole class of bug — the working agreement in `CLAUDE.md` says so
  from four such bugs found that way.
- **`--force` re-runs a job that takes minutes to hours.** There is no progress
  percentage, because SCT's stdout does not provide one.

## Change log

**2026-09-17 — built.** Client asked for the Assessment phase to drive the real
SCT tool rather than preset rules, with a target dropdown and SCT's own PDF and
Excel.

Three things were settled on the way, all of them by checking rather than
assuming:

1. **Lambda was requested and does not work here** — not for the reasons
   expected (image size and headlessness are both fine) but for the read-only
   filesystem, the hard 15-minute ceiling, SCT redistribution, and above all
   that nothing inside AWS can reach a local XE. Recorded in full above so it is
   not re-litigated.
2. **Elevation was not available**, so the Corretto MSI failed (exit 1602, UAC
   declined). The portable zip needs no admin and was used instead — worth
   knowing for a customer machine with locked-down installs.
3. **The JDBC driver was verified by connecting**, not by checking the file
   exists. Driver 21.11.0.0.0 against Oracle 21c XE returned `CONNECTED`. A
   present-but-wrong driver is exactly the failure that otherwise surfaces as an
   unreadable Java stack trace inside a batch run.

**2026-09-17 (later) — SCT installed, and the console panel built.**

Installing it found two things worth keeping:

- **The MSI needs admin; `msiexec /a` does not.** An administrative extract
  unpacked all 354 files with no UAC prompt (exit 0). Relevant to a customer
  machine with locked-down installs — but it writes **no registry key**, and
  `RunSCTBatch.cmd` resolves its own install path from
  `HKLM\SOFTWARE\Amazon Web Services, Inc.\AWS Schema Conversion Tool`. So the
  wrapper cannot find itself on such an install, which is why this project
  invokes the jar directly and locates it with `toolchain.py`.
- **SCT ships Corretto 17 and uses it.** The published CLI reference says to
  install Corretto 11; SCT 1.0.677's own wrapper defaults to
  `<install>/runtime/bin/java.exe`, which is 17, and the batch jar's manifest
  says `Build-Jdk: 17.0.15`. `toolchain.py` now prefers SCT's own runtime and
  accepts 17 or 11, asking the JVM its version rather than parsing a path.

Then four corrections to the scenario format, each from a failed run:

1. **`.scts` is not JSON.** It is a custom language parsed by ANTLR inside SCT
   — `Command -param: 'value'` blocks terminated by a bare `/`. The first
   version wrote JSON and died with `AntlrParsingException ... at line 1:0`.
   The real format came from **SCT's own `GetCliScenario` command**, which
   emits 13 templates including `ReportCreationTemplate.scts` — exactly this
   use case. Reading the vendor's template beat both the documentation and
   guesswork.
2. **`SetGlobalSettings` must register the JDBC driver**, or `AddSource` fails
   on the driver rather than the connection.
3. **Inline JSON is escaped once, not twice.** `json.dumps` already produces
   `C:\drivers\ojdbc8.jar`; escaping again yields a path SCT cannot resolve.
4. **`SaveReportPDF` takes a file, `SaveReportCSV` takes a directory.** Not
   symmetrical, and swapping them errors at save time.

**The bug that mattered most: SCT exits 0 when a command has failed.**
`AddSource` raised `DbLoaderInsufficientPrivilegesException`, every later
command ran against an empty tree and "succeeded", `SaveReportPDF` wrote
nothing, and the process returned 0 — so the runner reported a clean run with
no report. **An empty assessment reads as an estate with nothing wrong with
it**, in a document a client circulates, which is the one claim Phase 1
forbids. Success now requires an artefact; SCT's own logged errors are captured
regardless of exit code, and `exit_code_said_ok` is recorded separately so the
disagreement is visible rather than resolved silently.

**The privilege finding.** SCT connected to the live XE and then refused for
want of `SELECT ANY DICTIONARY`. The collector account holds
`SELECT_CATALOG_ROLE`, which SCT does not accept as equivalent — the second
time that distinction has cost this project time, after row access in
`05_grant_collector_read.sql`. Script `08_grant_sct_dictionary.sql` written and
**deliberately not run**: it reaches SYS tables including `USER$` and `LINK$`,
which belongs in a DBA's change request rather than a script this project runs
on its own initiative.

One test bug found and fixed while driving the browser: the provenance
assertions read `innerText`, which skips hidden subtrees, so they passed or
failed on whether a phase had run rather than on what the markup says. They
read `textContent` now.

**2026-09-17 (later still) — the grant landed and SCT produced real reports.**

`SELECT ANY DICTIONARY` was granted and SCT ran clean. Results, which are AWS's
verdict and not this project's:

| | `DBMIG_APP` (CLI) | all three schemas (console) |
|---|---|---|
| Occurrences | 33 | 75 |
| Action items | 12 | 14 |
| decision | 1 item / 13 occ | 1 / 27 |
| complex | 6 / 13 | 6 / 20 |
| simple | 5 / 7 | 7 / 28 |

Top items: `5984` specify precision and scale (27 columns), `5550` ROWID
unsupported (10), `9994` queuing objects unconvertible (7), `5028` unsupported
data types, `5200` external tables, `5208` domain indexes.

**The first real run corrected four guesses in `parse.py`, every one of which
mattered.** SCT's 17-column CSV reported **9 columns the parser did not
understand**:

1. **`Action item` is the issue *code*** (`9994`), not a title. Rows would have
   rendered with a blank code.
2. **`Occurrence` is one object's tree path, not a count.** SCT emits one row
   per occurrence; reading it as a number gave 0 for every row. Rows are now
   grouped by action item and counted, as `assess.engine.group_findings` does.
3. **There is no object-name column.** The object is the *leaf* of that path
   (`Schemas.DBMIG_APP.Queuing.Tables.LOAN_EVENT_QTAB` → `LOAN_EVENT_QTAB`) and
   its type the segment before it.
4. **`Group` is the readable problem statement; `Category` is a slug**
   (`queuing-table`) — the reverse of the assumption.

Two more from the filesystem rather than the format: **the CSVs are nested**
under `report/ORACLE/<target>/` rather than flat, and **SCT writes three of
them** — only one holds the action items, the others are rollups. So
`artefact_paths` lists the detail file first; taking whatever sorted first would
have parsed `_Action_Items_Summary.csv` and reported a handful of rows as the
whole assessment.

The self-test fixture is now **SCT's real header and real rows**, captured from
this run, with an assertion that **no column goes unmapped** — the check that
would have caught the original guesswork. `sct.selftest` **79/79**.

Verified on the served files, not on the handlers: the PDF and CSV are
**SHA-256 identical** to what SCT wrote, and the workbook carries all 75
occurrence rows with SCT's own headers plus a provenance sheet.

One browser-test bug fixed on the way: connecting auto-advances to Discover
after ~550ms, so navigating to Assess immediately raced that timer and left the
run button present but invisible. And the live-run wait was 15 minutes against a
run that needs about five plus JVM start — raised to 25, with a timeout now
skipping the result assertions instead of throwing on null and hiding the real
outcome.

**2026-09-17 (fix) — the downloads arrived as GUIDs with no extension.**
Reported from a real `DBMIG_TELCO` full-load run: clicking **PDF** in the
console produced a file named like `775662ec-37a9-421b-a291-f943d097ed7a` that
would not open.

**Cause: SCT names its CSV after the virtual target**, so the real filename is
`PostgreSQL_3cPostgreSQL (virtual)3e.csv` — a space and two parentheses,
straight from the vendor. Those were passed through into
`Content-Disposition`, and when a browser cannot parse that header it discards
the name entirely and falls back to a GUID with **no extension**. The PDF's own
name was clean, which is why this looked intermittent.

`report/export.py` had always sanitised (`.replace(" ", "-")`); this path did
not. `sct.export.safe_filename` now collapses every character that can trip a
header parse, keeps the extension, falls back rather than emitting a bare
`.pdf`, and truncates to Windows' path-component limit. All three routes build
their header through one helper, `web.server._sct_disposition`, and the
self-test asserts **no route builds a disposition by hand** — the refactor that
would reintroduce this.

Verified by downloading all three through real Edge and checking both the
filename and the magic bytes:

```
PDF    aws-sct-rds-postgresql-dbshift-...-rds-postgresql.pdf   %PDF  499679 bytes
Excel  aws-sct-rds-postgresql-PostgreSQL_3cPostgreSQL-virtual-3e.xlsx   PK   13740 bytes
CSV    aws-sct-rds-postgresql-PostgreSQL_3cPostgreSQL-virtual-3e.csv    "Cat 37921 bytes
```

`sct.selftest` **109/109**.

**`DBMIG_TELCO` assessed for the first time**, full load, RDS for PostgreSQL:
**7 action items, 10 occurrences** — 5 simple, 1 complex, 1 decision. Far
cleaner than `DBMIG_APP`'s 12/33, which is worth knowing before a demo picks an
estate to show.

**2026-09-17 (later) — SCT becomes the assessment, and every item is routed.**
Client decision: *"SCT should be main not our rules, and in the assessment phase
UI also hide or remove ours."*

**I argued against this and was wrong on the substance.** The objection was that
removing the 50-rule engine would blind the gate to NOARCHIVELOG, supplemental
logging and missing primary keys. Checking rather than assuming:

- `DBMIG_APP` has **exactly one CRITICAL** — `RDS-004`, external table — and SCT
  reports the same thing as `5200`, finding **two occurrences where the rule
  found one**.
- `OPS-001`, `OPS-002` and `DQ-001` are already **not-applicable on a full-load
  run** (the Phase 1 migration-mode work of 2026-09-16), so they gate nothing.
- For a CDC run, SCT's `5659` covers `DQ-001` directly, and CDC readiness is
  independently checked by the **Connect preflight**, which is a better source
  than the rules engine anyway.

So SCT genuinely covers the blockers. The warning was only ever true for
`OPS-001`/`OPS-002` on a CDC migration, and that evidence exists elsewhere.

### What changed

The **routing table**, `sct/route.py`, is the substantive addition. SCT grades
an action item by *effort* and stops; that does not say where the work lands or
who may do it:

| Where | Meaning | On `DBMIG_APP` |
|---|---|---|
| **Fix in the source** | a defect in Oracle; migrating does not fix it | 2 items, 29 occurrences |
| **Absorb in the target** | nothing wrong with the source; target DDL, extension or setting | 4 items, 9 occurrences |
| **Needs a decision** | no statement exists; somebody must choose | 2 items, 9 occurrences |
| **Needs a person to write it** | code must be rewritten | 6 items, 28 occurrences |

and a second axis for **who may act**: 1 automatic, 7 AI-drafted-then-gated,
6 human-only.

The examples that justify the whole table: `5639` (install `postgres_fdw`) is
`simple` and purely target — one extension, nobody touches Oracle. `5984` (NUMBER
without precision) is `decision` but is a **source** defect that survives the
migration. Effort would have grouped those the wrong way round.

**Three invariants, asserted rather than intended:**

1. **An unmapped SCT code routes to a person**, never to automatic, and is
   surfaced in the console as unmapped. Same instinct as
   `blocker/policy.BLOCKS`, where an unrecognised critical blocks everything:
   silence must not read as safety.
2. **Nothing is ever auto-applied to the source.** An automatic statement
   against a client's production Oracle is not something this project does, and
   the self-test enforces it per row.
3. **The model never decides the routing.** It drafts fix *text* for items
   routed to it; where a fix belongs, who may apply it and what it blocks stay
   in this table. Otherwise two runs of the same estate could disagree about
   what halts a phase — the thing `docs/phases/phase-02-assess.md` exists to
   prevent. The self-test asserts `route.py` imports no model client.

### The UI

The Assess screen now leads with SCT, and **an SCT run advances the stage rail**
— previously only the rules run did, which would have left the screen complete
and every later stage locked. The rules engine's run button, result panel and
empty state are **hidden, not deleted**: Phases 3, 7, 9 and 10 still call those
render paths, and deleting the elements would break them. The engine still runs
headlessly and still writes `assessment.json`, so the gate, sizing, the DMS
preflight and the report keep their evidence — verified after the change:
`python -m blocker.run` still reports `blocked by RDS-004`.

`sct.selftest` **191/191**; `drive_sct.js` **37/37** in headless Edge.

One test bug fixed on the way: the endpoint-gating assertions expected 409
"before a run" unconditionally, which fails on a console that already holds a
result — a long-running one usually does. The expectation is now derived from
the server's own state and both directions are still checked.

**Still to do** (steps 3–5 of the plan): Phase 4 drafting fixes for SCT items
through the gates with a **separate PostgreSQL allow-list** for target-side
statements — the Oracle allow-list must not be loosened, it is what protects the
source; Phase 5 splitting its halt output by source/target/decision and reading
CDC readiness from the Connect preflight; and re-pointing Phase 3 sizing to
SCT's complexity buckets.

**2026-09-17 (console) — Phases 1-5 run end to end in the browser, 45/45.**
`scripts/console-test/drive_sct_1to5.js`: Connect → Discover → Assess (AWS SCT)
→ Remediate (AI drafts, gates decide) → Blocker gate (split by where the fix
belongs), then a reload and phone width.

**The cache key was wrong, and the drive is what exposed it.** It included the
**collector run id**, so every fresh discovery invalidated the SCT result — and
the drive runs discovery first, so SCT re-ran for 25+ minutes on every attempt.
That is wrong outside the test too: **SCT connects to Oracle and reads the data
dictionary itself; it never opens a collector run**, so a new run id changes
nothing SCT would see.

The key is now the **schemas plus the target** (`sct.runner._cache_key`), which
is what a result actually depends on. A schema-list change still invalidates it,
correctly — that genuinely is a different assessment. Seven existing result
directories were migrated to the new keys; **four turned out to be duplicates**
of the same estate under different run ids, which is the bug measuring itself.

Also fixed, all found by running it:

- **A restore-on-load race wiped completed work off the screen.** The reload
  path re-showed the "run this first" panel unconditionally, and because it is
  awaited on load it could land *after* the operator had started a run. It now
  only shows an empty state where there is nothing to show, and never
  overwrites an already-rendered result.
- **Discover's tiles were a flake** — 8 on one run, 0 on the next. The stage is
  marked done before `renderDiscovery()` paints, so the drive waited for the
  tiles rather than the stage class.
- **Three `waitForFunction` polls were unreliable against an open
  `EventSource`.** The same expression timed out from the page while a
  test-side poll watching it saw the render at t+58s. All three now poll from
  the test, and a timeout reports how far it got instead of just "timed out".
- **`/api/state` now reports the SCT path's own state** —
  `has_sct_assessment`, `has_sct_remediation`, `has_sct_gate`, `model_mode` —
  so the reload path restores from one call instead of probing endpoints and
  inferring from their 409s.

**Measured:** a cached SCT assessment returns in seconds, a fresh one on the
three-schema estate takes over 25 minutes, and a live-model Phase 4 over 14
action items takes about 60 seconds.
