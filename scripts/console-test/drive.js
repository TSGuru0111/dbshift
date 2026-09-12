// Drives the DBShift console end to end through the local phases with the
// same browser engine the Playwright MCP server uses (Edge via playwright-core).
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const BASE = process.env.DBSHIFT_URL || 'http://127.0.0.1:8765';
const PASSWORD = process.env.DBSHIFT_COLLECTOR_PASSWORD || '';
const OUT = path.join(__dirname, 'shots');
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
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  page.on('response', r => { if (r.status() >= 400) errors.push(`${r.status()} ${r.url()}`); });

  const stageDone = (id, timeout) =>
    page.waitForSelector(`.stage[data-stage="${id}"].done`, { timeout });
  const shot = (name) => page.screenshot({ path: path.join(OUT, name + '.png'), fullPage: true });

  await page.goto(BASE, { waitUntil: 'networkidle' });
  // Start from a clean server: a previous run leaves the console connected.
  if (await page.isVisible('#btnDisconnect')) {
    await page.click('#btnDisconnect');
    await page.waitForSelector('#btnDisconnect', { state: 'hidden', timeout: 30000 });
    await page.reload({ waitUntil: 'networkidle' });
  }

  // 1. The rail carries Phase 10, and it is locked before anything has run.
  const rail = await page.$$eval('.stage', els => els.map(e => ({
    id: e.dataset.stage, title: e.getAttribute('title'), cls: e.className })));
  const report = rail.find(s => s.id === 'report');
  log('rail has a Report stage', !!report, report && report.title);
  log('Report is numbered Phase 10', !!report && report.title.startsWith('Phase 10'));
  log('Report is the last stage', rail[rail.length - 1].id === 'report');
  await page.click('.stage[data-stage="report"]');
  log('locked Report stage does not open', !(await page.isVisible('#view-report')));

  // 2. Connect.
  await page.fill('#password', PASSWORD);
  await page.click('#btnConnect');
  await page.waitForSelector('#connStatus.ok', { timeout: 60000 });
  log('connected', true, (await page.textContent('#connText')).trim());
  await shot('01-connect');

  // 3. Discover, assess, size.
  await page.click('.stage[data-stage="discover"]');
  await page.click('#btnDiscover');
  await stageDone('discover', 300000);
  log('discovery done', true);
  await shot('02-discover');

  await page.click('.stage[data-stage="assess"]');
  await page.click('#btnAssess');
  await stageDone('assess', 300000);
  log('assessment done', true);
  await shot('03-assess');

  const reportAfterAssess = await page.getAttribute('.stage[data-stage="report"]', 'class');
  log('Report unlocks after assessment', /active/.test(reportAfterAssess), reportAfterAssess);

  await page.click('.stage[data-stage="target"]');
  await page.click('#btnSize');
  await stageDone('target', 120000);
  log('sizing done', true);
  await shot('04-size');

  // 4. Convert (Phase 4b) against the local PostgreSQL.
  await page.click('.stage[data-stage="convert"]');
  await page.fill('#pgPassword', process.env.DBSHIFT_PG_PASSWORD || 'dbshift-local-only');
  await page.click('#btnPgTarget');
  await page.waitForFunction(() => /PostgreSQL|registered/.test(document.querySelector('#pgState').textContent), null, { timeout: 60000 });
  await page.click('#btnConvert');
  await stageDone('convert', 300000);
  log('conversion done', true, (await page.textContent('#convMeta')).trim());
  await shot('05-convert');

  // 5. Gate.
  await page.click('.stage[data-stage="gate"]');
  await page.click('#btnGate');
  await page.waitForSelector('.stage[data-stage="gate"].done, .stage[data-stage="gate"].active', { timeout: 60000 });
  await page.waitForTimeout(1500);
  await shot('06-gate');

  // 6. The Cutover screen's Next button points at Phase 10. Cutover itself
  // needs AWS, so render its navigation directly rather than reaching it.
  const navText = await page.evaluate(() => {
    const was = current; current = 'cutover'; renderPageNav();
    const t = document.querySelector('#pagenav').textContent; current = was; renderPageNav(); return t;
  });
  log('Next from Cutover names Phase 10', /Phase 10 · Report/.test(navText), navText.trim().replace(/\s+/g, ' '));
  const convMeta = (await page.textContent('#convMeta')).trim();
  log('conversion compiled objects for real', /[1-9]\d* compiled and rolled back/.test(convMeta), convMeta);

  // 7. Report.
  await page.click('.stage[data-stage="report"]');
  log('Report stage opens', await page.isVisible('#view-report'));
  await page.click('#btnReport');
  await page.waitForSelector('#reportResult', { state: 'visible', timeout: 60000 });
  await stageDone('report', 60000);
  const tiles = await page.$$('#reportTiles .tile');
  log('report shows four tiles', tiles.length === 4, String(tiles.length));
  const meta = (await page.textContent('#reportMeta')).trim();
  log('report meta names the estate and run', /DBMIG_\w+ · run [0-9a-f]{8}/.test(meta), meta);
  const frame = page.frame({ url: /\/api\/report/ });
  await frame.waitForLoadState('load');
  const frameText = await frame.textContent('body');
  log('embedded report renders SCT and DMS sections',
      /DMS/.test(frameText) && /conversion/i.test(frameText), `${frameText.length} chars`);
  const railSub = await page.textContent('.stage[data-stage="report"]');
  log('Report stage marked done', /✓/.test(railSub), railSub.trim().replace(/\s+/g, ' '));
  await shot('07-report');

  // 8. Header link now jumps to the stage rather than leaving the console.
  await page.click('.stage[data-stage="connect"]');
  await page.click('#navReport');
  await page.waitForTimeout(300);
  log('header Report link opens the stage in place', await page.isVisible('#view-report'));

  // 9. Reload restores the stage as reachable.
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForSelector('.stage[data-stage="assess"].done', { timeout: 60000 });
  const afterReload = await page.getAttribute('.stage[data-stage="report"]', 'class');
  log('Report reachable after reload', /active|done/.test(afterReload), afterReload);

  // 10. Phone width.
  await page.setViewportSize({ width: 400, height: 800 });
  await page.click('.stage[data-stage="report"]');
  await page.click('#btnReport');
  await page.waitForSelector('#reportResult', { state: 'visible', timeout: 60000 });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
  log('no horizontal scroll at 400px', !overflow);
  await shot('08-report-mobile');

  log('no page errors', errors.length === 0, errors.join(' | ').slice(0, 600));
  await browser.close();
  const failed = results.filter(r => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} checks passed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error('ABORT', e); process.exit(2); });
