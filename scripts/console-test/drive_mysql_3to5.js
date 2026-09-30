// Phases 3, 4, 4b, 4c, 4d and 5 on a MySQL source, for BOTH MySQL pairs.
//
// These screens were written against Oracle, and a MySQL run showed Oracle's
// words verbatim: "Fix it on Oracle first" beside a MySQL routine, a rail that
// read "PostgreSQL" after sizing RDS for MySQL, "oracle rehearsal: not
// configured" in the Phase 4 stream, and a 4b caption crediting "the
// deterministic rules" for seven routines Bedrock had drafted. This walks every
// one of those screens at desktop and phone width and fails on:
//
//   * Oracle-only wording in the visible text or a hint's tooltip
//   * 4b/4c/4d looking runnable on MySQL -> RDS for MySQL, or looking disabled
//     on MySQL -> RDS for PostgreSQL
//   * a JS error, or an /api/ call failing (409 is "no record yet", and
//     /api/cutover's 400 on MySQL is Phase 9's and known -- both excluded)
//   * the page scrolling sideways
//
// Needs the console up with a CONNECTED MySQL session that has run discovery
// (scratchpad/setup_mysql.py or console_pipeline.py). It sets the Phase 3
// target itself, and reuses SCT's cached assessment for each target. The
// MySQL -> RDS for PostgreSQL remediation is re-drafted (Bedrock) only if the
// plan on the server is for another target. Nothing here creates an AWS
// resource.
//
//   node drive_mysql_3to5.js                 # both pairs
//   PAIRS=MYSQL node drive_mysql_3to5.js     # one pair
//
// Ends with an in-page Oracle spot check: the same renderers with SRC flipped
// to ORACLE must produce the Oracle wording unchanged. It does not touch the
// server's source engine, so the MySQL session survives the run.
const { chromium } = require('playwright-core');
const path = require('path');
const fs = require('fs');

const URL = process.env.DBSHIFT_URL || 'http://127.0.0.1:8765';
const PAIRS = (process.env.PAIRS || 'POSTGRESQL,MYSQL').split(',').map(s => s.trim().toUpperCase());
const VIEWPORTS = [[1440, 900], [390, 844]];
const OUT = path.join(__dirname, 'shots-mysql-3to5');
const SCT_ID = {POSTGRESQL: 'rds-postgresql', MYSQL: 'rds-mysql'};

let pass = 0, fail = 0;
const ok = (n, c, d = '') => { c ? pass++ : fail++; console.log(`  [${c ? 'ok' : 'FAIL'}] ${n}${c ? '' : ' -- ' + d}`); };

// Oracle-only vocabulary. "PL/pgSQL" is PostgreSQL's and does not match.
const ORACLE_WORDS = /PL\/SQL|\bOracle\b|ARCHIVELOG|Data Pump|v\$database|\bDBA_|\bredo\b|ROWNUM|\b5200\b|\b5659\b|Enterprise Edition|Standard Edition|NOVALIDATE/;

async function get(p, url) {
  const r = await p.request.get(URL + url, { timeout: 900000 });
  return { status: r.status(), text: await r.text() };
}

// Visible text plus every tooltip a reader can hover, for one view. Closed
// <details> are opened first -- their contents are one click away and were
// where most of the Oracle wording lived.
async function viewText(p, id) {
  return p.evaluate(v => {
    const el = document.getElementById('view-' + v);
    el.querySelectorAll('details').forEach(d => { d.open = true; });
    const titles = [...el.querySelectorAll('[title]')]
      .filter(x => x.offsetParent !== null).map(x => x.title);
    // The topbar belongs to the view on screen: its Next button named the next
    // stage "Convert PL/SQL" on a MySQL run after the rail had stopped doing so.
    const bar = document.getElementById('pagenav');
    const barTitles = [...bar.querySelectorAll('[title]')].map(x => x.title);
    return [el.innerText, ...titles, bar.innerText, ...barTitles,
            document.getElementById('crumbTitle').textContent].join('\n');
  }, id);
}

// Deliberate contrasts, not leaks. The MySQL construct catalogue and SCT route
// explain SELECT ... INTO by saying it must NOT get "the Oracle rule" (STRICT) --
// naming Oracle there is the point, because the 4b model was told the opposite
// for Oracle code. Kept narrow so a real leak on the same line still fails.
const CONTRAST = /the Oracle rule/;

