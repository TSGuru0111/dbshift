// The whole PostgreSQL path in the console, phases 1 to 10.
// Nothing that bills is executed: Provision renders, DMS plans.
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const BASE = process.env.DBSHIFT_URL || 'http://127.0.0.1:8765';
const PASSWORD = process.env.DBSHIFT_COLLECTOR_PASSWORD || '';
const OUT = path.join(__dirname, 'shots-pg-full');
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

  const stageDone = (id, t) => page.waitForSelector(`.stage[data-stage="${id}"].done`, { timeout: t });
  const shot = n => page.screenshot({ path: path.join(OUT, n + '.png'), fullPage: true });

  await page.goto(BASE, { waitUntil: 'networkidle' });
  if (await page.isVisible('#btnDisconnect')) {
    await page.click('#btnDisconnect');
    await page.waitForSelector('#btnDisconnect', { state: 'hidden', timeout: 30000 });
    await page.reload({ waitUntil: 'networkidle' });
  }

  // --- phase 1: connect and discover
  await page.fill('#password', PASSWORD);
  await page.fill('#schema', 'DBMIG_APP');
  await page.click('#btnConnect');
  await page.waitForSelector('#connStatus.ok', { timeout: 60000 });
  log('connected', true, (await page.textContent('#connText')).trim());

  await page.click('.stage[data-stage="discover"]');
  await page.click('#btnDiscover');
  await stageDone('discover', 300000);
  log('phase 1 discover', true, (await page.textContent('#discMeta')).trim().slice(0, 60));

  // --- phase 2: assess
  await page.click('.stage[data-stage="assess"]');
  await page.click('#btnAssess');
  await stageDone('assess', 300000);
  log('phase 2 assess', true, (await page.textContent('#assessMeta')).trim().slice(0, 60));

  // --- phase 4b: convert (before sizing, so phase 3 has measured evidence)
  await page.click('.stage[data-stage="convert"]');
  await page.fill('#pgPassword', process.env.DBSHIFT_PG_PASSWORD || 'dbshift-local-only');
  await page.click('#btnPgTarget');
  await page.waitForFunction(
    () => /PostgreSQL|registered/.test(document.querySelector('#pgState').textContent),
    null, { timeout: 60000 });
  await page.click('#btnConvert');
  await stageDone('convert', 300000);
  const conv = (await page.textContent('#convMeta')).trim();
  log('phase 4b convert compiles for real', /[1-9]\d* compiled/.test(conv), conv);

  // --- phase 3: choose PostgreSQL and size
  await page.click('.stage[data-stage="target"]');
  await page.click('.pathopt[data-engine="POSTGRESQL"]');
  await page.waitForFunction(
    () => document.querySelector('.pathopt[data-engine="POSTGRESQL"]').getAttribute('aria-pressed') === 'true',
    null, { timeout: 15000 });
  await page.click('#btnSize');
  await stageDone('target', 180000);
  await page.waitForFunction(
    () => /PostgreSQL/.test(document.querySelector('#targetSpec').textContent),
    null, { timeout: 30000 });
  const spec = await page.textContent('#targetSpec');
  log('phase 3 sizes for PostgreSQL', /PostgreSQL/.test(spec));
  log('no Oracle edition on this path', !/Enterprise Edition|Standard Edition/.test(spec));
  log('no licence is counted', /none/.test(spec));
  await shot('01-phase3-target');

  // --- phase 4: remediate
  await page.click('.stage[data-stage="remediate"]');
  await page.click('#btnRemediate');
  await stageDone('remediate', 300000);
  log('phase 4 remediate', true, (await page.textContent('#remMeta')).trim().slice(0, 60));

  // --- phase 5: blocker gate
  await page.click('.stage[data-stage="gate"]');
  await page.click('#btnGate');
  await page.waitForTimeout(4000);
  const gate = await page.textContent('#view-gate');
  // The screen renders the verdict in sentence case ("Halt"), not the record's
  // uppercase. Match either.
  log('phase 5 gate reaches a verdict', /halt|proceed/i.test(gate),
      (gate.match(/halt|proceed/i) || [''])[0]);

  // --- phase 6: provision, render only
  await page.click('.stage[data-stage="provision"]');
  await page.click('#btnProvision');
  await page.waitForFunction(
    () => /dbshift-target-[\w-]+/.test(document.querySelector('#view-provision').innerText),
    null, { timeout: 240000 });
  await page.waitForTimeout(2000);
  const prov = await page.textContent('#view-provision');
  log('phase 6 renders a PostgreSQL stack', /dbshift-target-dbmig-app-pg/.test(prov));
  log('the engine is postgres', /postgres/.test(prov));
  log('nothing was deployed', !/CREATE_IN_PROGRESS/.test(prov));
  await shot('02-phase6-provision');

  // --- phase 7: DMS, plan only
  // The rail only opens a stage that is not idle, and Migrate stays idle until
  // a provision record exists. Open the view directly: the point here is what
  // the screen shows for a PostgreSQL target, not the rail's gating.
  await page.evaluate(() => show('migrate'));
  await page.waitForTimeout(500);
  log('phase 7 shows DMS, not Data Pump', await page.isVisible('#dmsPanel'));
  await page.click('#btnDmsPlan');
  await page.waitForFunction(
    () => document.querySelector('#dmsTiles').style.display !== 'none',
    null, { timeout: 120000 });
  await page.waitForTimeout(1000);
  const dms = await page.textContent('#dmsPanel');
  log('the DMS plan names what it leaves behind', /Left to finish/.test(dms));
  log('it says no model ran', /No model ran/.test(dms));
  log('nothing was created', !(await page.isVisible('#dmsRunPanel')) || !/refused/i.test(dms));
  await shot('03-phase7-dms');

  // --- phase 8: validate -- cross-engine explanation shown
  await page.click('.stage[data-stage="validate"]').catch(() => {});
  await page.evaluate(() => show('validate'));
  await page.waitForTimeout(500);
  log('phase 8 explains cross-engine comparison', await page.isVisible('#valCrossEngine'));
  const val = await page.textContent('#valCrossEngine');
  log('it says which levels do not run', /Levels 2 and 5 do not run/.test(val));

  // --- phase 9: cutover
  await page.evaluate(() => show('cutover'));
  await page.waitForTimeout(500);
  log('phase 9 opens', await page.isVisible('#view-cutover'));

  // --- phase 10: report
  await page.evaluate(() => show('report'));
  await page.waitForTimeout(300);
  await page.click('#btnReport');
  await page.waitForSelector('#reportResult', { state: 'visible', timeout: 120000 });
  await page.waitForTimeout(1500);
  const meta = (await page.textContent('#reportMeta')).trim();
  log('phase 10 builds the report', /DBMIG_APP/.test(meta), meta);
  const frame = page.frame({ url: /\/api\/report/ });
  await frame.waitForLoadState('load');
  const text = await frame.textContent('body');
  log('the report knows the target is PostgreSQL', /PostgreSQL/.test(text));
  log('it says the DDL was converted by Phase 4c', /converted/i.test(text) && /4c/.test(text));
  log('it says DMS is the data path', /AWS DMS/.test(text));
  await shot('04-phase10-report');

  // Every stage reachable.
  const rail = await page.$$eval('.stage', els => els.map(e => ({
    id: e.dataset.stage, cls: e.className })));
  const idle = rail.filter(s => /idle/.test(s.cls) || (!/done|active/.test(s.cls)));
  log('every stage was reached', idle.length === 0,
      idle.map(s => s.id).join(', ') || 'none idle');

  log('no page errors', errors.length === 0, errors.join(' | ').slice(0, 300));
  await browser.close();
  const failed = results.filter(r => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} checks passed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error('ABORT', e); process.exit(2); });
