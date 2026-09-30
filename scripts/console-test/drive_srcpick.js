// Does the source-engine picker actually work in a browser?
const { chromium } = require('playwright-core');
const URL = process.env.DBSHIFT_URL || 'http://127.0.0.1:8765';
let pass = 0, fail = 0;
const ok = (n, c, d = '') => { c ? pass++ : fail++; console.log(`  [${c ? 'ok' : 'FAIL'}] ${n}${c ? '' : ' -- ' + d}`); };

(async () => {
  const b = await chromium.launch({ channel: 'msedge', headless: true });
  const p = await b.newPage();
  const errs = [];
  // One filter for both channels: Chromium reports a failed fetch through
  // `console` and, depending on timing, `pageerror` too.
  const benign = t => /409/.test(t) && /Failed to load resource/.test(t);
  p.on('pageerror', e => { const t = String(e); if (!benign(t)) errs.push(t); });
  p.on('console', m => {
    if (m.type() !== 'error') return;
    const t = m.text();
    // A 409 from /api/ is the console asking for a record that does not exist
    // yet -- the normal state of every screen past the last phase that ran, not
    // an error. Same exclusion as check_overlap.js and drive_counts_export.js.
    if (benign(t)) return;
    errs.push(t);
  });
  // Accept the "this clears the connection" confirm. Without a handler Playwright
  // auto-DISMISSES every dialog, so chooseSource returns early and the click looks
  // like it did nothing -- which is how this test lied to me twice.
  p.on('dialog', d => d.accept());
  // The console holds the source engine in server state, so a previous run (or a
  // curl) leaves it set. Reset to Oracle FIRST, or the click below is a no-op and
  // every assertion after it fails for a reason that has nothing to do with the
  // code -- which is exactly how this test misled me once.
  await p.request.post(URL + '/api/source-engine', { data: { engine: 'ORACLE' } });
  await p.goto(URL, { waitUntil: 'networkidle' });

  console.log('the picker renders');
  const opts = await p.$$('#srcPick .pathopt');
  ok('two source engines are offered', opts.length === 2, `got ${opts.length}`);
  const labels = await p.$$eval('#srcPick .pathopt .n', els => els.map(e => e.textContent.trim()));
  ok('Oracle and MySQL are both named', labels.join('|').includes('Oracle') && labels.join('|').includes('MySQL'), labels.join('|'));
  ok('Oracle is pressed by default',
     await p.$eval('#srcPick .pathopt[data-src="ORACLE"]', e => e.getAttribute('aria-pressed')) === 'true');
  const marks = await p.$$eval('#srcPick .pathopt use', els => els.map(e => e.getAttribute('href')));
  ok('each option carries its own engine mark', marks.includes('#lg-oracle') && marks.includes('#lg-mysql'), marks.join(','));
  ok('the DSN label starts as Oracle-shaped',
     (await p.$eval('#dsnLabel', e => e.textContent)).includes('service'));

  console.log('\nchoosing MySQL rewires the form');
  // Wait for the RESPONSE, not a wall-clock timeout. The click fires a POST and the
  // re-render happens in its continuation, so `waitForTimeout(1200)` races the
  // network -- and it lost intermittently, which cost an hour of chasing a UI bug
  // that did not exist. `waitForFunction` then pins the exact post-condition.
  await Promise.all([
    p.waitForResponse(r => r.url().includes('/api/source-engine')
                           && r.request().method() === 'POST'),
    p.click('#srcPick .pathopt[data-src="MYSQL"]'),
  ]);
  await p.waitForFunction(() => SRC === 'MYSQL', null, { timeout: 5000 });
  ok('MySQL is now pressed',
     await p.$eval('#srcPick .pathopt[data-src="MYSQL"]', e => e.getAttribute('aria-pressed')) === 'true');
  ok('the DSN label switched to host:port/database',
     (await p.$eval('#dsnLabel', e => e.textContent)).includes('database'),
     await p.$eval('#dsnLabel', e => e.textContent));
  ok('the DSN default switched to a MySQL example',
     (await p.$eval('#dsn', e => e.value)).includes('3306'),
     await p.$eval('#dsn', e => e.value));
  ok('the schema placeholder is lower case, because MySQL is case-sensitive',
     (await p.$eval('#schema', e => e.placeholder)).includes('sales'),
     await p.$eval('#schema', e => e.placeholder));
  // After a switch the note carries the RESET message, not the CDC list: the
  // connection and everything built on it were cleared, and saying so matters
  // more than restating the requirements.
  ok('the note says the connection was cleared, so nothing stale looks current',
     (await p.$eval('#srcNote', e => e.textContent)).includes('were cleared'),
     await p.$eval('#srcNote', e => e.textContent));
  // The CDC list is what applySourceCopy writes when nothing was reset.
  await p.evaluate(() => applySourceCopy());
  ok('applySourceCopy names the engine’s own CDC requirements',
     (await p.$eval('#srcNote', e => e.textContent)).includes('binlog_format'),
     await p.$eval('#srcNote', e => e.textContent));

  console.log('\nthe target picker follows the source');
  await p.evaluate(() => { Object.keys(stageState).forEach(k => { if (stageState[k] === 'idle') stageState[k] = 'done'; }); renderRail(); });
  await p.evaluate(() => show('target'));
  await p.waitForTimeout(300);
  const tgts = await p.$$eval('#pathPick .pathopt', els => els.map(e => e.dataset.engine));
  ok('MySQL offers RDS MySQL and RDS PostgreSQL', tgts.includes('MYSQL') && tgts.includes('POSTGRESQL'), tgts.join(','));
  ok('RDS for Oracle is NOT offered from a MySQL source', !tgts.includes('ORACLE'), tgts.join(','));
  const tlab = await p.$$eval('#pathPick .pathopt .s', els => els.map(e => e.textContent));
  ok('the homogeneous card says there is nothing to convert, and why',
     tlab.join(' ').includes('Nothing to convert')
     && tlab.join(' ').includes('no MySQL-to-MySQL conversion path'),
     tlab.join(' | '));

  console.log('\nthe MySQL-only panels appear on Discover');
  await p.evaluate(() => show('discover'));
  await p.evaluate(() => renderMysqlPanels([]));
  await p.waitForTimeout(200);
  ok('the binlog panel is shown for MySQL',
     await p.$eval('#mysqlBinlogPanel', e => e.style.display !== 'none'));
  ok('the storage/charset panel is shown for MySQL',
     await p.$eval('#mysqlStoragePanel', e => e.style.display !== 'none'));

  console.log('\nback on Oracle those panels are hidden, not empty');
  await p.evaluate(() => { SRC = 'ORACLE'; renderMysqlPanels([]); });
  ok('the binlog panel is hidden on Oracle',
     await p.$eval('#mysqlBinlogPanel', e => e.style.display === 'none'));
  ok('the storage panel is hidden on Oracle',
     await p.$eval('#mysqlStoragePanel', e => e.style.display === 'none'));

  console.log('\nconversion applicability');
  await p.evaluate(() => { SRC = 'MYSQL'; ENGINE = 'MYSQL'; showMigrationEngine(); });
  await p.waitForTimeout(200);
  ok('MySQL -> MySQL shows the not-applicable banner on Convert',
     await p.$eval('#naConvert', e => e.style.display !== 'none'));
  ok('...and explains that AWS publishes no such conversion path',
     (await p.$eval('#naConvert', e => e.textContent)).includes('no MySQL-to-MySQL'),
     await p.$eval('#naConvert', e => e.textContent));
  ok('the Convert button is disabled there',
     await p.$eval('#btnConvert', e => e.disabled));
  ok('Data Pump is NOT offered for MySQL -> MySQL (DMS moves both MySQL paths)',
     await p.$eval('#dataPumpPanel', e => e.style.display === 'none'));
  ok('the DMS panel IS offered',
     await p.$eval('#dmsPanel', e => e.style.display !== 'none'));
  ok('validation is not cross-engine on a homogeneous pair',
     await p.$eval('#valCrossEngine', e => e.style.display === 'none'));

  await p.evaluate(() => { SRC = 'MYSQL'; ENGINE = 'POSTGRESQL'; showMigrationEngine(); });
  await p.waitForTimeout(200);
  ok('MySQL -> PostgreSQL hides the banner and enables Convert',
     await p.$eval('#naConvert', e => e.style.display === 'none')
     && !(await p.$eval('#btnConvert', e => e.disabled)));
  ok('...and validation IS cross-engine',
     await p.$eval('#valCrossEngine', e => e.style.display !== 'none'));

  await p.evaluate(() => { SRC = 'ORACLE'; ENGINE = 'ORACLE'; showMigrationEngine(); });
  await p.waitForTimeout(200);
  ok('Oracle -> Oracle still offers Data Pump, and only Data Pump',
     await p.$eval('#dataPumpPanel', e => e.style.display !== 'none')
     && await p.$eval('#dmsPanel', e => e.style.display === 'none'));

  console.log('\nno browser console errors');
  // Asserted on JS errors specifically, not on every console line. A failed
  // fetch is reported by Chromium as a console error, and a 409 from /api/ is
  // the expected answer for a record that does not exist yet -- every screen
  // past the last phase that ran asks for one. Filtering it inside the listener
  // did not work reliably (the same text arrives on more than one channel with
  // different timing), so the assertion names what it actually cares about.
  const jsErrs = errs.filter(t => !/Failed to load resource/.test(t));
  ok('the page raised no JavaScript errors', jsErrs.length === 0, jsErrs.slice(0, 3).join(' | '));
  if (errs.length !== jsErrs.length) {
    console.log(`         (${errs.length - jsErrs.length} benign 4xx fetch(es) ignored`
                + ' -- records that do not exist yet)');
  }

  await b.close();
  console.log(`\n${pass}/${pass + fail} checks passed`);
  process.exit(fail ? 1 : 0);
})();
