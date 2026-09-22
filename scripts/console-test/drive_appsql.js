// Drives Phase 4d in the browser: Application SQL.
//
// What only a browser can settle here:
//
//   1. **The phase arms without a connection.** 4d reads mapper files, not the
//      database, so its button must be live on first load -- unlike every
//      other phase. If it needed a discovery run the phase would be useless at
//      a client site where nobody has granted database access yet.
//   2. **The row grid does not collapse.** The five-column layout is shared
//      with the SCT panels, and a side-by-side attempt on 2026-09-17 wrapped
//      every title one word per line. Measured here, not assumed.
//   3. **The honest numbers reach the screen.** The tile must show the
//      statement-level percentage (11%), never the construct-level one (8 of 27),
//      because the construct figure flatters the tooling.
//   4. **The unproven state is visible as unproven.** With no target the
//      statements must read "Rewritten, not yet proven", and the band above
//      the run must say what parse and result established either way -- a
//      client shown "ready" about something no engine ran is being told
//      something untrue.
//
// Run: node drive_appsql.js          (DBSHIFT_URL, default http://127.0.0.1:8765)
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const BASE = process.env.DBSHIFT_URL || 'http://127.0.0.1:8765';
const OUT = path.join(__dirname, 'shots-appsql');
fs.mkdirSync(OUT, { recursive: true });

