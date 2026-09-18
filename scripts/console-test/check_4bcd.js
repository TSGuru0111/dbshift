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

  const notesBox = await page.textContent('#ddlNotesBox');
  log('4c says the compile did not run', notesBox.includes('did not run'), notesBox.slice(0, 90));
  log('4c does not claim the DDL is proven', !notesBox.includes('proven to execute'),
      notesBox.slice(0, 90));

  const g4c = await geometry(page, '#ddlGroups summary.sctgrid');
  log('4c has a row per statement', g4c.rows > 20, String(g4c.rows));
  log('4c: no column overlaps', (g4c.overlap || []).length === 0,
      JSON.stringify((g4c.overlap || []).slice(0, 2)));
  log('4c: no cell text is clipped', (g4c.clipped || []).length === 0,
      JSON.stringify((g4c.clipped || []).slice(0, 2)));

  const gn = await geometry(page, '#ddlNoteList summary.sctgrid');
  log('4c lists the reviewer notes', gn.rows > 0, String(gn.rows));
  log('4c: notes do not overlap', (gn.overlap || []).length === 0,
      JSON.stringify((gn.overlap || []).slice(0, 2)));

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

  const notes4d = await page.textContent('#appsqlNotes');
  log('4d says the statements are unproven', notes4d.includes('No PostgreSQL target'));

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