function oracleHits(text) {
  return text.split('\n').filter(l => ORACLE_WORDS.test(l.replace(CONTRAST, '')))
    .map(l => l.trim().slice(0, 140));
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const b = await chromium.launch({ channel: 'msedge', headless: true });

  // ---- preconditions, from the server ------------------------------------
  const probe = await b.newPage();
  const st = JSON.parse((await get(probe, '/api/state')).text);
  console.log('preconditions');
  ok('the session is a MySQL source', st.source_engine === 'MYSQL', st.source_engine);
  ok('the source is connected', !!st.connected);
  ok('discovery has run', !!st.has_discovery);
  if (st.source_engine !== 'MYSQL' || !st.has_discovery) {
    console.log('\nPopulate a MySQL session first (scratchpad setup_mysql.py).');
    await b.close(); process.exit(1);
  }
  await probe.close();

  for (const pair of PAIRS) {
    const sct = SCT_ID[pair];
    console.log(`\n==== MySQL -> ${pair} (${sct}) ====`);
    const setup = await b.newPage();
    let r = await setup.request.post(URL + '/api/engine', { data: { engine: pair } });
    ok(`Phase 3 accepts ${pair} from a MySQL source`, r.ok(), await r.text());
    let a = await get(setup, `/api/sct/assess?target=${sct}`);
    ok(`SCT assessment for ${sct} is available`, a.status === 200 && /"event":\s*"(end|complete|done)"/.test(a.text),
       a.text.slice(-200));
    const plan = await get(setup, '/api/sct/remediation');
    const planTarget = plan.status === 200 ? JSON.parse(plan.text).target : null;
    if (planTarget !== sct) {
      console.log(`  (re-drafting Phase 4 for ${sct}; plan on the server was for ${planTarget})`);
      a = await get(setup, `/api/sct/remediate?target=${sct}`);
      ok(`Phase 4 remediation for ${sct} completes`, /"event":\s*"complete"/.test(a.text), a.text.slice(-300));
    }
    a = await get(setup, `/api/sct/gate?target=${sct}`);
    ok(`the gate evaluates for ${sct}`, a.status === 200, a.text.slice(0, 200));
    await setup.close();

    for (const [W, H] of VIEWPORTS) {
      const tag = `${pair}@${W}`;
      console.log(`\n-- ${tag}`);
      const p = await b.newPage({ viewport: { width: W, height: H } });
      const errs = [], bad = [];
      p.on('pageerror', e => errs.push(String(e)));
      p.on('console', m => {
        if (m.type() !== 'error') return;
        const t = m.text();
        if (/Failed to load resource/.test(t)) return;   // recorded with its URL below
        errs.push(t);
      });
      p.on('response', x => {
        const u = x.url().replace(/^https?:\/\/[^/]+/, '');
        if (x.status() >= 400 && x.status() !== 409 && u.startsWith('/api/')
            && !u.startsWith('/api/cutover')) bad.push(`${x.status()} ${u}`);
      });
      await p.goto(URL, { waitUntil: 'networkidle' });
      await p.waitForFunction(() => stageState.remediate !== 'idle' && SRC === 'MYSQL', null, { timeout: 30000 });
      // The SCT gate restore is the last await in loadState.
      await p.waitForFunction(() => !!SCTGATE, null, { timeout: 30000 }).catch(() => {});

      ok(`${tag}: reload restores the SCT target for this pair, so 4 and 5 act on it`,
         await p.$eval('#sctTarget', e => e.value) === sct, await p.$eval('#sctTarget', e => e.value));
      ok(`${tag}: the 4b rail label is not PL/SQL`,
         await p.$eval('.stage[data-stage="convert"] .label', e => e.textContent) === 'Convert routines',
         await p.$eval('.stage[data-stage="convert"] .label', e => e.textContent));

      // ---- Phase 3 --------------------------------------------------------
      await p.evaluate(() => show('target'));
      if (W === VIEWPORTS[0][0]) {
        await p.click('#btnSize');
        await p.waitForFunction(() => stageState.target === 'done' || /failed|lost/.test(stageSub.target || ''),
                                null, { timeout: 300000 });
      } else {
        await p.waitForFunction(() => $('#sizeResult').style.display !== 'none', null, { timeout: 30000 })
          .catch(() => {});
      }
      const spec = await p.$eval('#targetSpec', e => e.innerText);
      const want = pair === 'MYSQL' ? 'RDS for MySQL' : 'RDS for PostgreSQL';
      ok(`${tag}: Phase 3 names ${want}`, spec.includes(want), spec.slice(0, 120));
      ok(`${tag}: no edition or Oracle licence on an open-source target`,
         !/Edition|Oracle licences/.test(spec) && await p.$eval('#editionPanels', e => e.style.display) === 'none');
      if (W === VIEWPORTS[0][0]) {
        const sub = await p.evaluate(() => stageSub.target);
        ok(`${tag}: the rail sub names the chosen engine`, pair === 'MYSQL' ? /MySQL/.test(sub) && !/PostgreSQL/.test(sub)
                                                                              : /PostgreSQL/.test(sub), sub);
      }

      // ---- Phase 4 --------------------------------------------------------
      await p.evaluate(() => show('remediate'));
      if (pair === 'MYSQL' && W === VIEWPORTS[0][0]) {
        // Cheap on this pair (zero items), and it is the only way to see the
        // server's stream text in the browser.
        await p.click('#btnSctRemediate');
        await p.waitForFunction(() => stageState.remediate === 'done' || /failed|lost/.test(stageSub.remediate || ''),
                                null, { timeout: 120000 });
        const ticker = await p.$eval('#sctRemTicker', e => e.innerText);
        ok(`${tag}: the Phase 4 stream says there is no rehearsal copy on MySQL`,
           /no rehearsal copy/.test(ticker) && !/oracle rehearsal/i.test(ticker), ticker.slice(0, 200));
      }
      await p.waitForFunction(() => $('#sctRemResult').style.display !== 'none', null, { timeout: 30000 })
        .catch(() => {});
      const rem = await p.$eval('#sctRemGroups', e => e.innerText);
      if (pair === 'MYSQL') {
        ok(`${tag}: an empty homogeneous plan says zero is the expected result`,
           /expected result/.test(rem), rem.slice(0, 160));
      } else {
        ok(`${tag}: the plan lists SCT's items`, /Needs a decision|Needs a person/.test(rem), rem.slice(0, 160));
      }
      ok(`${tag}: no target-mismatch warning on the plan`, !/but Phase 3 chose/.test(rem), rem.slice(0, 200));
      ok(`${tag}: the rehearsal form is disabled on MySQL`,
         await p.$eval('#btnRehearsal', e => e.disabled) && await p.$eval('#rehearsalDsn', e => e.disabled));

      // ---- 4b / 4c / 4d ---------------------------------------------------
      const homog = pair === 'MYSQL';
      for (const [view, banner, btn] of [['convert', '#naConvert', '#btnConvert'],
                                         ['schemaddl', '#naSchemaDdl', '#btnSchemaDdl'],
                                         ['appsql', '#naAppSql', '#btnAppsql']]) {
        await p.evaluate(v => show(v), view);
        const shown = await p.$eval(banner, e => e.style.display !== 'none' && e.offsetParent !== null);
        const dis = await p.$eval(btn, e => e.disabled);
        if (homog) {
          ok(`${tag}: ${view} states it is not applicable`, shown
             && /Not applicable/.test(await p.$eval(banner, e => e.innerText)));
          ok(`${tag}: ${view}'s run button is disabled, not hidden`,
             dis && await p.$eval(btn, e => e.offsetParent !== null));
        } else {
          ok(`${tag}: ${view} is applicable`, !shown && !dis, `banner ${shown}, disabled ${dis}`);
        }
      }
      if (!homog && W === VIEWPORTS[0][0]) {
        // 4c and 4d are not restored on reload; run them (local PostgreSQL,
        // model tier off for 4d -- nothing bills).
        await p.evaluate(() => show('schemaddl'));
        await p.click('#btnSchemaDdl');
        await p.waitForFunction(() => stageState.schemaddl === 'done' || /failed/.test(stageSub.schemaddl || ''),
                                null, { timeout: 180000 });
        ok(`${tag}: 4c generates and compiles`, /compiled and rolled back/.test(await p.$eval('#ddlMeta', e => e.textContent)),
           await p.$eval('#ddlMeta', e => e.textContent));
        await p.evaluate(() => show('appsql'));
        ok(`${tag}: 4d defaults to the MySQL corpus`,
           await p.$eval('#appsqlRoot', e => e.value) === 'scripts/demo-app-mysql/mappers');
        await p.click('#btnAppsql');
        await p.waitForFunction(() => stageState.appsql === 'done' || /failed/.test(stageSub.appsql || ''),
                                null, { timeout: 180000 });
        ok(`${tag}: 4d converts`, / statements/.test(await p.$eval('#appsqlMeta', e => e.textContent)),
           await p.$eval('#appsqlMeta', e => e.textContent));
        await p.evaluate(() => show('convert'));
        await p.waitForFunction(() => $('#convResult').style.display !== 'none', null, { timeout: 30000 })
          .catch(() => {});
        const notes = await p.$eval('#convNotes', e => e.innerText);
        ok(`${tag}: 4b does not credit the rules with model-drafted routines`,
           !/handled by the deterministic rules/.test(notes) && /no rule tier/.test(notes), notes.slice(0, 200));
        ok(`${tag}: 4b's generation card says MySQL has no rule tier`,
           // textContent: after a run the card is folded into a closed <details>.
           /no rule tier/.test(await p.$eval('#convGenStatus', e => e.textContent)));
      }

      // ---- Phase 5 --------------------------------------------------------
      await p.evaluate(() => show('gate'));
      await p.click('#btnSctGate');
      await p.waitForFunction(() => SCTGATE && $('#sctGateResult').style.display !== 'none', null, { timeout: 60000 });
      const meta = await p.$eval('#sctGateMeta', e => e.textContent);
      ok(`${tag}: the gate names the MySQL source and ${sct}`, /source MySQL/.test(meta) && meta.includes(sct), meta);
      const verdict = await p.$eval('#sctGateVerdict', e => e.innerText);
      if (homog) ok(`${tag}: the homogeneous summary explains zero items`, /homogeneous/i.test(verdict), verdict.slice(0, 200));
      ok(`${tag}: no target-mismatch warning on the gate`, !/but Phase 3 chose/.test(verdict));
      const cdc = await p.$eval('#sctGateCdc', e => e.innerText);
      ok(`${tag}: CDC readiness speaks binlog, not redo`, /bin(ary )?log/i.test(cdc) && !/ARCHIVELOG|redo/.test(cdc),
         cdc.slice(0, 200));
      if (homog) ok(`${tag}: the empty work list says why it is empty`,
                    /No action items/.test(await p.$eval('#sctGateGroups', e => e.innerText)));

      // ---- layout, then wording (wording opens every <details>) -----------
      for (const v of ['target', 'remediate', 'convert', 'schemaddl', 'appsql', 'gate']) {
        await p.evaluate(x => show(x), v);
        await p.waitForTimeout(150);
        const over = await p.evaluate(() => {
          const m = document.querySelector('main');
          return {page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                  pane: m.scrollWidth - m.clientWidth};
        });
        ok(`${tag}: ${v} does not scroll sideways`, over.page <= 1 && over.pane <= 1, JSON.stringify(over));
        await p.screenshot({ path: path.join(OUT, `${pair.toLowerCase()}-${W}-${v}.png`) });
      }
      for (const v of ['target', 'remediate', 'convert', 'schemaddl', 'appsql', 'gate']) {
        await p.evaluate(x => show(x), v);
        const hits = oracleHits(await viewText(p, v));
        ok(`${tag}: ${v} carries no Oracle-only wording`, !hits.length, hits.slice(0, 4).join(' | '));
      }

      ok(`${tag}: no JS errors`, !errs.length, errs.slice(0, 3).join(' | '));
      ok(`${tag}: no failing /api/ calls`, !bad.length, bad.slice(0, 5).join(', '));
      await p.close();
    }
  }

  // ---- Oracle spot check: same renderers, SRC flipped in the page -----------
  console.log('\nOracle wording is unchanged (in-page, server untouched)');
  const p = await b.newPage({ viewport: { width: 1440, height: 900 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e)));
  await p.goto(URL, { waitUntil: 'networkidle' });
  await p.waitForFunction(() => SRC === 'MYSQL', null, { timeout: 30000 });
  const ora = await p.evaluate(() => {
    SRC = 'ORACLE';
    renderRail(); applySourceWording();
    const out = {
      rail: document.querySelector('.stage[data-stage="convert"] .label').textContent,
      gen: $('#convGenStatus').textContent,
      broken: convAction('EXCLUDED_BROKEN_ON_SOURCE'),
      key: sctStateKey(),
      ddlHint: $('#ddlOrderHint').title,
      proofHint: $('#appsqlProofHint').title,
      genHint: $('#convGenHint').title,
      rehearsal: !$('#btnRehearsal').disabled,
      name: srcName(),
      stage: convStage(),
    };
    SRC = 'MYSQL'; renderRail(); applySourceWording();
    return out;
  });
  ok('the 4b rail label is Convert PL/SQL', ora.rail === 'Convert PL/SQL', ora.rail);
  ok('4c/4d point at Convert PL/SQL', ora.stage === 'Convert PL/SQL', ora.stage);
  ok('the 4b card still leads with the rules', /Rules first|Rules only|Rules, then|could not be determined/.test(ora.gen), ora.gen);
  ok('a broken object is fixed on Oracle', ora.broken === 'Fix it on Oracle first', ora.broken);
  ok('blocked still names the rehearsal database', /rehearsal database/.test(ora.key));
  ok('the 4c hint still names Data Pump', /Data Pump/.test(ora.ddlHint));
  ok('the 4d hint still names ROWNUM', /ROWNUM/.test(ora.proofHint));
  ok('the 4b hint still describes the rule tier', /rewritten by a rule/.test(ora.genHint));
  ok('the rehearsal form is enabled on Oracle', ora.rehearsal);
  ok('the source name is Oracle', ora.name === 'Oracle', ora.name);
  ok('flipping the source back and forth raises no JS error', !errs.length, errs.join(' | '));
  await b.close();

  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
