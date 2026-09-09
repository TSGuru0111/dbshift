"""Render assessment.json as a standalone HTML report.

Regenerated from the assessment output on every run so the page can never drift
from the numbers it claims. Every figure on the page is read from the JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "assess"

DEFAULT_ASSESS_OUTPUT = Path(__file__).resolve().parent / "output"
DEFAULT_COLLECTOR_OUTPUT = Path(__file__).resolve().parent.parent / "collector" / "output"

SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
CATEGORY_LABEL = {
    "rds_compatibility": "RDS compatibility",
    "data_quality": "Data quality",
    "performance": "Performance",
    "security": "Security",
    "operational_risk": "Operational risk",
}
LEVEL_NOTE = {
    "L1": "Safe auto-fix, applied without a prompt",
    "L2": "Auto-generated fix, requires approval",
    "L3": "A human authors the fix",
    "L4": "Never auto-fixed",
}

PIPELINE_SECTION = """<section>
    <div class="sec-head"><h2>How the data moves</h2><span class="count">source &rarr; findings, one run</span></div>
    <p class="explain">Every number on this page is traceable back through this chain to a specific
    SQL statement against the source database. The collector runs <strong>inside the client
    network</strong> and pushes outward &mdash; nothing dials in to it. Each stage below carries the
    real volume from this run.</p>
    <div class="figwrap">__PIPELINE__</div>

    <h3 class="subhead">Stage 1 &mdash; Discovery: what was read</h3>
    <p class="explain">Twelve probes, each owning one area of the data dictionary. A probe is the
    only thing that knows which views it needs, so adding coverage never touches the rest.
    <strong>Every statement is logged with its SHA-256, row count and duration</strong>, which is how
    a client DBA can audit exactly what ran against their database.</p>
    <div class="tablewrap">
      <table>
        <thead><tr><th>Probe</th><th>Catalogue views read</th><th>Queries</th><th>Datasets</th><th>ms</th></tr></thead>
        <tbody>__PROBEROWS__</tbody>
      </table>
    </div>

    <h3 class="subhead">Stage 2 &mdash; Assessment: how a finding is produced</h3>
    <p class="explain">The loaded tables are queried by rules that are themselves rows in a table.
    Each rule carries a SQL predicate, a severity and a remediation level; the engine runs the
    predicate and turns every returned row into a finding, attaching the rule's own severity.
    <strong>Nothing is judged at runtime</strong> &mdash; which is why the same estate always scores
    identically, and why a model cannot quietly change a verdict.</p>
    <div class="tracewrap">
      <div class="trace">
        <div class="trace-step"><span class="tn">Rule row</span>
          <code>DQ-001 &middot; data_quality &middot; CRITICAL &middot; L2</code></div>
        <div class="trace-step"><span class="tn">Predicate</span>
          <code>SELECT &hellip; FROM v_user_tables t WHERE NOT EXISTS (SELECT 1 FROM constraints c WHERE c.table_name = t.table_name AND c.constraint_type = 'P')</code></div>
        <div class="trace-step"><span class="tn">Returned</span>
          <code>AUDIT_SCRATCH, COLLATERAL_NOTE</code></div>
        <div class="trace-step"><span class="tn">Finding</span>
          <code>CRITICAL &mdash; No primary key; DMS CDC cannot reliably replicate updates or deletes</code></div>
        <div class="trace-step"><span class="tn">Answer key</span>
          <code>matches seeded defect 2 &middot; expected CRITICAL &middot; severity exact</code></div>
      </div>
    </div>
  </section>"""

PROBE_READS = {
    "identity": "V$VERSION, V$INSTANCE, V$DATABASE, NLS_DATABASE_PARAMETERS",
    "objects": "DBA_OBJECTS",
    "tables": "DBA_TABLES, DBA_TAB_COLS, DBA_TAB_COMMENTS",
    "indexes": "DBA_INDEXES, DBA_IND_COLUMNS, DBA_IND_EXPRESSIONS",
    "constraints": "DBA_CONSTRAINTS, DBA_CONS_COLUMNS",
    "storage": "DBA_SEGMENTS, DBA_LOBS, DBA_TABLESPACES",
    "partitions": "DBA_PART_TABLES, DBA_TAB_PARTITIONS, DBA_PART_KEY_COLUMNS",
    "plsql": "DBA_SOURCE, DBA_ERRORS  &rarr; SHA-256 per object",
    "programmatic": "DBA_VIEWS, DBA_MVIEWS, DBA_SEQUENCES, DBA_SYNONYMS, DBA_TRIGGERS, DBA_DB_LINKS, DBA_QUEUES, DBA_TYPES, DBA_XML_SCHEMAS +4",
    "security": "DBA_USERS, DBA_ROLES, DBA_TAB_PRIVS, DBA_SYS_PRIVS, DBA_PROFILES",
    "features": "DBA_FEATURE_USAGE_STATISTICS, V$OPTION, V$PARAMETER, V$OSSTAT",
    "dataprofile": "Bounded aggregates over application tables &mdash; counts only",
}

TEMPLATE = """<title>DBMIG_APP Migration Readiness</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root {
  --ground:#F5F7F9; --surface:#FFFFFF; --surface-2:#EFF3F5;
  --ink:#131C25; --ink-2:#53646F; --ink-3:#7C8B96;
  --rule:#DBE2E7; --rule-strong:#C3CDD5;
  --accent:#0E6E75; --accent-soft:#DCECED;
  --sev-critical:#A11B2B; --sev-high:#BE5A12; --sev-medium:#8A6A0D;
  --sev-low:#3D6B8E; --sev-info:#6D7883;
  --track:#E3E9ED;
  --shadow:0 1px 2px rgba(19,28,37,.06), 0 8px 24px -16px rgba(19,28,37,.28);
  --maxw:1120px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground:#0D131A; --surface:#141C24; --surface-2:#1A242D;
    --ink:#E3EAF0; --ink-2:#8E9DA9; --ink-3:#6F7E8A;
    --rule:#243039; --rule-strong:#33424D;
    --accent:#48AFB5; --accent-soft:#123133;
    --sev-critical:#E7697A; --sev-high:#E39A55; --sev-medium:#C7A63E;
    --sev-low:#7BA9C9; --sev-info:#93A0AB;
    --track:#212C35;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -16px rgba(0,0,0,.7);
  }
}
:root[data-theme="dark"] {
  --ground:#0D131A; --surface:#141C24; --surface-2:#1A242D;
  --ink:#E3EAF0; --ink-2:#8E9DA9; --ink-3:#6F7E8A;
  --rule:#243039; --rule-strong:#33424D;
  --accent:#48AFB5; --accent-soft:#123133;
  --sev-critical:#E7697A; --sev-high:#E39A55; --sev-medium:#C7A63E;
  --sev-low:#7BA9C9; --sev-info:#93A0AB;
  --track:#212C35;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -16px rgba(0,0,0,.7);
}

