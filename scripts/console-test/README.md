# Console drive (Playwright)

Runs the console through Connect → Discover → Assess → Size → Convert (4b) →
Gate → Report (10) in headless Edge, then reloads and checks phone width.
Phases 6–9 are not driven: they need AWS credentials and would bill a target.

```powershell
cd scripts\console-test
npm install                                   # once; pulls playwright-core
$env:DBSHIFT_COLLECTOR_PASSWORD = '...'       # the read-only collector account
$env:DBSHIFT_PG_PASSWORD = 'dbshift-local-only'  # the Docker PostgreSQL, if not default
npm test                                      # console must be up on 127.0.0.1:8765
node drive_paths.js                           # the Phase 3 two-path chooser
```

Screenshots land in `shots/` and `shots-paths/`. Set `DBSHIFT_URL` to point at another port.

The same browser is available interactively to Claude Code through the
Playwright MCP server registered in the repo's `.mcp.json`
(`npx @playwright/mcp@latest --browser msedge`); approve it when a new
session asks.


## Phases 3–5 on a MySQL source — `drive_mysql_3to5.js`

```powershell
# the console must be up with a connected MySQL session that has run discovery
node drive_mysql_3to5.js                   # both pairs, 148/148 (2026-09-30)
$env:PAIRS='MYSQL'; node drive_mysql_3to5.js   # one pair
```

Walks Target & Sizing, Remediate, 4b, 4c, 4d and the Blocker gate for
MySQL → RDS for PostgreSQL and MySQL → RDS for MySQL, at 1440x900 and 390x844.
Fails on Oracle-only wording (visible text, hint tooltips, the topbar), on 4b–4d
looking runnable on the homogeneous pair or disabled on the heterogeneous one, on a
JS error, on a failing `/api/` call (409 and Phase 9's known `/api/cutover` 400
excluded) and on sideways scroll. Sets the Phase 3 target itself and reuses SCT's
cached assessments; re-drafts Phase 4 (Bedrock) only when the server's plan is for
the other target. Ends with an in-page Oracle spot check that flips `SRC` without
touching the server, so the MySQL session survives. Run it **before**
`drive_srcpick.js`, which resets the session.


## The source-engine picker — `drive_srcpick.js`

```powershell
npm install                                # once
# the console must be up on 127.0.0.1:8765
node drive_srcpick.js                      # 28/28
```

Drives the Connect picker and everything downstream of the `(source, target)`
pair: the DSN label and default rewiring, the schema placeholder's casing, the
target picker refusing RDS for Oracle from a MySQL source, the MySQL-only Discover
panels, the not-applicable banners on 4b/4c/4d, and Data Pump versus DMS.

**It resets the source engine to Oracle before loading the page.** The console
holds it in server state, so a previous run leaves it set and the first click is a
no-op — which makes every later assertion fail for a reason unrelated to the code.

**It waits on the POST response, not a timeout.** `waitForTimeout` raced the fetch
and lost intermittently; an hour went into chasing a UI bug that did not exist.
`waitForResponse` plus `waitForFunction` pins the post-condition instead.

**It asserts on JavaScript errors, not on every console line.** A 409 from `/api/`
is the expected answer for a record that does not exist yet, and Chromium logs the
failed fetch as a console error. The count of ignored 4xx is printed rather than
hidden, so a real one is still visible.
