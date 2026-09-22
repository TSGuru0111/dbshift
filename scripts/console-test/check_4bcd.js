// Checks the three "prepare the target" phases render correctly and consistently:
//
//   4b  Convert PL/SQL      stored code  -> PL/pgSQL
//   4c  Schema DDL          the target's table structure
//   4d  Application SQL     the SQL the application sends
//
// They are three different artefacts on the same target, so a reader moving
// between them must not be learning three layouts. What this settles:
//
//   1. **All three are in the rail, in order.** 4c had no console screen at
//      all until 2026-09-18 -- it was CLI-only, which made the phase that
//      builds the target's schema invisible in a demo.
//   2. **No column overlaps.** 4b shipped with the full status label in the
//      78px state chip, which is white-space:nowrap, so it spilled across the
//      next two columns and made every row unreadable. CSS review missed it;
//      measuring the rendered boxes catches it.
//   3. **Nothing claims to be proven when it was not.** With no PostgreSQL
//      target, 4c must say the compile did not run and 4d must say the
//      statements are unproven -- never a silent pass.
//
// Run: node check_4bcd.js        (DBSHIFT_URL, default http://127.0.0.1:8765)
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const BASE = process.env.DBSHIFT_URL || 'http://127.0.0.1:8765';
let pass = 0, fail = 0;
function log(name, ok, detail = '') {
  ok ? pass++ : fail++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  -- ' + detail : ''}`);
}