let pass = 0, fail = 0;
function log(name, ok, detail = '') {
  ok ? pass++ : fail++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  -- ' + detail : ''}`);
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge' });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on('pageerror', e => log('no page error', false, e.message));
  await page.goto(BASE, { waitUntil: 'networkidle' });

  // ---------------------------------------------------------------- the rail
  const rail = await page.$$eval('#rail *', els =>
    els.map(e => e.textContent.trim()).filter(Boolean));
  log('the rail carries Application SQL', rail.some(t => t.includes('Application SQL')),
      rail.slice(0, 20).join(' | '));
  log('it is numbered 4d', rail.some(t => t.includes('4d')));

  // -------------------------------------------- it arms without a connection
  await page.click('text=Application SQL');
  await page.waitForSelector('#view-appsql', { state: 'visible' });
  log('the phase opens without connecting',
      await page.isVisible('#view-appsql'));
  log('the button is armed on first load',
      !(await page.$eval('#btnAppsql', b => b.disabled)));
  log('the default path is filled in',
      (await page.$eval('#appsqlRoot', i => i.value)).includes('mappers'));
  // The markup wraps across lines, so match on collapsed whitespace rather
  // than a literal substring.
  const emptyText = (await page.textContent('#appsqlEmpty')).replace(/\s+/g, ' ');
  log('the empty state explains what to do',
      emptyText.includes('mapper files') && emptyText.includes('Nothing else in the pipeline'),
      emptyText);

  // -------------------------------------------------------------------- run
  await page.click('#btnAppsql');
  await page.waitForSelector('#appsqlResult', { state: 'visible', timeout: 60000 });
  await page.waitForFunction(() => document.querySelectorAll('#appsqlGroups details').length > 0,
                             { timeout: 60000 });

  const meta = await page.textContent('#appsqlMeta');
  log('the meta line names the file and statement counts',
      /2 file\(s\).*18 statements/.test(meta), meta);
  log('the meta line says nothing was applied', meta.includes('nothing applied'), meta);

  // ------------------------------------------------- the honest percentage
  const tiles = await page.$$eval('#appsqlTiles .tile', els => els.map(e => ({
    k: e.querySelector('.k').textContent.trim(),
    v: e.querySelector('.v').textContent.trim(),
    h: e.querySelector('.h').textContent.trim(),
  })));
  log('four tiles render', tiles.length === 4, JSON.stringify(tiles));
  const byRule = tiles.find(t => t.k === 'By rule');
  log('the rule tile counts statements, not constructs', byRule && byRule.v === '2',
      byRule && byRule.v);
  log('the percentage shown is the statement-level 11%',
      byRule && byRule.h.includes('11%'), byRule && byRule.h);
  log('the construct-level 31% is NOT shown',
      !tiles.some(t => t.h.includes('31%')));
  const person = tiles.find(t => t.k === 'Need a person');
  log('six statements need a person', person && person.v === '6', person && person.v);

  // --------------------------------------------- unproven reads as unproven
  // The proof moved out of #appsqlNotes on 2026-09-20. The band above the run
  // is now the single place parse and result are stated, and the note box
  // keeps only what the run could not *cover*. These read the band, and they
  // hold whether or not this machine happens to have a target registered --
  // the claim under test is that nothing reads as proven, not that the target
  // is missing.
  const proof = await page.textContent('#appsqlTargetStatus');
  const compared = await page.evaluate(() => !!(APPSQL && APPSQL.results_compared));
  log('the band states what the parse gate did',
      /shadow table|Parse reported blocked|parsed on/i.test(proof), proof.slice(0, 110));
  log('the band names the missing result comparison',
      compared || /No result comparison ran/i.test(proof), proof.slice(-140));
  log('the band explains the ROWNUM risk', compared || proof.includes('ROWNUM'));
  log('the caveat box does not repeat the proof',
      !/No result comparison ran|No PostgreSQL target\./i
        .test(await page.textContent('#appsqlNotes')));

  const groups = await page.$$eval('#appsqlGroups .eyebrow', els =>
    els.map(e => e.textContent.trim()));
  log('a group reads "Rewritten, not yet proven"',
      groups.some(g => g.includes('Rewritten, not yet proven')), groups.join(' | '));
  log('no group claims "ready" without a target',
      !groups.some(g => /\bProven\b/.test(g)), groups.join(' | '));
  log('a group reads "Needs a person"', groups.some(g => g.includes('Needs a person')));

  // ------------------------------------------------------ the row geometry
  const geom = await page.$$eval('#appsqlGroups summary.sctrow', els => {
    const r = els.map(el => {
      const title = el.children[2];
      const lh = parseFloat(getComputedStyle(title).lineHeight) || 20;
      return Math.round(title.getBoundingClientRect().height / lh);
    });
    return { rows: els.length, maxLines: Math.max(...r),
             cols: getComputedStyle(els[0]).gridTemplateColumns };
  });
  log('every statement has a row', geom.rows === 18, String(geom.rows));
  log('no title wraps at desktop width', geom.maxLines === 1, `max ${geom.maxLines} lines`);
  log('the row keeps five columns', geom.cols.split(' ').length === 5, geom.cols);

  // Columns must not overlap. Phase 4b's table shipped with the full status
  // label in the 78px state chip, which is white-space:nowrap, so it spilled
  // across the next two columns. Measured here so 4d cannot repeat it.
  const overlap = await page.$$eval('#appsqlGroups summary.sctrow', els => {
    const bad = [];
    for (const el of els) {
      const boxes = [...el.children].map(c => c.getBoundingClientRect());
      for (let i = 0; i < boxes.length - 1; i++) {
        if (boxes[i].right > boxes[i + 1].left + 0.5) {
          bad.push({ cell: i, text: el.children[i].textContent.trim().slice(0, 40) });
        }
      }
    }
    return bad;
  });
  log('no column overlaps the next', overlap.length === 0,
      JSON.stringify(overlap.slice(0, 3)));

  const overflow = await page.evaluate(() =>
    document.documentElement.scrollWidth > window.innerWidth + 1);
  log('the page does not scroll sideways', !overflow);

  // ------------------------------------------------ an opened row explains
  await page.click('#appsqlGroups details summary');
  await page.waitForTimeout(150);
  const open = await page.$eval('#appsqlGroups details[open]', d => d.textContent);
  log('an opened row shows the Oracle SQL', open.includes('Oracle, as the application sends it'));
  log('an opened row shows the proposed PostgreSQL', open.includes('PostgreSQL, proposed'));
  log('an opened row explains why it is not a text swap',
      open.includes('not a text swap'));

  await page.screenshot({ path: path.join(OUT, 'appsql-1440.png'), fullPage: true });

  // ------------------------------------------------------------ phone width
  await page.setViewportSize({ width: 400, height: 900 });
  await page.waitForTimeout(200);
  const narrowOverflow = await page.evaluate(() =>
    document.documentElement.scrollWidth > window.innerWidth + 1);
  log('no sideways scroll at 400px', !narrowOverflow);
  const narrowCols = await page.$eval('#appsqlGroups summary.sctrow',
    el => getComputedStyle(el).gridTemplateColumns.split(' ').length);
  log('the row collapses to three columns on a phone', narrowCols === 3, String(narrowCols));
  await page.screenshot({ path: path.join(OUT, 'appsql-400.png'), fullPage: true });

  await browser.close();
  console.log(`\n${pass}/${pass + fail} checks passed`);
  process.exit(fail ? 1 : 0);
})();
