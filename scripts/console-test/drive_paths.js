// Drives the new Phase 3 target chooser: both paths assessed, the client picks,
// and the sizing that follows reflects the pick.
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const BASE = process.env.DBSHIFT_URL || 'http://127.0.0.1:8765';
const PASSWORD = process.env.DBSHIFT_COLLECTOR_PASSWORD || '';
const OUT = path.join(__dirname, 'shots-paths');
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
  // A 409 on a phase that has not run yet is the gate answering correctly
  // ("not rendered yet", "no sizing run yet"), not a fault.
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

  // Connect and get to an assessment, which unlocks the target stage.
  await page.fill('#password', PASSWORD);
  await page.fill('#schema', 'DBMIG_APP');
  await page.click('#btnConnect');
  await page.waitForSelector('#connStatus.ok', { timeout: 60000 });
  await page.click('.stage[data-stage="discover"]');
  await page.click('#btnDiscover');
  await stageDone('discover', 300000);
  await page.click('.stage[data-stage="assess"]');
  await page.click('#btnAssess');
  await stageDone('assess', 300000);

  // Convert first, so the PostgreSQL path has measured stored-code evidence.
  await page.click('.stage[data-stage="convert"]');
  await page.fill('#pgPassword', process.env.DBSHIFT_PG_PASSWORD || 'dbshift-local-only');
  await page.click('#btnPgTarget');
  await page.waitForFunction(
    () => /PostgreSQL|registered/.test(document.querySelector('#pgState').textContent),
    null, { timeout: 60000 });
  await page.click('#btnConvert');
  await stageDone('convert', 300000);
  log('conversion compiled for real', /[1-9]\d* compiled/.test(await page.textContent('#convMeta')),
      (await page.textContent('#convMeta')).trim());

  // 1. The chooser exists and offers both paths.
  // The chosen path is server state and survives a disconnect by design, so a
  // previous run may have left PostgreSQL selected. Start from Oracle
  // explicitly rather than assuming the default.
  await page.click('.stage[data-stage="target"]');
  await page.click('.pathopt[data-engine="ORACLE"]');
  await page.waitForFunction(
    () => document.querySelector('.pathopt[data-engine="ORACLE"]')
            .getAttribute('aria-pressed') === 'true', null, { timeout: 15000 });
  const opts = await page.$$eval('.pathopt', els => els.map(e => ({
    engine: e.dataset.engine, pressed: e.getAttribute('aria-pressed'), disabled: e.disabled,
    text: e.textContent.replace(/\s+/g, ' ').trim() })));
  log('two paths are offered', opts.length === 2, opts.map(o => o.engine).join(', '));
  log('the selected path is marked', opts.find(o => o.engine === 'ORACLE').pressed === 'true');
  log('neither path is blocked on this estate', opts.every(o => !o.disabled));
  await shot('01-paths-before-sizing');

  // 2. Size on the Oracle path.
  await page.click('#btnSize');
  await stageDone('target', 180000);
  const oracleMeta = (await page.textContent('#sizeMeta')).trim();
  log('Oracle sizing completes', oracleMeta.length > 0, oracleMeta);
  const oracleSpec = await page.textContent('#targetSpec');
  log('Oracle shows an edition and licence',
      /Enterprise Edition|Standard Edition/.test(oracleSpec) && /BYOL|license-included/.test(oracleSpec));
  log('Oracle shows the edition panels', await page.isVisible('#editionPanels'));
  log('the PostgreSQL note is hidden on Oracle', !(await page.isVisible('#pgNote')));
  await shot('02-oracle-sized');

  // 3. Evidence for the PostgreSQL path is real and traceable.
  // A real click, then wait for the POST to land and the panel to redraw --
  // evaluating .click() returns before the handler's fetch resolves.
  await page.click('.pathopt[data-engine="POSTGRESQL"]');
  await page.waitForFunction(
    () => document.querySelector('.pathopt[data-engine="POSTGRESQL"]')
            .getAttribute('aria-pressed') === 'true', null, { timeout: 15000 });
  const ev = await page.evaluate(() => ({
    rows: [...document.querySelectorAll('#pathEvidence details')].map(
      d => d.querySelector('summary').textContent.replace(/\s+/g, ' ').trim()),
    rec: document.querySelector('#pathRec').textContent.replace(/\s+/g, ' ').trim(),
  }));
  log('PostgreSQL evidence lists items', ev.rows.length > 0, `${ev.rows.length} item(s)`);
  log('stored code is measured, not guessed',
      ev.rows.some(r => /stored objects convert automatically/.test(r)),
      ev.rows.find(r => /stored objects/.test(r)) || '');
  log('DMS is named as the data path', ev.rows.some(r => /DMS/.test(r)));
  log('a recommendation with reasoning is shown', /recommend|No recommendation/i.test(ev.rec),
      ev.rec.slice(0, 120));

  // 4. Changing the path invalidates the sizing on screen.
  const afterSwitch = (await page.textContent('#sizeMeta')).trim();
  log('switching path marks the sizing stale', /re-run|path changed/.test(afterSwitch), afterSwitch);
  const railSub = await page.textContent('.stage[data-stage="target"]');
  log('the rail says re-run too', /re-run/.test(railSub), railSub.replace(/\s+/g, ' ').trim());
  await shot('03-postgres-selected');

  // 5. The server holds the choice.
  const st = await (await page.request.get(BASE + '/api/state')).json();
  log('server records the PostgreSQL choice', st.engine === 'POSTGRESQL', st.engine_label);

  // 6. Size on the PostgreSQL path.
  await page.click('#btnSize');
  await stageDone('target', 180000);
  // The rail turns done before renderSizing has repainted the spec panel, so
  // wait for the panel itself to describe the engine that just ran.
  await page.waitForFunction(
    () => /PostgreSQL/.test(document.querySelector('#targetSpec').textContent),
    null, { timeout: 30000 });
  const pgSpec = await page.textContent('#targetSpec');
  log('PostgreSQL names the engine, not an edition',
      /PostgreSQL/.test(pgSpec) && !/Enterprise Edition|Standard Edition/.test(pgSpec));
  log('no licence badge on PostgreSQL', !/BYOL|license-included/.test(pgSpec));
  log('licences read none', /none/.test(pgSpec), pgSpec.replace(/\s+/g, ' ').slice(0, 200));
  log('edition panels are hidden', !(await page.isVisible('#editionPanels')));
  log('the PostgreSQL explanation is shown', await page.isVisible('#pgNote'));
  await shot('04-postgres-sized');

  // 7. The choice survives a reload.
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForSelector('.stage[data-stage="assess"].done', { timeout: 60000 });
  await page.click('.stage[data-stage="target"]');
  await page.waitForTimeout(800);
  const pressed = await page.getAttribute('.pathopt[data-engine="POSTGRESQL"]', 'aria-pressed');
  log('the chosen path survives a reload', pressed === 'true', String(pressed));

  // 8. Phone width.
  await page.setViewportSize({ width: 400, height: 800 });
  await page.waitForTimeout(300);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
  log('no horizontal scroll at 400px', !overflow);
  await shot('05-paths-mobile');

  log('no page errors', errors.length === 0, errors.join(' | ').slice(0, 400));
  await browser.close();
  const failed = results.filter(r => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} checks passed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error('ABORT', e); process.exit(2); });