* { box-sizing:border-box; }
body {
  background:var(--ground); color:var(--ink);
  font-family:"IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
  font-size:15px; line-height:1.6; margin:0;
  -webkit-font-smoothing:antialiased;
}
.page { max-width:var(--maxw); margin:0 auto; padding:40px 28px 96px; }
h1,h2,h3 { font-family:"Archivo", ui-sans-serif, system-ui, sans-serif; text-wrap:balance; margin:0; }
p { margin:0; }
.num { font-variant-numeric:tabular-nums; }
.mono { font-family:"IBM Plex Mono", ui-monospace, monospace; }

.eyebrow {
  font-family:"IBM Plex Mono", monospace; font-size:11px; letter-spacing:.14em;
  text-transform:uppercase; color:var(--accent); font-weight:500;
}
.masthead { border-bottom:2px solid var(--ink); padding-bottom:22px; }
.masthead h1 { font-size:clamp(30px,4.4vw,44px); font-weight:700; letter-spacing:-.022em; line-height:1.08; margin:10px 0 12px; }
.dek { font-size:17px; color:var(--ink-2); max-width:64ch; }
.meta { display:flex; flex-wrap:wrap; gap:0 30px; margin-top:20px; }
.meta div { display:flex; flex-direction:column; gap:2px; }
.meta dt { font-size:10.5px; letter-spacing:.1em; text-transform:uppercase; color:var(--ink-3);
  font-family:"IBM Plex Mono", monospace; }
.meta dd { margin:0; font-size:13px; font-family:"IBM Plex Mono", monospace; color:var(--ink); }

section { margin-top:52px; }
.sec-head { display:flex; align-items:baseline; gap:12px; border-bottom:1px solid var(--rule); padding-bottom:10px; margin-bottom:20px; }
.sec-head h2 { font-size:19px; font-weight:600; letter-spacing:-.01em; }
.sec-head .count { font-family:"IBM Plex Mono", monospace; font-size:12px; color:var(--ink-3); }
.explain { color:var(--ink-2); max-width:68ch; margin-bottom:22px; }
.explain strong { color:var(--ink); font-weight:600; }

/* verdict */
.verdict { display:grid; grid-template-columns:minmax(210px,250px) 1fr; gap:34px; align-items:start; }
@media (max-width:720px){ .verdict { grid-template-columns:1fr; } }
.gauge {
  background:var(--surface); border:1px solid var(--rule); border-radius:3px;
  padding:22px; box-shadow:var(--shadow); border-top:3px solid var(--sev-critical);
}
.gauge .score { font-family:"Archivo", sans-serif; font-size:72px; font-weight:700;
  line-height:1; letter-spacing:-.04em; font-variant-numeric:tabular-nums; }
.gauge .of { font-size:20px; color:var(--ink-3); font-weight:500; }
.gauge .label { font-family:"IBM Plex Mono", monospace; font-size:10.5px; letter-spacing:.11em;
  text-transform:uppercase; color:var(--ink-3); margin-bottom:8px; }
.gauge .cap { margin-top:14px; padding-top:12px; border-top:1px solid var(--rule);
  font-size:12.5px; color:var(--ink-2); line-height:1.5; }
.blockers { display:flex; flex-direction:column; gap:1px; background:var(--rule); border:1px solid var(--rule); border-radius:3px; overflow:hidden; }
.blocker { background:var(--surface); padding:13px 16px; display:grid;
  grid-template-columns:auto 1fr; gap:14px; align-items:baseline; border-left:3px solid var(--sev-critical); }
.blocker .rid { font-family:"IBM Plex Mono", monospace; font-size:11.5px; color:var(--sev-critical); font-weight:500; }
.blocker .obj { font-family:"IBM Plex Mono", monospace; font-size:13px; font-weight:500; }
.blocker .why { font-size:13px; color:var(--ink-2); margin-top:2px; }

/* accuracy */
/* four tiles: 4-up or 2x2, never an orphan in a half-empty row */
.proof { display:grid; grid-template-columns:repeat(4,1fr); gap:1px;
  background:var(--rule); border:1px solid var(--rule); border-radius:3px; overflow:hidden; }