// Measures one panel's rows: overlap, clipping, wrapping, column count.
async function geometry(page, selector) {
  return page.$$eval(selector, els => {
    if (!els.length) return { rows: 0 };
    const overlap = [], clipped = [];
    let maxLines = 1;
    for (const el of els) {
      const boxes = [...el.children].map(c => c.getBoundingClientRect());
      for (let i = 0; i < boxes.length - 1; i++) {
        if (boxes[i].right > boxes[i + 1].left + 0.5) {
          overlap.push({ cell: i, text: el.children[i].textContent.trim().slice(0, 36) });
        }
      }
      for (const cell of el.children) {
        if (cell.scrollWidth > cell.clientWidth + 1 &&
            getComputedStyle(cell).textOverflow !== 'ellipsis') {
          clipped.push(cell.textContent.trim().slice(0, 30));
        }
        const lh = parseFloat(getComputedStyle(cell).lineHeight) || 20;
        maxLines = Math.max(maxLines, Math.round(cell.getBoundingClientRect().height / lh));
      }
    }
    return { rows: els.length, overlap, clipped, maxLines,
             cols: getComputedStyle(els[0]).gridTemplateColumns.split(' ').length };
  });
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge' });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  page.on('pageerror', e => log('no page error', false, e.message));
  await page.goto(BASE, { waitUntil: 'networkidle' });

  // ---------------------------------------------------------------- the rail
  const rail = await page.$$eval('#rail .stage', els => els.map(e => ({
    label: e.querySelector('.label').textContent.trim(),
    n: e.querySelector('.dot').textContent.trim(),
  })));
  const labels = rail.map(r => r.label);
  for (const [n, label] of [['4b', 'Convert PL/SQL'], ['4c', 'Schema DDL'],
                            ['4d', 'Application SQL']]) {
    log(`the rail carries ${n} ${label}`,
        rail.some(r => r.n === n && r.label === label), labels.join(' | '));
  }
  const i4b = labels.indexOf('Convert PL/SQL');
  const i4c = labels.indexOf('Schema DDL');
  const i4d = labels.indexOf('Application SQL');
  log('they are in order 4b -> 4c -> 4d', i4b < i4c && i4c < i4d,
      `${i4b} ${i4c} ${i4d}`);

  // ------------------------------------------------------------ 4b: the table
  // 4b needs a discovery run to produce a plan; render a stored one so the
  // table is testable without a live Oracle.
  const plan4b = JSON.parse(fs.readFileSync(
    path.join(__dirname, '..', '..', 'convert', 'output', 'conversion_plan.json'), 'utf8'));
  const ok4b = await page.evaluate(p => {
    try {
      const by = {};
      for (const e of p.entries) (by[e.status] ||= []).push(e);
      const head = `<div class="sctrow" style="padding:4px 14px"><span>state</span>
        <span>type</span><span>object</span><span class="obj">where it came from</span>
        <span class="act">what to do</span></div>`;
      document.querySelector('#convList').innerHTML =
        CONV_ORDER.filter(s => by[s]).map(s => `<div class="mt-s">
          <div class="eyebrow">${CONV_LABEL[s] || s} <span> — ${by[s].length}</span></div>
          ${head}<div class="acc">${by[s].map(e =>
            convRow(e, CONV_TONE[s] || 'INFO')).join('')}</div></div>`).join('');
      document.querySelector('#convResult').style.display = '';
      stageState.convert = 'active'; show('convert');
      return true;
    } catch (e) { return String(e); }
  }, plan4b);
  log('4b renders its table without error', ok4b === true, String(ok4b));
  await page.waitForTimeout(200);

  const g4b = await geometry(page, '#convList summary.sctrow');
  log('4b has a row per object', g4b.rows > 20, String(g4b.rows));
  log('4b: no column overlaps', (g4b.overlap || []).length === 0,
      JSON.stringify((g4b.overlap || []).slice(0, 2)));
  log('4b: no cell text is clipped', (g4b.clipped || []).length === 0,
      JSON.stringify((g4b.clipped || []).slice(0, 2)));
  log('4b: nothing wraps to a second line', g4b.maxLines === 1, `max ${g4b.maxLines}`);
  log('4b: five columns', g4b.cols === 5, String(g4b.cols));

  // ------------------------------------------------------------ 4c: the table
  const plan4c = JSON.parse(fs.readFileSync(
    path.join(__dirname, '..', '..', 'convert', 'output', 'schema_ddl.json'), 'utf8'));
  // Force the unproven case: the compile must read as not-run, not as a pass.
  plan4c.compile = { ran: 0, failed: 0, ok: null, rolled_back: null, failures: [],
                     not_run_because: 'no PostgreSQL target is registered' };
  const ok4c = await page.evaluate(p => {
    try {
      renderSchemaDdl(p);
      stageState.schemaddl = 'active'; show('schemaddl');
      return true;
    } catch (e) { return String(e); }
  }, plan4c);
  log('4c renders without error', ok4c === true, String(ok4c));
  await page.waitForTimeout(200);

  log('4c opens its own view', await page.isVisible('#view-schemaddl'));
  const tiles4c = await page.$$eval('#ddlTiles .tile', els => els.map(e => ({
    k: e.querySelector('.k').textContent.trim(), v: e.querySelector('.v').textContent.trim() })));
  log('4c shows four tiles', tiles4c.length === 4, JSON.stringify(tiles4c));
  log('4c counts tables', tiles4c.some(t => t.k === 'Tables' && +t.v > 0),
      JSON.stringify(tiles4c));

  const order = await page.textContent('#ddlOrder');
  log('4c shows the execution order', order.includes('tables') && order.includes('indexes'));
  log('4c marks where the data load happens', order.includes('THE DATA LOAD'));

  // The compile is stated in one place. It used to be three -- a dead panel,
  // a note box and the meta line -- and the dead panel said "Checking the
  // target..." on every load because no code ever wrote to it.
  const proof = await page.textContent('#ddlTargetStatus');
  log('4c says the compile did not run', proof.includes('did not run'), proof.slice(0, 90));
  log('4c does not claim the DDL is proven', !proof.includes('proven to execute'),
      proof.slice(0, 90));
  log('4c has no dead "checking the target" line', !/Checking the target/i.test(proof),
      proof.slice(0, 60));
  log('4c states its proof once',
      await page.$$eval('#view-schemaddl .status', e => e.length) === 1);

  // **One scroll region.** The statements list is a .scrollcap; the notes
  // list below it was not, so the notes took the height, the capped list
  // collapsed to its 150px floor and painted over their heading.
  const caps = await page.$$eval('#view-schemaddl .scrollcap', els => els.length);
  log('4c has exactly one capped list', caps === 1, String(caps));
  const spill = await page.evaluate(() => {
    const view = document.querySelector('#view-schemaddl').getBoundingClientRect();
    const cap = document.querySelector('#ddlGroups').getBoundingClientRect();
    return { over: Math.round(cap.bottom - view.bottom), h: Math.round(cap.height),
             parent: Math.round(document.querySelector('#ddlGroups')
               .parentElement.getBoundingClientRect().height) };
  });
  log('4c: the list stays inside its view', spill.over <= 1, JSON.stringify(spill));
  log('4c: the list gets real height', spill.h > 200, JSON.stringify(spill));

  const g4c = await geometry(page, '#ddlGroups summary.sctgrid');
  log('4c has a row per statement', g4c.rows > 20, String(g4c.rows));
  log('4c: no column overlaps', (g4c.overlap || []).length === 0,
      JSON.stringify((g4c.overlap || []).slice(0, 2)));
  log('4c: no cell text is clipped', (g4c.clipped || []).length === 0,
      JSON.stringify((g4c.clipped || []).slice(0, 2)));

  // Three panes of that one region. The notes split by whether anyone has to
  // do anything: 25 of DBMIG_APP's 31 notes are automatic type mappings and
  // listing them with the six that need a decision buried them.
  const tabs = await page.$$eval('#ddlTabs .chip', els => els.map(e => e.textContent.trim()));
  log('4c offers statements, review and mappings', tabs.length === 3, JSON.stringify(tabs));
  log('4c counts each pane in its tab', tabs.every(t => /·\s*\d+$/.test(t)),
      JSON.stringify(tabs));
  log('4c starts on the statements', await page.isVisible('#ddlStatements') &&
      !(await page.isVisible('#ddlNoteList')));

  await page.click('#ddlTabs .chip[data-ddl="review"]');
  await page.waitForTimeout(150);
  log('4c: the review pane opens', await page.isVisible('#ddlNoteList') &&
      !(await page.isVisible('#ddlStatements')));
  const gn = await geometry(page, '#ddlNoteList .noterow');
  log('4c lists what needs a person', gn.rows > 0, String(gn.rows));
  log('4c: notes do not overlap', (gn.overlap || []).length === 0,
      JSON.stringify((gn.overlap || []).slice(0, 2)));
  const sev = await page.$$eval('#ddlNoteList .sev', els =>
    [...new Set(els.map(e => e.textContent.trim()))]);
  log('4c: the review pane holds no info notes', !sev.includes('info'), JSON.stringify(sev));

  await page.click('#ddlTabs .chip[data-ddl="mappings"]');
  await page.waitForTimeout(150);
  const gm = await geometry(page, '#ddlMapList .noterow');
  log('4c lists the type mappings', gm.rows > 0, String(gm.rows));
  log('4c: mappings do not overlap', (gm.overlap || []).length === 0,
      JSON.stringify((gm.overlap || []).slice(0, 2)));

  await page.click('#ddlTabs .chip[data-ddl="statements"]');
  await page.waitForTimeout(150);
  await page.click('#ddlGroups details summary');
  await page.waitForTimeout(150);
  const body4c = await page.$eval('#ddlGroups details[open]', d => d.textContent);
  log('4c: an opened row shows the SQL', /CREATE|ALTER/i.test(body4c));

  log('4c: the page does not scroll sideways',
      !(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)));
  await page.screenshot({ path: path.join(__dirname, 'shots-appsql', 'ddl-1440.png') });

  // ------------------------------------------------------------ 4d: the table
  await page.evaluate(() => show('appsql'));
  await page.waitForSelector('#view-appsql', { state: 'visible' });
  await page.click('#btnAppsql');
  await page.waitForSelector('#appsqlResult', { state: 'visible', timeout: 60000 });
  await page.waitForFunction(() => document.querySelectorAll('#appsqlGroups details').length > 0,
                             { timeout: 60000 });

  const g4d = await geometry(page, '#appsqlGroups summary.sctrow');
  log('4d has a row per statement', g4d.rows === 18, String(g4d.rows));
  log('4d: no column overlaps', (g4d.overlap || []).length === 0,
      JSON.stringify((g4d.overlap || []).slice(0, 2)));
  log('4d: no cell text is clipped', (g4d.clipped || []).length === 0,
      JSON.stringify((g4d.clipped || []).slice(0, 2)));
  log('4d: five columns', g4d.cols === 5, String(g4d.cols));

  // 4d's proof, like 4c's, is stated once and is wired. The old check read
  // #appsqlNotes for "No PostgreSQL target" and so failed whenever a target
  // *was* registered -- it tested the demo's data condition, not the claim.
  // This holds either way: parse and result are separate, and nothing may
  // read as proven while no result comparison has run.
  const proof4d = await page.textContent('#appsqlTargetStatus');
  log('4d has no dead "checking the target" line', !/Checking the target/i.test(proof4d),
      proof4d.slice(0, 60));
  log('4d states its proof once',
      await page.$$eval('#view-appsql .status', e => e.length) === 1);
  log('4d reports the parse gate', /shadow table|Parse reported blocked|shadow schema/i
      .test(proof4d), proof4d.slice(0, 110));
  const compared4d = await page.evaluate(() => !!(APPSQL && APPSQL.results_compared));
  log('4d does not claim a rewrite is proven without a result comparison',
      compared4d || /No result comparison ran/i.test(proof4d), proof4d.slice(-120));
  log('4d does not repeat the proof in its notes',
      !/No result comparison ran|No PostgreSQL target\./i
        .test(await page.textContent('#appsqlNotes')));

  // ------------------------------------------------------------ phone width
  await page.setViewportSize({ width: 400, height: 900 });
  await page.waitForTimeout(250);
  log('4d: no sideways scroll at 400px',
      !(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)));
  await page.evaluate(() => show('schemaddl'));
  await page.waitForTimeout(200);
  log('4c: no sideways scroll at 400px',
      !(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)));
  await page.evaluate(() => show('convert'));
  await page.waitForTimeout(200);
  log('4b: no sideways scroll at 400px',
      !(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)));

  await browser.close();
  console.log(`\n${pass}/${pass + fail} checks passed`);
  process.exit(fail ? 1 : 0);
})();
