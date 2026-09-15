// Does the console show the DMS path, what it leaves behind, and the fact that
// no model ran? Plans only; creates nothing and bills nothing.
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const BASE = process.env.DBSHIFT_URL || 'http://127.0.0.1:8765';
const OUT = path.join(__dirname, 'shots-dms');
fs.mkdirSync(OUT, { recursive: true });

const results = [];
function log(name, ok, detail = '') {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  -- ' + detail : ''}`);
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error' && !/40\d \(/.test(m.text())) errors.push('console: ' + m.text()); });
  page.on('response', r => {
    if (r.status() >= 400 && !(r.status() === 409 && /\/api\//.test(r.url())))
      errors.push(`${r.status()} ${r.url()}`);
  });
  const shot = n => page.screenshot({ path: path.join(OUT, n + '.png'), fullPage: true });

  await page.goto(BASE, { waitUntil: 'networkidle' });

  // The chosen target is server state that survives a reload, so set it
  // explicitly rather than depending on what a previous run left behind.
  await page.request.post(BASE + '/api/engine', {data: {engine: 'POSTGRESQL'}});
  await page.reload({waitUntil: 'networkidle'});
  const st = await (await page.request.get(BASE + '/api/state')).json();
  log('the server holds the PostgreSQL choice', st.engine === 'POSTGRESQL', st.engine);

  await page.evaluate(() => show('migrate'));
  await page.waitForTimeout(400);
  log('the DMS panel is shown on the PostgreSQL path', await page.isVisible('#dmsPanel'));
  log('the Data Pump panel is hidden', !(await page.isVisible('#dataPumpPanel')));
  const intro = await page.textContent('#view-migrate');
  log('the screen explains why Data Pump cannot be used', /Oracle-only format/.test(intro));
  log('the hourly bill is stated up front', /bills by the\s+hour/.test(intro));

  await page.click('#btnDmsPlan');
  await page.waitForFunction(
    () => document.querySelector('#dmsTiles').style.display !== 'none',
    null, { timeout: 120000 });
  await page.waitForTimeout(800);

  const tiles = await page.$$eval('#dmsTiles .tile', els => els.map(e => e.textContent.replace(/\s+/g, ' ').trim()));
  log('four tiles summarise the plan', tiles.length === 4, String(tiles.length));
  log('tables moved is shown', /Tables moved/.test(tiles.join(' ')));
  log('what is left to finish is shown', /Left to finish/.test(tiles.join(' ')));

  const body = await page.textContent('#dmsPanel');
  log('lower-case folding is explained', /folded to lower case/.test(body));
  log('the preflight verdict is shown', (await page.$$('#dmsChecks .check')).length > 0);

  // What DMS holds back, each with a reason.
  const held = await page.$$eval('#dmsTables details', els => els.map(
    e => e.querySelector('summary').textContent.replace(/\s+/g, ' ').trim()));
  log('held-back tables are named', held.some(h => /internal|mview|external/.test(h)),
      String(held.length) + ' rows');
  log('the external table is held back', held.some(h => /EXT_CUSTOMER_EXTRACT/.test(h)));

  // The residue: what AI would finish, and the fact that none ran.
  const residue = await page.$$eval('#dmsResidue details', els => els.map(
    e => e.querySelector('summary').textContent.replace(/\s+/g, ' ').trim()));
  log('residue items are listed', residue.length > 0, String(residue.length) + ' items');
  log('sequences are marked ready', residue.some(r => /ready.*SEQ_/.test(r)),
      residue.find(r => /SEQ_/.test(r)) || 'none');
  log('a stand-in is labelled as such', residue.some(r => /stand-in/.test(r)),
      residue.find(r => /stand-in/.test(r)) || 'none');
  log('an item needing a person is marked', residue.some(r => /needs a person/.test(r)));

  const note = await page.textContent('#dmsResidueNote');
  log('the screen says no model ran', /No model ran/.test(note), note.slice(0, 80).trim());
  log('it names Bedrock as blocked', /Bedrock/.test(note));
  log('it says nothing was invented', /Nothing is invented|never/.test(note + body));

  // Open a stand-in and check the SQL and the label are both there.
  const idx = residue.findIndex(r => /stand-in/.test(r));
  if (idx >= 0) {
    await page.$$eval('#dmsResidue details', (els, i) => els[i].open = true, idx);
    await page.waitForTimeout(300);
    const opened = await page.$$eval('#dmsResidue details', (els, i) => els[i].textContent, idx);
    log('the stand-in shows its SQL', /CREATE (OR REPLACE VIEW|MATERIALIZED VIEW)/.test(opened));
    log('the stand-in is not passed off as model output',
        /not model output/i.test(opened), opened.slice(0, 120).replace(/\s+/g, ' '));
  }
  await shot('01-dms-postgres');

  // Nothing was created.
  log('the run panel is hidden while the preflight refuses',
      !(await page.isVisible('#dmsRunPanel')) || !/refused/i.test(body));

  // Switching back to Oracle shows Data Pump again.
  await page.evaluate(() => show('target'));
  await page.click('.pathopt[data-engine="ORACLE"]');
  await page.waitForFunction(
    () => document.querySelector('.pathopt[data-engine="ORACLE"]').getAttribute('aria-pressed') === 'true',
    null, { timeout: 15000 });
  await page.evaluate(() => show('migrate'));
  await page.waitForTimeout(400);
  log('Oracle shows Data Pump again', await page.isVisible('#dataPumpPanel'));
  log('and hides DMS', !(await page.isVisible('#dmsPanel')));
  await shot('02-oracle-datapump');

  // Phone width.
  await page.evaluate(() => show('migrate'));
  await page.setViewportSize({ width: 400, height: 800 });
  await page.waitForTimeout(300);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
  log('no horizontal scroll at 400px', !overflow);

  log('no page errors', errors.length === 0, errors.join(' | ').slice(0, 300));
  await browser.close();
  const failed = results.filter(r => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} checks passed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error('ABORT', e); process.exit(2); });
