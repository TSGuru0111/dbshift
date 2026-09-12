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
```

Screenshots land in `shots/`. Set `DBSHIFT_URL` to point at another port.

The same browser is available interactively to Claude Code through the
Playwright MCP server registered in the repo's `.mcp.json`
(`npx @playwright/mcp@latest --browser msedge`); approve it when a new
session asks.