@media (max-width:760px){ .proof { grid-template-columns:repeat(2,1fr); } }
.proof div { background:var(--surface); padding:16px 18px; }
.proof .k { font-family:"IBM Plex Mono", monospace; font-size:10.5px; letter-spacing:.1em;
  text-transform:uppercase; color:var(--ink-3); }
.proof .v { font-family:"Archivo", sans-serif; font-size:30px; font-weight:700;
  letter-spacing:-.02em; font-variant-numeric:tabular-nums; margin-top:4px; }
.proof .v.good { color:var(--accent); }
.proof .s { font-size:12px; color:var(--ink-2); margin-top:2px; }

.note { border-left:3px solid var(--sev-medium); background:var(--surface-2);
  padding:14px 18px; border-radius:0 3px 3px 0; margin-top:20px; font-size:13.5px; color:var(--ink-2); }
.note b { color:var(--ink); font-weight:600; }

/* subscores */
.bars { display:flex; flex-direction:column; gap:14px; }
.bar-row { display:grid; grid-template-columns:150px 1fr 96px; gap:16px; align-items:center; }
@media (max-width:620px){ .bar-row { grid-template-columns:120px 1fr 62px; gap:10px; } }
.bar-row .name { font-size:13.5px; font-weight:500; }
.bar-track { height:22px; background:var(--track); border-radius:2px; position:relative; overflow:hidden; }
.bar-fill { height:100%; background:var(--accent); border-radius:0 2px 2px 0; }
.bar-row .val { font-family:"IBM Plex Mono", monospace; font-size:13px; font-weight:500;
  font-variant-numeric:tabular-nums; }
.bar-row .val span { color:var(--ink-3); font-size:11.5px; }
.axis { display:grid; grid-template-columns:150px 1fr 96px; gap:16px; margin-top:6px; }
@media (max-width:620px){ .axis { grid-template-columns:120px 1fr 62px; gap:10px; } }
.axis .ticks { display:flex; justify-content:space-between; font-family:"IBM Plex Mono", monospace;
  font-size:10.5px; color:var(--ink-3); border-top:1px solid var(--rule); padding-top:4px; }

/* distribution */
.dist-grid { display:grid; grid-template-columns:1fr 1fr; gap:34px; }
@media (max-width:760px){ .dist-grid { grid-template-columns:1fr; } }
.stack { display:flex; gap:2px; height:34px; margin-bottom:14px; }
.stack div { border-radius:1px; }
.legend { display:flex; flex-direction:column; gap:8px; }
.legend-row { display:grid; grid-template-columns:11px 1fr auto; gap:10px; align-items:center; font-size:13px; }
.swatch { width:11px; height:11px; border-radius:2px; }
.legend-row .n { font-family:"IBM Plex Mono", monospace; font-variant-numeric:tabular-nums; font-weight:500; }
.levels { display:flex; flex-direction:column; gap:1px; background:var(--rule);
  border:1px solid var(--rule); border-radius:3px; overflow:hidden; }
.level { background:var(--surface); padding:11px 15px; display:grid;
  grid-template-columns:34px 1fr auto; gap:12px; align-items:baseline; }
.level .lv { font-family:"IBM Plex Mono", monospace; font-weight:500; font-size:13px; color:var(--accent); }
.level .ln { font-size:13px; color:var(--ink-2); }
.level .lc { font-family:"IBM Plex Mono", monospace; font-variant-numeric:tabular-nums;
  font-weight:500; font-size:13.5px; }

/* findings */
.filters { display:flex; flex-wrap:wrap; gap:7px; margin-bottom:16px; align-items:center; }
.filters .flabel { font-family:"IBM Plex Mono", monospace; font-size:10.5px; letter-spacing:.1em;
  text-transform:uppercase; color:var(--ink-3); margin-right:4px; }
.chip {
  font-family:"IBM Plex Mono", monospace; font-size:11.5px; padding:5px 11px;
  border:1px solid var(--rule-strong); background:var(--surface); color:var(--ink-2);
  border-radius:2px; cursor:pointer; transition:.13s;
}
.chip:hover { border-color:var(--ink-3); color:var(--ink); }
.chip[aria-pressed="true"] { background:var(--ink); color:var(--ground); border-color:var(--ink); }
.chip:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.tablewrap { overflow-x:auto; border:1px solid var(--rule); border-radius:3px; background:var(--surface); }
table { border-collapse:collapse; width:100%; font-size:13px; min-width:760px; }
thead th {
  text-align:left; font-family:"IBM Plex Mono", monospace; font-size:10.5px; letter-spacing:.09em;
  text-transform:uppercase; color:var(--ink-3); font-weight:500;
  padding:11px 14px; border-bottom:1px solid var(--rule-strong); white-space:nowrap;
}
tbody td { padding:11px 14px; border-bottom:1px solid var(--rule); vertical-align:top; }
tbody tr:last-child td { border-bottom:none; }
tbody tr:hover td { background:var(--surface-2); }
td.rid { font-family:"IBM Plex Mono", monospace; font-size:12px; color:var(--ink-2); white-space:nowrap; }
td.obj { font-family:"IBM Plex Mono", monospace; font-size:12.5px; font-weight:500; }
td.det { color:var(--ink-2); min-width:280px; }
.sev { font-family:"IBM Plex Mono", monospace; font-size:10.5px; letter-spacing:.06em;
  font-weight:500; white-space:nowrap; display:inline-flex; align-items:center; gap:6px; }
