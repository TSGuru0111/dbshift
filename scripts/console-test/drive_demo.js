// A demo walkthrough of the console: every phase, 1 to 10, one screenshot each,
// against the telco estate and the live PostgreSQL target on account 280646578374.
//
// This is the *demo* driver. drive_pg_full.js asserts the PostgreSQL path is
// correct; this one records what a person is shown while walking through it, so
// the screenshots are the deliverable and the assertions are secondary.
//
// Nothing here spends money: Provision renders a template, Migrate plans a DMS
// task, Cutover builds a certificate. The two writes that bill -- deploy and
// cutover --execute -- are never clicked.
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const BASE = process.env.DBSHIFT_URL || 'http://127.0.0.1:8765';
const PASSWORD = process.env.DBSHIFT_COLLECTOR_PASSWORD || '';
const ESTATE = process.env.DBSHIFT_DEMO_ESTATE || 'DBMIG_TELCO';
const OUT = path.join(__dirname, 'shots-demo');
fs.mkdirSync(OUT, { recursive: true });

const steps = [];
function log(phase, title, ok, detail = '') {
  steps.push({ phase, title, ok, detail });
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${String(phase).padEnd(3)} ${title}${detail ? '  -- ' + detail : ''}`);
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => {
    if (m.type() === 'error' && !/40\d \(/.test(m.text())) errors.push('console: ' + m.text());
  });

  const stageDone = (id, t) => page.waitForSelector(`.stage[data-stage="${id}"].done`, { timeout: t });
  // Screenshots are the point here, so each one is named for the phase it shows
  // and taken after the screen has settled rather than mid-render.
  const shot = async (n) => {
    await page.waitForTimeout(700);
    await page.screenshot({ path: path.join(OUT, n + '.png'), fullPage: true });
  };
  const text = sel => page.textContent(sel).then(t => (t || '').trim());

  await page.goto(BASE, { waitUntil: 'networkidle' });
  if (await page.isVisible('#btnDisconnect')) {
    await page.click('#btnDisconnect');
    await page.waitForSelector('#btnDisconnect', { state: 'hidden', timeout: 30000 });
    await page.reload({ waitUntil: 'networkidle' });
  }
  await shot('00-console');

  // ---- connect
  await page.fill('#password', PASSWORD);
  await page.fill('#schema', ESTATE);
  await page.click('#btnConnect');
  await page.waitForSelector('#connStatus.ok', { timeout: 60000 });
  log('0', 'connected to the source', true, await text('#connText'));

  // ---- phase 1: discover
  await page.click('.stage[data-stage="discover"]');
  await page.click('#btnDiscover');
  await stageDone('discover', 300000);
  log(1, 'discover', true, (await text('#discMeta')).slice(0, 70));
  await shot('01-discover');

  // ---- phase 2: assess
  await page.click('.stage[data-stage="assess"]');
  await page.click('#btnAssess');
  await stageDone('assess', 300000);
  log(2, 'assess', true, (await text('#assessMeta')).slice(0, 70));
  await shot('02-assess');

  // ---- phase 4b: convert, before sizing so phase 3 has measured evidence
  // rather than an estimate. The order on screen follows the rail; the order
  // that matters is which phase has evidence when it decides.
  await page.click('.stage[data-stage="convert"]');
  await page.fill('#pgPassword', process.env.DBSHIFT_PG_PASSWORD || 'dbshift-local-only');
  await page.click('#btnPgTarget');
  await page.waitForFunction(
    () => /PostgreSQL|registered/.test(document.querySelector('#pgState').textContent),
    null, { timeout: 60000 });
  await page.click('#btnConvert');
  await stageDone('convert', 300000);
  const conv = await text('#convMeta');
  log('4b', 'convert PL/SQL and compile it for real', /[1-9]\d* compiled/.test(conv), conv.slice(0, 70));
  await shot('03-convert-plsql');

  // ---- phase 3: the client picks the engine
  await page.click('.stage[data-stage="target"]');
  await page.click('.pathopt[data-engine="POSTGRESQL"]');
  await page.waitForFunction(
    () => document.querySelector('.pathopt[data-engine="POSTGRESQL"]').getAttribute('aria-pressed') === 'true',
    null, { timeout: 15000 });
  await shot('04-target-path-choice');
  await page.click('#btnSize');
  await stageDone('target', 180000);
  await page.waitForFunction(
    () => /PostgreSQL/.test(document.querySelector('#targetSpec').textContent),
    null, { timeout: 30000 });
  log(3, 'target and sizing, PostgreSQL chosen', /PostgreSQL/.test(await text('#targetSpec')));
  await shot('05-target-sizing');

  // ---- phase 4: remediate
  await page.click('.stage[data-stage="remediate"]');
  await page.click('#btnRemediate');
  await stageDone('remediate', 300000);
  log(4, 'remediate', true, (await text('#remMeta')).slice(0, 70));
  await shot('06-remediate');

  // ---- phase 5: the blocker gate
  await page.click('.stage[data-stage="gate"]');
  await page.click('#btnGate');
  await page.waitForTimeout(4000);
  const gate = await text('#view-gate');
  log(5, 'blocker gate reaches a verdict', /halt|proceed/i.test(gate),
      (gate.match(/halt|proceed/i) || [''])[0]);
  await shot('07-blocker-gate');

  // ---- phase 6: provision, render only. The real stack is already deployed;
  // clicking Provision here renders and checks, it does not deploy again.
  await page.click('.stage[data-stage="provision"]');
  await page.click('#btnProvision');
  await page.waitForFunction(
    () => /dbshift-target-[\w-]+/.test(document.querySelector('#view-provision').innerText),
    null, { timeout: 240000 });
  const prov = await text('#view-provision');
  log(6, 'provision renders a PostgreSQL stack', /postgres/.test(prov));
  await shot('08-provision');

  // ---- phase 7: DMS plan. The rail gates Migrate on a provision record; open
  // the view directly so the demo shows the screen regardless.
  await page.evaluate(() => show('migrate'));
  await page.waitForTimeout(500);
  log(7, 'migrate shows DMS rather than Data Pump', await page.isVisible('#dmsPanel'));
  await page.click('#btnDmsPlan');
  await page.waitForFunction(
    () => document.querySelector('#dmsTiles').style.display !== 'none',
    null, { timeout: 120000 });
  const dms = await text('#dmsPanel');
  log(7, 'the plan names what DMS leaves behind', /Left to finish/.test(dms));
  await shot('09-migrate-dms');

  // ---- phase 8: validate against the live target
  await page.evaluate(() => show('validate'));
  await page.waitForTimeout(500);
  await shot('10-validate-before');
  await page.click('#btnValidate');
  // Validation ends either validated or stopped; wait for the stage to leave
  // busy rather than for a verdict we expect, so an honest mismatch is captured
  // rather than timing out.
  await page.waitForFunction(
    () => !document.querySelector('.stage[data-stage="validate"]').className.includes('busy'),
    null, { timeout: 600000 });
  await page.waitForTimeout(1500);
  const val = await text('#view-validate');
  log(8, 'validate runs the levels against the live target',
      /Level|level/.test(val), (val.match(/validation[^\n]{0,60}/i) || [''])[0]);
  await shot('11-validate-after');

  // ---- phase 9: cutover certificate
  await page.evaluate(() => show('cutover'));
  await page.waitForTimeout(2500);
  const cut = await text('#view-cutover');
  log(9, 'cutover builds a certificate', cut.length > 0);
  log(9, 'and it refuses while the gate blocks',
      /not authorised|blocks cutover|Validate first/i.test(cut));
  await shot('12-cutover-certificate');

  // ---- phase 10: the report
  await page.evaluate(() => show('report'));
  await page.waitForTimeout(300);
  await page.click('#btnReport');
  await page.waitForSelector('#reportResult', { state: 'visible', timeout: 120000 });
  await page.waitForTimeout(1500);
  const meta = await text('#reportMeta');
  log(10, 'report builds', new RegExp(ESTATE).test(meta), meta.slice(0, 70));
  const frame = page.frame({ url: /\/api\/report/ });
  if (frame) {
    await frame.waitForLoadState('load');
    const body = await frame.textContent('body');
    log(10, 'the report knows the target is PostgreSQL', /PostgreSQL/.test(body));
    log(10, 'and that DMS is the data path', /AWS DMS/.test(body));
  }
  await shot('13-report');

  const rail = await page.$$eval('.stage', els => els.map(e => ({ id: e.dataset.stage, cls: e.className })));
  const idle = rail.filter(s => !/done|active/.test(s.cls));
  log('-', 'every stage was reached', idle.length === 0, idle.map(s => s.id).join(', ') || 'none idle');
  log('-', 'no page errors', errors.length === 0, errors.join(' | ').slice(0, 200));

  await browser.close();
  fs.writeFileSync(path.join(OUT, 'steps.json'), JSON.stringify(steps, null, 2));
  const failed = steps.filter(s => !s.ok).length;
  console.log(`\n${steps.length - failed}/${steps.length} steps ok; screenshots in ${OUT}`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error('ABORT', e); process.exit(2); });
