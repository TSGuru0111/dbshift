"""Render the report data as one self-contained HTML page. Prints cleanly."""

from __future__ import annotations

from html import escape as _e

CSS = """
:root{--ink:#111A22;--ink-2:#4F626F;--ink-3:#5F6E78;--rule:#DAE1E7;--rule-strong:#BFCAD3;--surface:#fff;
  --surface-2:#EDF1F4;--accent:#0C6E75;--accent-soft:#DCEDEE;--ok:#1F7A4C;--ok-soft:#E0F0E7;--warn:#8A6A0D;
  --warn-soft:#F6EEDA;--bad:#A31D2C;--bad-soft:#F7E3E5;--info:#3D6B8E;--info-soft:#E3ECF3}
*{box-sizing:border-box}
body{margin:0;background:#F4F6F8;color:var(--ink);font-family:"IBM Plex Sans",system-ui,-apple-system,Segoe UI,sans-serif;
  font-size:14px;line-height:1.55;-webkit-font-smoothing:antialiased}
.page{max-width:1040px;margin:0 auto;padding:40px 28px 80px}
h1{font-family:Archivo,system-ui,sans-serif;font-size:28px;letter-spacing:-.02em;margin:0 0 6px}
h2{font-family:Archivo,system-ui,sans-serif;font-size:20px;letter-spacing:-.015em;margin:44px 0 6px}
h3{font-size:14.5px;font-weight:600;margin:24px 0 8px}
p{margin:0 0 10px;max-width:78ch}
.lead{color:var(--ink-2);font-size:15px}
.meta{color:var(--ink-3);font-size:13px}
.mono{font-family:"IBM Plex Mono",ui-monospace,monospace}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--rule);border:1px solid var(--rule);
  border-radius:4px;overflow:hidden;margin:18px 0}
.tile{background:var(--surface);padding:16px 18px}
.tile .k{font-size:13px;color:var(--ink-2)}
.tile .v{font-family:Archivo,system-ui,sans-serif;font-size:28px;font-weight:700;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums;margin-top:4px;line-height:1.15}
.tile .v small{font-size:13px;font-weight:500;color:var(--ink-3);margin-left:3px}
.tile .h{font-size:12.5px;color:var(--ink-3);margin-top:4px}
table{border-collapse:collapse;width:100%;font-size:13px;background:var(--surface);border:1px solid var(--rule);
  border-radius:4px;overflow:hidden;margin:10px 0 4px}
th{text-align:left;font-size:12.5px;color:var(--ink-2);font-weight:600;padding:9px 12px;background:var(--surface-2);
  border-bottom:1px solid var(--rule-strong);white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid var(--rule);vertical-align:top}
tr:last-child td{border-bottom:none}
td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.sub{display:block;color:var(--ink-3);font-size:12.5px;margin-top:2px}
th.w,td.w{min-width:230px}
.pill{display:inline-block;font-size:12px;font-weight:600;padding:2px 9px;border-radius:12px;white-space:nowrap}
.pill.pass{background:var(--ok-soft);color:var(--ok)}
.pill.warning{background:var(--warn-soft);color:var(--warn)}
.pill.fail{background:var(--bad-soft);color:var(--bad)}
.pill.info{background:var(--info-soft);color:var(--info)}
.pill.simple{background:var(--ok-soft);color:var(--ok)}
.pill.medium{background:var(--warn-soft);color:var(--warn)}
.pill.complex{background:var(--bad-soft);color:var(--bad)}
.pill.decision{background:var(--surface-2);color:var(--ink-2)}
.note{padding:12px 16px;border-radius:4px;font-size:13.5px;margin:12px 0;background:var(--accent-soft);color:#0A5A60}
.note.warn{background:var(--warn-soft);color:var(--warn)}
.note.bad{background:var(--bad-soft);color:var(--bad)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.card{background:var(--surface);border:1px solid var(--rule);border-radius:4px;padding:18px 20px}
.card h3{margin-top:0}
.objs{color:var(--ink-3);font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px}
.foot{margin-top:48px;padding-top:16px;border-top:1px solid var(--rule);color:var(--ink-3);font-size:12.5px}
@media(max-width:760px){.tiles,.grid2{grid-template-columns:1fr 1fr}}
@media print{body{background:#fff}.page{padding:0;max-width:none}h2{break-after:avoid}table{break-inside:auto}tr{break-inside:avoid}}
"""