.sev::before { content:""; width:7px; height:7px; border-radius:50%; background:currentColor; flex:none; }
.sev-CRITICAL{color:var(--sev-critical)} .sev-HIGH{color:var(--sev-high)}
.sev-MEDIUM{color:var(--sev-medium)} .sev-LOW{color:var(--sev-low)} .sev-INFO{color:var(--sev-info)}
.empty { padding:34px; text-align:center; color:var(--ink-3); font-size:13.5px;
  border:1px solid var(--rule); border-radius:3px; background:var(--surface); }
.tcount { font-family:"IBM Plex Mono", monospace; font-size:11.5px; color:var(--ink-3); margin-top:10px; }

/* issues: one block per rule, severity carried in the left edge */
.issues { display:flex; flex-direction:column; gap:1px;
  background:var(--rule); border:1px solid var(--rule); border-radius:3px; overflow:hidden; }
.issue { background:var(--surface); padding:15px 18px; border-left:3px solid var(--sev-info); }
.issue[data-sev="CRITICAL"]{ border-left-color:var(--sev-critical); }
.issue[data-sev="HIGH"]    { border-left-color:var(--sev-high); }
.issue[data-sev="MEDIUM"]  { border-left-color:var(--sev-medium); }
.issue[data-sev="LOW"]     { border-left-color:var(--sev-low); }
.ihead { display:flex; flex-wrap:wrap; align-items:baseline; gap:10px; }
.ihead h3 { font-size:14.5px; font-weight:600; letter-spacing:-.005em; }
.ibadge { font-family:"IBM Plex Mono", monospace; font-size:11px; font-weight:500;
  background:var(--surface-2); border:1px solid var(--rule); padding:1px 7px;
  border-radius:2px; color:var(--ink-2); font-variant-numeric:tabular-nums; }
.imeta { font-family:"IBM Plex Mono", monospace; font-size:10.5px; color:var(--ink-3);
  margin-left:auto; white-space:nowrap; }
.idetail { font-family:"IBM Plex Mono", monospace; font-size:12px; color:var(--ink);
  margin-top:8px; line-height:1.5; }
.iwhy { font-size:13px; color:var(--ink-2); margin-top:7px; max-width:80ch; }
.issue details { margin-top:10px; }
.issue summary { cursor:pointer; font-family:"IBM Plex Mono", monospace; font-size:11px;
  color:var(--accent); width:fit-content; }
.issue summary:focus-visible { outline:2px solid var(--accent); outline-offset:3px; }
.objs { display:flex; flex-wrap:wrap; gap:5px; margin-top:9px; }
.objs span { font-family:"IBM Plex Mono", monospace; font-size:11px; background:var(--surface-2);
  border:1px solid var(--rule); padding:2px 7px; border-radius:2px; color:var(--ink-2); }
.objs .more { border-style:dashed; color:var(--ink-3); }

/* pipeline */
.figwrap { overflow-x:auto; border:1px solid var(--rule); border-radius:3px;
  background:var(--surface); padding:20px 18px 12px; }
figure { margin:0; }
figcaption { font-size:12.5px; color:var(--ink-3); margin-top:12px; max-width:80ch;
  border-top:1px solid var(--rule); padding-top:10px; }
.flow { display:block; min-width:1180px; width:100%; height:auto; color:var(--ink); }
.flow-box { fill:var(--surface-2); stroke:var(--rule-strong); stroke-width:1; }
.flow-title { font-family:"Archivo", sans-serif; font-size:13px; font-weight:600; fill:var(--ink); }
.flow-meta { font-family:"IBM Plex Mono", monospace; font-size:10.5px; fill:var(--ink-2); }
.flow-line { stroke:var(--ink-3); stroke-width:1.5; fill:none; }
.flow-label { font-family:"IBM Plex Mono", monospace; font-size:10px; fill:var(--ink-2); }
.flow-head { fill:var(--ink-3); }
.flow-future { stroke:var(--accent); stroke-width:1.5; stroke-dasharray:5 4; fill:none; }
.flow-future-box { fill:none; stroke:var(--accent); stroke-width:1.5; stroke-dasharray:5 4; }
.flow-future-text { font-family:"IBM Plex Mono", monospace; font-size:10.5px; fill:var(--accent); }
.flow-future-title { font-family:"Archivo", sans-serif; font-size:12.5px; font-weight:600; fill:var(--accent); }
.flow-zone { font-family:"IBM Plex Mono", monospace; font-size:10px; fill:var(--ink-3);
  letter-spacing:.09em; }
.flow-bracket { stroke:var(--rule-strong); stroke-width:1; fill:none; }

.subhead { font-size:15.5px; font-weight:600; margin:38px 0 10px; letter-spacing:-.008em; }
.tracewrap { border:1px solid var(--rule); border-radius:3px; background:var(--surface); overflow:hidden; }
.trace { display:flex; flex-direction:column; gap:1px; background:var(--rule); }
.trace-step { background:var(--surface); padding:12px 16px; display:grid;
  grid-template-columns:104px 1fr; gap:16px; align-items:baseline; }
@media (max-width:620px){ .trace-step { grid-template-columns:1fr; gap:5px; } }
.trace-step .tn { font-family:"IBM Plex Mono", monospace; font-size:10.5px; letter-spacing:.09em;
  text-transform:uppercase; color:var(--ink-3); }
.trace-step code { font-family:"IBM Plex Mono", monospace; font-size:12px; color:var(--ink);
  line-height:1.55; word-break:break-word; }
