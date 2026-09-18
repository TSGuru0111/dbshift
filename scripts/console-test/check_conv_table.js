// Checks that Phase 4b's object list renders as a grouped table, not a flat
// wall of cards, and that the row geometry matches the SCT panels and 4d.
//
// 4b needs a discovery run to *produce* a plan, but the console renders a
// stored one on load, so this drives the render rather than the run.
const { chromium } = require('playwright-core');

const BASE = process.env.DBSHIFT_URL || 'http://127.0.0.1:8765';
let pass = 0, fail = 0;
function log(name, ok, detail = '') {
  ok ? pass++ : fail++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  -- ' + detail : ''}`);
}

(async () => {
  const b = await chromium.launch({ channel: 'msedge' });
  const page = await b.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on('pageerror', e => log('no page error', false, e.message));
  await page.goto(BASE, { waitUntil: 'networkidle' });

  // Render a stored plan directly: the table is a rendering concern, and
  // requiring a live Oracle here would make this untestable offline.
  const plan = JSON.parse(require('fs').readFileSync(
    require('path').join(__dirname, '..', '..', 'convert', 'output', 'conversion_plan.json'),
    'utf8'));
  await page.evaluate(p => { window.__plan = p; }, plan);
  const ok = await page.evaluate(() => {
    try {
      // renderConversion fetches; call the DOM half with the stored plan.
      const p = window.__plan;
      const byStatus = {};
      for (const e of p.entries) (byStatus[e.status] ||= []).push(e);
      const head = `<div class="sctrow" style="padding:4px 14px"><span>state</span>
        <span>type</span><span>object</span><span class="obj">where it came from</span>
        <span class="act">what to do</span></div>`;
      document.querySelector('#convList').innerHTML =
        CONV_ORDER.filter(st => byStatus[st]).map(st => {
          const items = byStatus[st];
          return `<div class="mt-s"><div class="eyebrow">${CONV_LABEL[st] || st}
            <span> — ${items.length}</span></div>${head}
            <div class="acc">${items.map(e => convRow(e, CONV_TONE[st] || 'INFO')).join('')}</div></div>`;
        }).join('');
      document.querySelector('#convResult').style.display = '';
      return true;
    } catch (e) { return String(e); }
  });
  log('the table renders without error', ok === true, String(ok));

  await page.evaluate(() => { stageState.convert = 'active'; show('convert'); });
  await page.waitForSelector('#view-convert', { state: 'visible' });
  await page.waitForTimeout(200);

  const groups = await page.$$eval('#convList > div > .eyebrow', els =>
    els.map(e => e.textContent.replace(/\s+/g, ' ').trim()));
  log('objects are grouped by outcome', groups.length >= 2, groups.join(' | '));

  const heads = await page.$$eval('#convList > div > .sctrow', els => els.length);
  const rows = await page.$$eval('#convList summary.sctrow', els => els.length);
  log('every group carries a header row', heads === groups.length,
      `${heads} header rows, ${groups.length} groups`);
  log('every object has a row', rows > 20, String(rows));

  const geom = await page.$$eval('#convList summary.sctrow', els => {
    const lines = els.map(el => {
      const t = el.children[2];
      const lh = parseFloat(getComputedStyle(t).lineHeight) || 20;
      return Math.round(t.getBoundingClientRect().height / lh);
    });
    return { maxLines: Math.max(...lines),
             cols: getComputedStyle(els[0]).gridTemplateColumns.split(' ').length };
  });
  log('no object name wraps at desktop width', geom.maxLines === 1, `max ${geom.maxLines}`);
  log('the row keeps five columns, like the SCT panels', geom.cols === 5, String(geom.cols));

  // **Columns must not overlap.** The first version of this table put the full
  // label ("Compiled — needs your approval") in the 78px state chip, which is
  // white-space:nowrap, so it spilled across the type and object columns and
  // made every row unreadable. CSS review did not catch it; measuring the
  // rendered boxes does.
  const overlap = await page.$$eval('#convList summary.sctrow', els => {
    const bad = [];
    for (const el of els) {
      const boxes = [...el.children].map(c => c.getBoundingClientRect());
      for (let i = 0; i < boxes.length - 1; i++) {
        // A cell may not start before the previous one ends.
        if (boxes[i].right > boxes[i + 1].left + 0.5) {
          bad.push({ cell: i, text: el.children[i].textContent.trim().slice(0, 40),
                     overhang: Math.round(boxes[i].right - boxes[i + 1].left) });
        }
      }
    }
    return bad;
  });
  log('no column overlaps the next', overlap.length === 0,
      JSON.stringify(overlap.slice(0, 3)));

  // Every cell's text must fit its own box, not merely avoid overlapping.
  const clipped = await page.$$eval('#convList summary.sctrow', els => {
    const bad = [];
    for (const el of els) {
      for (const cell of el.children) {
        if (cell.scrollWidth > cell.clientWidth + 1 &&
            getComputedStyle(cell).textOverflow !== 'ellipsis') {
          bad.push({ text: cell.textContent.trim().slice(0, 30),
                     over: cell.scrollWidth - cell.clientWidth });
        }
      }
    }
    return bad;
  });
  log('no cell text exceeds its column', clipped.length === 0,
      JSON.stringify(clipped.slice(0, 3)));

  // The type column must hold "package body" on one line.
  const typeLines = await page.$$eval('#convList summary.sctrow', els =>
    Math.max(...els.map(el => {
      const c = el.children[1];
      const lh = parseFloat(getComputedStyle(c).lineHeight) || 20;
      return Math.round(c.getBoundingClientRect().height / lh);
    })));
  log('"package body" fits the type column on one line', typeLines === 1,
      `max ${typeLines} lines`);
  log('the page does not scroll sideways',
      !(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)));

  // Opening a row must still show everything the card used to.
  await page.click('#convList details summary');
  await page.waitForTimeout(150);
  const body = await page.$eval('#convList details[open]', d => d.textContent);
  log('an opened row still shows what a person must confirm',
      body.includes('Before this is applied'));
  log('an opened row still shows the gates', body.includes('compile') || body.includes('parity'));

  await page.setViewportSize({ width: 400, height: 900 });
  await page.waitForTimeout(200);
  log('no sideways scroll at 400px',
      !(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)));
  const narrow = await page.$eval('#convList summary.sctrow',
    el => getComputedStyle(el).gridTemplateColumns.split(' ').length);
  log('the row collapses to three columns on a phone', narrow === 3, String(narrow));

  await b.close();
  console.log(`\n${pass}/${pass + fail} checks passed`);
  process.exit(fail ? 1 : 0);
})();