def _pill(kind: str, text: str | None = None) -> str:
    return f'<span class="pill {kind}">{_e(text or kind)}</span>'


def _objs(items, more=0) -> str:
    if not items:
        return ""
    s = ", ".join(_e(str(x)) for x in items)
    if more:
        s += f" +{more} more"
    return f'<span class="objs">{s}</span>'


def render(r: dict) -> str:
    sc, dms, st, prov = r["schema_conversion"], r["dms_assessment"], r["status"], r["provenance"]
    cs = sc["code_summary"]
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Migration assessment — {_e(r['estate'] or '')}</title><style>{CSS}</style></head><body><div class='page'>"]

    parts.append(f"""
<h1>Migration assessment report</h1>
<p class="lead">{_e(r['estate'] or 'estate')} &rarr; Amazon RDS for Oracle, with the stored code also assessed for PostgreSQL.</p>
<p class="meta">Collector run <span class="mono">{_e(r['collector_run_id'][:8])}</span> · assessed {_e((prov.get('assessed_at_utc') or '')[:19].replace('T',' '))} UTC ·
report generated {_e(r['generated_at_utc'][:19].replace('T',' '))} UTC · {prov.get('rules_evaluated') or 0} assessment rules</p>
<div class="note">This report follows the structure of the two documents an AWS migration usually produces &mdash; the
<b>SCT assessment report</b> (what converts automatically, and the action items by complexity) and the
<b>DMS pre-migration assessment</b> (whether the tables can replicate). Every figure below is read from DBShift's
own records; it is not produced by those tools and does not claim to be.</div>
""")

    # ---- at a glance
    scores = r.get("scores") or {}
    gate = (st.get("gate") or {})
    parts.append('<div class="tiles">')
    parts.append(_tile("Assessment score", f"{scores.get('overall_score', '—')}<small>/100</small>",
                       "capped by a critical finding" if scores.get("capped_by_critical") else "five categories, averaged"))
    parts.append(_tile("Critical findings", str(scores.get("critical_findings", "—")),
                       f"gate says {gate.get('verdict') or 'not evaluated'}"))
    if cs.get("ran"):
        parts.append(_tile("Stored code converted", f"{cs['pct_automatic']}<small>%</small>",
                           f"{cs['automatic']} of {cs['convertible']} by rule, compiled on PostgreSQL"))
    else:
        parts.append(_tile("Stored code converted", "—", "Phase 4b has not run for this estate"))
    bc = sc["by_complexity"]
    parts.append(_tile("Action items", str(sum(bc.values())),
                       f"{bc['simple']} simple · {bc['medium']} medium · {bc['complex']} complex · {bc['decision']} decisions"))
    parts.append("</div>")

    # ---- target decision
    td = r.get("target_decision")
    if td:
        forced = ", ".join(td["forced_by"]) if td.get("forced_by") else "nothing"
        parts.append(f"""
<h2>Target decision</h2>
<p>The rules engine's verdict, after checking the proposal. Licence counts are computed; licence cost is not quoted.</p>
<div class="tiles">
{_tile("Edition", f"{_e(td.get('edition') or '—')}<small>{_e(td.get('licence_model') or '')}</small>", f"forced by {_e(forced)}")}
{_tile("Instance", _e(td.get('instance_class') or '—'), "capacity floor unless a utilization feed was supplied")}
{_tile("Storage", f"{td.get('storage_gb') or '—'}<small>GB</small>", "gp3")}
{_tile("Processor licences", str(td.get('processor_licences') or '—'), "BYOL, held by the client")}
</div>""")

    # ---- schema conversion (SCT-style)
    parts.append("<h2>Schema conversion assessment</h2>")
    parts.append("<p>What converts automatically, what needs judgement, and what needs a person. Complexity is the "
                 "remediation level the assessment rule assigned, never re-judged for the report.</p>")
    parts.append("<h3>Database code objects</h3>")
    if cs.get("ran"):
        parts.append(f"<p>Converted by Phase 4b. <b>Converted automatically</b> means a deterministic rule produced the "
                     f"PL/pgSQL, it passed the static, policy and parity gates, and it was created and rolled back on "
                     f"{_e(prov.get('compile_target') or 'PostgreSQL')}. Model mode was <b>{_e(prov.get('model_mode') or 'off')}</b>; "
                     f"{prov.get('static_outputs_used', 0)} stand-in(s) used, model generation "
                     f"{'enabled' if prov.get('model_generation_enabled') else 'not enabled'}.</p>")
        parts.append("<table><thead><tr><th>Object type</th><th class='n'>Total</th><th class='n'>Converted automatically</th>"
                     "<th class='n'>Converted with the model tier</th><th class='n'>Needs the model tier</th>"
                     "<th class='n'>Needs a person</th><th class='n'>Broken on the source</th><th class='n'>Folded into body</th></tr></thead><tbody>")
        for row in sc["code_objects"]:
            parts.append(f"<tr><td>{_e(row['type'].title())}</td><td class='n'>{row['total']}</td><td class='n'>{row['automatic']}</td>"
                         f"<td class='n'>{row['model_converted']}</td><td class='n'>{row['needs_model']}</td><td class='n'>{row['manual']}</td>"
                         f"<td class='n'>{row['excluded']}</td><td class='n'>{row['absorbed']}</td></tr>")
        parts.append(f"<tr><td><b>All</b></td><td class='n'><b>{cs['total']}</b></td><td class='n'><b>{cs['automatic']}</b></td>"
                     f"<td class='n'><b>{cs['model_converted']}</b></td><td class='n'><b>{cs['needs_model']}</b></td><td class='n'><b>{cs['manual']}</b></td>"
                     f"<td class='n'><b>{cs['excluded']}</b></td><td class='n'><b>{cs['absorbed']}</b></td></tr></tbody></table>")
        if cs.get("uncompiled") or cs.get("rejected"):
            parts.append(f"<p class='meta'>{cs.get('uncompiled', 0)} converted but not yet compiled (no PostgreSQL target at the time); "
                         f"{cs.get('rejected', 0)} refused by a gate.</p>")
    else:
        parts.append("<div class='note warn'>Phase 4b (Convert PL/SQL) has not run for this estate, so no conversion figures are claimed.</div>")

    parts.append("<h3>Database storage objects</h3>")
    parts.append(f"<p>{_e(sc['storage_note'])}</p>")
    parts.append("<table><thead><tr><th>Object type</th><th class='n'>Count</th><th class='n'>Action items</th><th>Rules that fired</th></tr></thead><tbody>")
    for row in sc["storage_objects"]:
        parts.append(f"<tr><td>{_e(row['type'].title())}</td><td class='n'>{row['count']}</td><td class='n'>{row['action_items']}</td>"
                     f"<td class='mono'>{_e(', '.join(row['rules']) or '—')}</td></tr>")
    parts.append("</tbody></table>")
    if sc.get("partitions"):
        parts.append(f"<p class='meta'>{sc['partitions']} table partitions across the partitioned tables; partitioning is what forces Enterprise Edition on RDS.</p>")

    parts.append("<h3>Action items</h3>")
    parts.append("<table><thead><tr><th>Complexity</th><th>Item</th><th class='n'>Objects</th><th>Where</th></tr></thead><tbody>")
    for it in sc["action_items"]:
        src = "conversion" if it["source"] == "conversion" else _e(it["rule_id"])
        rec = f"<span class='sub'>{_e(it['recommendation'])}</span>" if it.get("recommendation") else ""
        parts.append(f"<tr><td>{_pill(it['complexity'])}</td><td>{_e(it['title'])} <span class='mono meta'>{src}</span>{rec}</td>"
                     f"<td class='n'>{it['occurrences']}</td><td>{_objs(it['objects'], it['more'])}</td></tr>")
    parts.append("</tbody></table>")

    # ---- DMS pre-migration assessment
    path = dms["path"]
    parts.append("<h2>DMS pre-migration assessment</h2>")
    parts.append("<p>The checks DMS runs before a task, answered from the assessment rules that decide them, with what "
                 "each failure blocks according to the blocker gate.</p>")
    parts.append("<table><thead><tr><th class='w'>Check</th><th>Result</th><th class='n'>Count</th><th>Evidence</th><th>Blocks</th></tr></thead><tbody>")
    for c in dms["checks"]:
        ev = _e(c["detail"] or "no finding") + (("<br>" + _objs(c["objects"])) if c["objects"] else "")
        parts.append(f"<tr><td class='w'>{_e(c['name'])}<span class='sub'>{_e(c['why'])}</span></td><td>{_pill(c['result'])}</td>"
                     f"<td class='n'>{c['count'] or ''}</td><td>{ev}</td><td class='mono'>{_e(', '.join(c['blocks']) or '—')}</td></tr>")
    parts.append("</tbody></table>")
    kind = "bad" if path["cdc"] == "blocked" else ""
    parts.append(f"<div class='note {kind}'><b>Replication path.</b> Full load: {_e(path['full_load'])}"
                 f"{(' (' + ', '.join(path['full_load_blocked_by']) + ')') if path['full_load_blocked_by'] else ''}. "
                 f"Change data capture: {_e(path['cdc'])}{(' (' + ', '.join(path['cdc_blocked_by']) + ')') if path['cdc_blocked_by'] else ''}. "
                 f"{_e(path['consequence'])}</div>")
    if path.get("used"):
        parts.append(f"<p>{_e(path['used'])}</p>")

    # ---- status
    parts.append("<h2>Where the migration stands</h2><div class='grid2'>")
    if st.get("gate"):
        parts.append(f"<div class='card'><h3>Blocker gate</h3>{_pill('fail' if st['gate']['verdict']=='HALT' else 'pass', st['gate']['verdict'])}"
                     f"<p style='margin-top:10px'>{_e(st['gate']['summary'] or '')}</p></div>")
    if st.get("provision"):
        p = st["provision"]
        parts.append(f"<div class='card'><h3>Target</h3><p>{_e(p.get('stack') or '')} · {_e((p.get('engine') or '').replace('oracle-','').upper())} "
                     f"{_e(p.get('instance') or '')}</p><p class='meta'>rendered from the records; deploying is a separate, acknowledged act</p></div>")
    if st.get("validation"):
        v = st["validation"]
        parts.append(f"<div class='card'><h3>Validation</h3>{_pill('pass' if v['status']=='validated' else 'warning', v['status'] or 'not run')}"
                     f"<p style='margin-top:10px'>{v.get('mismatches', 0)} mismatches · {v.get('not_comparable', 0)} not comparable"
                     f"<span class='sub'>against run {_e((v.get('run_id') or '')[:8])}</span></p></div>")
    if st.get("cutover"):
        c = st["cutover"]
        unmet = "".join(f"<li>{_e(u)}</li>" for u in c["unmet"])
        parts.append(f"<div class='card'><h3>Cutover certificate</h3>{_pill('pass' if c['ready'] else 'fail', 'ready' if c['ready'] else 'not ready')}"
                     f"<p style='margin-top:10px' class='meta'>built against run {_e((c.get('run_id') or '')[:8])}</p>"
                     f"{'<ul style=\"margin:6px 0 0 18px;padding:0\">' + unmet + '</ul>' if unmet else ''}</div>")
    parts.append("</div>")
    if not st.get("same_run"):
        parts.append("<div class='note warn'>The validation report or the certificate describes a different collector run than this "
                     "assessment. They are shown for status only; the certificate itself refuses on that mismatch.</div>")

    # ---- recall, if applicable
    rc = r.get("recall") or {}
    if rc.get("applicable"):
        parts.append(f"<h2>Measured recall</h2><p>This estate carries seeded defects with a known answer key. The assessment found "
                     f"<b>{rc['detected']} of {rc['detectable']}</b> detectable defects, severity exact on {rc['severity_exact']}"
                     f"{'; ' + str(rc['not_present_in_source']) + ' seeded defect is not present in the source and is excluded from the denominator' if rc.get('not_present_in_source') else ''}.</p>")

    # ---- provenance
    recs = ", ".join(f"{k} {_e((v or '')[:8])}" for k, v in prov["records"].items())
    parts.append(f"<div class='foot'>Records: {recs} · {'all from one collector run' if prov['consistent'] else '<b>records disagree on the run</b>'}"
                 f"{' · conversion rules ' + _e(prov['conversion_rule_version']) if prov.get('conversion_rule_version') else ''}"
                 f" · Generated by DBShift Phase 10 from records on disk. Nothing in this report was written by a model.</div>")
    parts.append("</div></body></html>")
    return "\n".join(parts)


def _tile(k: str, v: str, h: str) -> str:
    return f'<div class="tile"><div class="k">{_e(k)}</div><div class="v">{v}</div><div class="h">{_e(h)}</div></div>'