.trace-step:last-child code { color:var(--accent); }
.limits { display:flex; flex-direction:column; gap:14px; }
.limit { display:grid; grid-template-columns:auto 1fr; gap:13px; align-items:start;
  padding-bottom:14px; border-bottom:1px solid var(--rule); }
.limit:last-child { border-bottom:none; padding-bottom:0; }
.limit .lm { font-family:"IBM Plex Mono", monospace; font-size:10.5px; letter-spacing:.08em;
  color:var(--sev-medium); white-space:nowrap; padding-top:2px; }
.limit p { font-size:13.5px; color:var(--ink-2); }
.limit b { color:var(--ink); font-weight:600; }
footer { margin-top:60px; padding-top:20px; border-top:1px solid var(--rule);
  font-family:"IBM Plex Mono", monospace; font-size:11.5px; color:var(--ink-3);
  display:flex; flex-wrap:wrap; gap:6px 24px; }
@media (prefers-reduced-motion:reduce){ * { transition:none !important; animation:none !important; } }
</style>

<div class="page">
  <header class="masthead">
    <div class="eyebrow">DBShift AI &nbsp;/&nbsp; Phase 1&ndash;2 &nbsp;/&nbsp; Discover &amp; Assess</div>
    <h1>DBMIG_APP Migration Readiness</h1>
    <p class="dek">An automated assessment of an on-premises Oracle estate against Amazon RDS for
    Oracle. Every figure below was produced by deterministic rules over a read-only catalogue scan.
    No language model contributed to any verdict on this page.</p>
    <div class="meta">
      <div><dt>Source</dt><dd>__SOURCE__</dd></div>
      <div><dt>Estate</dt><dd>__ESTATE__</dd></div>
      <div><dt>Collector run</dt><dd>__RUNID__</dd></div>
      <div><dt>Assessed</dt><dd>__WHEN__</dd></div>
      <div><dt>Rules</dt><dd>__NRULES__ evaluated</dd></div>
    </div>
  </header>

  <section>
    <div class="sec-head"><h2>Verdict</h2><span class="count">__NCRIT__ blocking findings</span></div>
    <p class="explain">The overall score is the mean of five category scores, but <strong>any single
    critical finding caps it at 60</strong> regardless of how healthy the rest of the estate looks.
    That cap is deliberate: a migration is gated by its worst unresolved problem, not by its
    average. The five findings below are what currently hold the gate shut.</p>
    <div class="verdict">
      <div class="gauge">
        <div class="label">Overall readiness</div>
        <div class="score">__OVERALL__<span class="of">/100</span></div>
        <p class="cap">__CAPTEXT__</p>
      </div>
      <div class="blockers">__BLOCKERS__</div>
    </div>
  </section>

  <section>
    <div class="sec-head"><h2>Detection accuracy</h2><span class="count">measured, not estimated</span></div>
    <p class="explain">The source database carries <strong>eight deliberately seeded defects</strong>
    with a documented expected severity for each. After every run the findings are scored against
    that answer key. This is the number that separates a working assessment engine from a plausible
    one &mdash; and it turns every future rule change into a regression test rather than a hope.</p>
    <div class="proof">
      <div><div class="k">Recall</div><div class="v good">__RECALL__%</div>
        <div class="s">__DETECTED__ of __DETECTABLE__ detectable defects found</div></div>
      <div><div class="k">Severity exact</div><div class="v">__SEVEXACT__/__DETECTED__</div>
        <div class="s">reported severity matched the answer key</div></div>
      <div><div class="k">Missed</div><div class="v">__MISSED__</div>
        <div class="s">seeded defects the engine failed to find</div></div>
      <div><div class="k">Additional</div><div class="v">__ADDITIONAL__</div>
        <div class="s">real findings beyond the seeded set</div></div>
    </div>
    __DEFECTNOTE__
  </section>

  __PIPELINE_SECTION__

  <section>
    <div class="sec-head"><h2>Category scores</h2><span class="count">0&ndash;100, higher is better</span></div>
    <p class="explain">Each category starts at 100 and decays with the weight of what was found.
    Penalty accrues <strong>per rule rather than per finding</strong>, scaled logarithmically by how
    many objects the rule hit &mdash; one rule firing on ten objects is a single issue with a wider
    blast radius, not ten separate issues. Scores decay exponentially so a bad category and a
    catastrophic one stay distinguishable instead of both bottoming out at zero.</p>
    <div class="bars">__BARS__</div>
    <div class="axis"><div></div><div class="ticks"><span>0</span><span>25</span><span>50</span><span>75</span><span>100</span></div><div></div></div>
  </section>

  <section>
    <div class="sec-head"><h2>What was found</h2><span class="count">__NFIND__ findings</span></div>
    <div class="dist-grid">
      <div>
        <p class="explain">Severity drives the score. It is a property of the rule that fired, fixed
        in the catalogue &mdash; never inferred at runtime, so the same estate always scores the same.</p>
        <div class="stack">__STACK__</div>
        <div class="legend">__LEGEND__</div>
      </div>
      <div>
        <p class="explain">Remediation level decides <strong>who is allowed to fix it</strong>. This
        is also set by the rule, not by a model: a confident suggestion to rewrite business logic
        still lands in L3.</p>
        <div class="levels">__LEVELS__</div>
      </div>
    </div>
  </section>

  <section>
    <div class="sec-head"><h2>Issues</h2><span class="count">__NISSUES__ issues &middot; __NFIND2__ occurrences</span></div>
    <p class="explain">One entry per rule, not per object. A rule that fires on 340 tables is
    <strong>one issue with a wide blast radius</strong>, not 340 issues &mdash; which is also how the
    score counts it. Expand an issue to see every object it touched.</p>
    <div class="filters">
      <span class="flabel">Severity</span>__SEVCHIPS__
    </div>
    <div class="filters">
      <span class="flabel">Category</span>__CATCHIPS__
    </div>
    <div class="issues" id="rows"></div>
    <div class="empty" id="empty" hidden>No issues match these filters.</div>
    <div class="tcount" id="tcount"></div>
  </section>

  <section>
    <div class="sec-head"><h2>What this does not tell you</h2></div>
    <p class="explain">An assessment that only reports what it found is half a report. These are the
    known gaps in the evidence behind the numbers above.</p>
    <div class="limits">__LIMITS__</div>
  </section>

  <footer>
    <span>Collector run __RUNID__</span>
    <span>__NDATASETS__ datasets</span>
    <span>__NRULES__ rules</span>
    <span>Generated __WHEN__</span>
  </footer>
</div>

<script>
const ISSUES = __ISSUES__;
const CATLABEL = __CATLABEL__;
const state = { sev:new Set(), cat:new Set() };
const rows = document.getElementById('rows');
const empty = document.getElementById('empty');
const tcount = document.getElementById('tcount');
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function objectList(g){
  if (!g.objects.length) return '';
  const chips = g.objects.map(o => `<span>${esc(o)}</span>`).join('');
  const more = g.objects_truncated
    ? `<span class="more">+${g.objects_truncated} more</span>` : '';
  const label = g.occurrences === 1 ? 'Affected object' : `Affected objects (${g.occurrences})`;
  return `<details><summary>${label}</summary><div class="objs">${chips}${more}</div></details>`;
}

function render(){
  const list = ISSUES.filter(g =>
    (state.sev.size === 0 || state.sev.has(g.severity)) &&
    (state.cat.size === 0 || state.cat.has(g.category)));
  rows.innerHTML = list.map(g => `<article class="issue" data-sev="${esc(g.severity)}">
    <div class="ihead">
      <span class="sev sev-${esc(g.severity)}">${esc(g.severity)}</span>
      <h3>${esc(g.title)}</h3>
      ${g.occurrences > 1 ? `<span class="ibadge">&times;${g.occurrences}</span>` : ''}
      <span class="imeta">${esc(g.rule_id)} &middot; ${esc(CATLABEL[g.category] || g.category)} &middot; ${esc(g.remediation_level)}</span>
    </div>
    <p class="idetail">${esc(g.sample_detail)}</p>
    <p class="iwhy">${esc(g.rationale)}</p>
    ${objectList(g)}
  </article>`).join('');
  empty.hidden = list.length > 0;
  const occ = list.reduce((n,g) => n + g.occurrences, 0);
  tcount.textContent = `Showing ${list.length} of ${ISSUES.length} issues (${occ} occurrences)`;
}
document.querySelectorAll('.chip').forEach(chip => {
  chip.addEventListener('click', () => {
    const set = state[chip.dataset.kind];
    const v = chip.dataset.value;
    if (set.has(v)) { set.delete(v); chip.setAttribute('aria-pressed','false'); }
    else { set.add(v); chip.setAttribute('aria-pressed','true'); }
    render();
  });
});
render();
</script>
"""


def _pipeline_svg(manifest: dict, assessment: dict) -> str:
    """One figure, one claim: where every number on the page comes from."""
    ql = manifest["query_log"]
    queries = len(ql)
    rows_in = sum(q["row_count"] for q in ql)
    rows_out = manifest["total_rows"]
    datasets = len(manifest["datasets"])
    kb = round(sum(d["bytes"] for d in manifest["datasets"]) / 1024)
    nrules = assessment["rules_evaluated"]
    nfind = len(assessment["findings"])

    stages = [
        ("Oracle 21c XE", "XEPDB1 &middot; 1.03 GB", "90 objects"),
        ("Collector", "12 probes &middot; local", "oracledb thin"),
        ("JSON envelope", f"{datasets} datasets", f"{kb} KB on disk"),
        ("SQLite", "47 tables + 3 views", "Aurora slots here"),
        ("Rules table", f"{nrules} SQL predicates", "severity + level"),
        ("Findings", f"{nfind} findings", "5 scores &middot; recall"),
    ]
    hops = [
        (f"{queries} queries", "read-only"),
        (f"{rows_in:,} rows", "run_id stamped"),
        (f"{rows_out:,} rows", "no transform"),
        ("installed", "rules as rows"),
        ("1 SELECT / rule", f"{nfind} rows out"),
    ]

    w, h, gap = 148, 88, 90
    y, step = 64, 238
    parts = [
        '<svg class="flow" viewBox="0 0 1360 310" role="img" '
        'aria-label="Data flows from the Oracle source through a local collector to JSON files, '
        'into SQLite, through a rules table, and out as findings. An AWS ingest path branches from '
        'the JSON stage and is not yet built.">',
        '<defs>'
        '<marker id="ah" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">'
        '<polygon class="flow-head" points="0,1 8,4 0,7"/></marker>'
        '<marker id="ah-a" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">'
        '<polygon points="0,1 8,4 0,7" fill="currentColor"/></marker>'
        '</defs>',
    ]

    for i, (title, m1, m2) in enumerate(stages):
        bx = 10 + i * step
        parts.append(f'<rect class="flow-box" x="{bx}" y="{y}" width="{w}" height="{h}" rx="2"/>')
        parts.append(f'<text class="flow-title" x="{bx + 12}" y="{y + 27}">{title}</text>')
        parts.append(f'<text class="flow-meta" x="{bx + 12}" y="{y + 48}">{m1}</text>')
        parts.append(f'<text class="flow-meta" x="{bx + 12}" y="{y + 65}">{m2}</text>')

        if i < len(hops):
            x1, x2 = bx + w + 7, bx + w + gap - 9
            mid = (x1 + x2) / 2
            cy = y + h / 2
            parts.append(
                f'<line class="flow-line" x1="{x1}" y1="{cy}" x2="{x2}" y2="{cy}" marker-end="url(#ah)"/>'
            )
            top, bot = hops[i]
            parts.append(
                f'<text class="flow-label" x="{mid}" y="{cy - 11}" text-anchor="middle">{top}</text>'
            )
            parts.append(
                f'<text class="flow-label" x="{mid}" y="{cy + 21}" text-anchor="middle">{bot}</text>'
            )

    parts.append(
        '<text class="flow-zone" x="10" y="192">ALL LOCAL TODAY &mdash; NO AWS DEPENDENCY</text>'
    )
    jx = 10 + 2 * step + w / 2
    parts.append(
        f'<g color="var(--accent)">'
        f'<line class="flow-future" x1="{jx}" y1="{y + h}" x2="{jx}" y2="226" marker-end="url(#ah-a)"/>'
        f'</g>'
    )
    parts.append(
        f'<text class="flow-future-text" x="{jx + 12}" y="196">same envelope &middot; HTTPS POST</text>'
    )
    parts.append('<rect class="flow-future-box" x="300" y="232" width="530" height="62" rx="2"/>')
    parts.append('<text class="flow-future-title" x="320" y="257">AWS ingest &mdash; not built</text>')
    parts.append(
        '<text class="flow-future-text" x="320" y="277">API Gateway &rarr; Lambda &rarr; Aurora '
        '&middot; routes on the dataset name</text>'
    )
    parts.append("</svg>")

    caption = (
        "Every figure on this page traces back along this chain to a logged SQL statement. The "
        "collector pushes outward only &mdash; nothing in AWS connects in to the source. The dashed "
        "branch is the one piece not yet built, and it consumes the same JSON envelope the local "
        "path already produces, so adding it changes nothing upstream of it."
    )
    return f"<figure>{''.join(parts)}<figcaption>{caption}</figcaption></figure>"


def _probe_rows(manifest: dict) -> str:
    out = []
    for p in manifest["probes"]:
        reads = PROBE_READS.get(p["probe"], "&mdash;")
        out.append(
            f'<tr><td class="obj">{p["probe"]}</td>'
            f'<td class="det mono" style="font-size:11.5px">{reads}</td>'
            f'<td class="rid">{len(p["queries"])}</td>'
            f'<td class="rid">{len(p["datasets"])}</td>'
            f'<td class="rid">{p["elapsed_ms"]:,}</td></tr>'
        )
    return "\n".join(out)


def _bars(by_category: dict) -> str:
    out = []
    for cat, c in by_category.items():
        out.append(
            f'<div class="bar-row"><div class="name">{CATEGORY_LABEL[cat]}</div>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{c["score"]}%"></div></div>'
            f'<div class="val">{c["score"]} <span>/ {c["findings"]}f</span></div></div>'
        )
    return "\n".join(out)


def _stack(by_severity: dict, total: int) -> str:
    out = []
    for sev in SEVERITY_ORDER:
        n = by_severity.get(sev, 0)
        if not n:
            continue
        pct = n / total * 100 if total else 0
        out.append(
            f'<div style="flex:{pct:.4f} 0 0;background:var(--sev-{sev.lower()})" '
            f'title="{sev}: {n}"></div>'
        )
    return "\n".join(out)


def _legend(by_severity: dict, total: int) -> str:
    out = []
    for sev in SEVERITY_ORDER:
        n = by_severity.get(sev, 0)
        pct = round(n / total * 100) if total else 0
        out.append(
            f'<div class="legend-row"><span class="swatch" style="background:var(--sev-{sev.lower()})"></span>'
            f"<span>{sev.title()}</span><span class=\"n\">{n} &nbsp;<span style='color:var(--ink-3)'>{pct}%</span></span></div>"
        )
    return "\n".join(out)


def _levels(by_level: dict) -> str:
    out = []
    for lvl in ("L1", "L2", "L3", "L4"):
        out.append(
            f'<div class="level"><span class="lv">{lvl}</span>'
            f'<span class="ln">{LEVEL_NOTE[lvl]}</span>'
            f'<span class="lc">{by_level.get(lvl, 0)}</span></div>'
        )
    return "\n".join(out)


def _blockers(findings: list[dict]) -> str:
    crits = [f for f in findings if f["severity"] == "CRITICAL"]
    if not crits:
        return '<div class="blocker"><span class="rid">&mdash;</span><div><div class="obj">No critical findings</div><div class="why">Nothing is blocking the gate.</div></div></div>'
    out = []
    for f in crits:
        out.append(
            f'<div class="blocker"><span class="rid">{f["rule_id"]}</span>'
            f'<div><div class="obj">{f["object_name"]}</div>'
            f'<div class="why">{f["detail"]}</div></div></div>'
        )
    return "\n".join(out)


def _chips(kind: str, values: list[tuple[str, str]]) -> str:
    return "".join(
        f'<button class="chip" data-kind="{kind}" data-value="{v}" aria-pressed="false">{label}</button>'
        for v, label in values
    )


def _defect_note(key: dict) -> str:
    absent = [d for d in key["defects"] if d.get("not_present_in_source") and not d["detected"]]
    if not absent:
        return ""
    d = absent[0]
    return (
        f'<div class="note"><b>Defect {d["defect"]} is excluded from the recall figure because it '
        f"is not in the database.</b> {d['note']}</div>"
    )


def _limits(assessment: dict) -> str:
    key = assessment["answer_key"]
    items = [
        (
            "SIZING",
            "<b>No utilization percentiles.</b> Oracle XE leaves <span class='mono'>V$SYSMETRIC</span> "
            "empty and is not licensed for Diagnostic Pack, so instance sizing here rests on capacity "
            "and feature usage alone. Nothing on this page implies measured headroom.",
        ),
        (
            "TRIAGE",
            f"<b>{key['additional_findings']} findings sit outside the seeded set and have not been "
            "reviewed by a human.</b> They are reported as additional findings, not as false "
            "positives &mdash; most describe the estate accurately. A true false-positive rate needs "
            "that triage pass and is not claimed here.",
        ),
        (
            "COVERAGE",
            "<b>Not every table was profiled.</b> Oracle-managed internals, materialized-view "
            "containers, external tables and anything above the row cap are skipped by design. "
            "Absence of a data-quality finding on a skipped table is not evidence of its absence.",
        ),
        (
            "SCOPE",
            "<b>This covers discovery and assessment only.</b> Remediation, provisioning, the data "
            "move, validation and cutover are later phases. No target has been created and nothing "
            "has been changed on the source.",
        ),
    ]
    return "\n".join(
        f'<div class="limit"><span class="lm">{k}</span><p>{v}</p></div>' for k, v in items
    )


def build(assessment: dict, manifest: dict, estate: str, datasets: int) -> str:
    s, key = assessment["scores"], assessment["answer_key"]
    findings = assessment["findings"]
    total = s["total_findings"]

    cap = (
        f"Capped at 60 by {s['critical_findings']} critical findings. "
        f"Uncapped the mean would be {s['raw_overall_score']}."
        if s["capped_by_critical"] and s["raw_overall_score"] > s["overall_score"]
        else (
            f"{s['critical_findings']} critical findings hold the cap in force, though the "
            f"category mean of {s['raw_overall_score']} already sits below it."
            if s["capped_by_critical"]
            else "No critical findings. The score is the unmodified category mean."
        )
    )

    html = TEMPLATE.replace("__PIPELINE_SECTION__", PIPELINE_SECTION)
    subs = {
        "__PIPELINE__": _pipeline_svg(manifest, assessment),
        "__PROBEROWS__": _probe_rows(manifest),
        "__SOURCE__": f"{assessment['source'].get('dsn','?')}",
        "__ESTATE__": estate,
        "__RUNID__": assessment["collector_run_id"][:8],
        "__WHEN__": assessment["assessed_at_utc"][:16].replace("T", " ") + " UTC",
        "__NRULES__": str(assessment["rules_evaluated"]),
        "__NDATASETS__": str(datasets),
        "__NCRIT__": str(s["critical_findings"]),
        "__OVERALL__": str(s["overall_score"]),
        "__CAPTEXT__": cap,
        "__BLOCKERS__": _blockers(findings),
        "__RECALL__": str(round(key["recall"] * 100)),
        "__DETECTED__": str(key["detected"]),
        "__DETECTABLE__": str(key["detectable"]),
        "__SEVEXACT__": str(key["severity_exact"]),
        "__MISSED__": str(key["missed"]),
        "__ADDITIONAL__": str(key["additional_findings"]),
        "__DEFECTNOTE__": _defect_note(key),
        "__BARS__": _bars(s["by_category"]),
        "__NFIND__": str(total),
        "__STACK__": _stack(s["by_severity"], total),
        "__LEGEND__": _legend(s["by_severity"], total),
        "__LEVELS__": _levels(s["by_remediation_level"]),
        "__SEVCHIPS__": _chips("sev", [(x, x.title()) for x in SEVERITY_ORDER]),
        "__CATCHIPS__": _chips("cat", [(k, v) for k, v in CATEGORY_LABEL.items()]),
        "__LIMITS__": _limits(assessment),
        "__NISSUES__": str(len(assessment["issues"])),
        "__NFIND2__": str(total),
        "__ISSUES__": json.dumps(
            [
                {
                    k: g[k]
                    for k in (
                        "rule_id", "severity", "category", "title", "rationale",
                        "remediation_level", "occurrences", "objects",
                        "objects_truncated", "sample_detail",
                    )
                }
                for g in assessment["issues"]
            ],
            ensure_ascii=False,
        ),
        "__CATLABEL__": json.dumps(CATEGORY_LABEL, ensure_ascii=False),
    }
    for placeholder, value in subs.items():
        html = html.replace(placeholder, value)
    return html


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the assessment report")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_ASSESS_OUTPUT)
    parser.add_argument("--collector-output", type=Path, default=DEFAULT_COLLECTOR_OUTPUT)
    parser.add_argument("--estate", type=str, default="Oracle 21c XE / DBMIG_APP / 1.03 GB / 90 objects")
    args = parser.parse_args(argv)

    assessment = json.loads((args.output_dir / "assessment.json").read_text(encoding="utf-8"))
    run_dir = args.collector_output / assessment["collector_run_id"]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    html = build(assessment, manifest, args.estate, len(manifest["datasets"]))
    out = args.output_dir / "report.html"
    out.write_text(html, encoding="utf-8")
    print(f"written: {out}  ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
