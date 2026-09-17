// Drives the two changes a client asked for:
//   1. the assessment as a table, with a download
//   2. the discovery counts reporting what a client would count
//
// Both are UI claims that only a browser can settle: the CSV is checked by
// downloading it for real and parsing what arrives, not by reading the handler.
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const BASE = process.env.DBSHIFT_URL || 'http://127.0.0.1:8799';
const PASSWORD = process.env.DBSHIFT_COLLECTOR_PASSWORD || '';
const OUT = path.join(__dirname, 'shots-counts');
fs.mkdirSync(OUT, { recursive: true });

let pass = 0, fail = 0;
function log(name, ok, detail = '') {
  ok ? pass++ : fail++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  -- ' + detail : ''}`);
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1360, height: 900 }, acceptDownloads: true });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error' && !/40\d \(/.test(m.text())) errors.push('console: ' + m.text()); });
  page.on('response', r => {
    if (r.status() >= 400 && !(r.status() === 409 && /\/api\//.test(r.url())))
      errors.push(`${r.status()} ${r.url()}`);
  });
  const stageDone = (id, t) => page.waitForSelector(`.stage[data-stage="${id}"].done`, { timeout: t });

  await page.goto(BASE, { waitUntil: 'networkidle' });

  // Two ways in. With a collector password, drive the real thing. Without one,
  // run against a server seeded from the newest run on disk -- which exercises
  // the same render paths against the same records, and is the only way to
  // check these screens on a machine that cannot reach Oracle.
  const SEEDED = !PASSWORD;
  if (!SEEDED) {
    if (await page.isVisible('#btnDisconnect')) {
      await page.click('#btnDisconnect');
      await page.waitForSelector('#btnDisconnect', { state: 'hidden', timeout: 30000 });
      await page.reload({ waitUntil: 'networkidle' });
    }
    await page.fill('#password', PASSWORD);
    await page.click('#btnConnect');
    await page.waitForSelector('#connStatus.ok', { timeout: 60000 });
  }
  console.log(SEEDED ? '    mode: seeded (no collector password)' : '    mode: live Oracle');

  // ---- Phase 1: the counts -------------------------------------------------
  if (SEEDED) {
    await page.evaluate(() => {
      setStage('discover', 'done'); show('discover');
      document.querySelector('#discEmpty').style.display = 'none';
    });
    await page.evaluate(() => renderDiscovery());
    await page.waitForSelector('#discTiles .tile', { timeout: 30000 });
  } else {
    await page.click('.stage[data-stage="discover"]');
    await page.click('#btnDiscover');
    await stageDone('discover', 300000);
  }

  const tiles = await page.$$eval('#discTiles .tile', els => els.map(e => ({
    k: e.querySelector('.k').textContent.trim(),
    v: e.querySelector('.v').textContent.trim(),
    h: e.querySelector('.h').textContent.trim(),
  })));
  const tables = tiles.find(t => t.k === 'Tables');
  const objects = tiles.find(t => t.k === 'Objects');
  console.log('    tiles:', JSON.stringify({ tables, objects }));

  // The raw dataset row count is still shown in the dataset list; the tile must
  // now differ from it, and must say how many it set aside.
  const rawTables = await page.$$eval('#discAcc details', els => {
    const d = els.find(e => e.querySelector('.title').textContent.trim().startsWith('tables'));
    return d ? d.querySelector('.count').textContent.trim() : null;
  });
  log('Tables tile is the user count, not the raw count',
      !!tables && !!rawTables && tables.v !== rawTables, `tile ${tables && tables.v} vs dataset ${rawTables}`);
  log('the tile says what it excluded', !!tables && /excluded|every table/.test(tables.h), tables && tables.h);
  log('Objects tile does the same', !!objects && /excluded|every object/.test(objects.h), objects && objects.h);
  await page.screenshot({ path: path.join(OUT, '01-counts.png'), fullPage: true });

  // The console must agree with the API, which the self-test pins to Phase 2.
  const api = await page.evaluate(async () => (await (await fetch('/api/discovery')).json()).summary);
  const apiTables = api.find(t => t.label === 'Tables');
  log('the tile matches /api/discovery', String(apiTables.value) === tables.v.replace(/,/g, ''),
      `api ${apiTables.value}`);

  // ---- Phase 2: the table and the download ---------------------------------
  if (SEEDED) {
    // Hide the empty panel exactly as the Run button's handler does -- otherwise
    // the seeded path leaves "Run discovery first" under a rendered table, which
    // is an artifact of this harness and not what a client sees.
    await page.evaluate(() => {
      setStage('assess', 'done'); show('assess');
      document.querySelector('#assessEmpty').style.display = 'none';
    });
    await page.evaluate(() => renderAssessment());
    await page.waitForSelector('#issueTable table', { timeout: 30000 });
  } else {
    await page.click('.stage[data-stage="assess"]');
    await page.click('#btnAssess');
    await stageDone('assess', 300000);
  }

  log('the table is the default view', await page.isVisible('#issueTable'));
  log('the accordion is not shown by default', !(await page.isVisible('#issueAcc')));

  const headers = await page.$$eval('#issueTable th', els => els.map(e => e.textContent.trim().replace(/[↑↓]/g, '').trim()));
  log('the table has the scan columns', headers.join(',') === 'Severity,Rule,Finding,Category,Objects,Fix', headers.join(','));

  const firstSev = await page.$$eval('#issueTable tbody tr[data-idx] .sev', e => e.map(x => x.textContent.trim()));
  log('sorted by severity, worst first', firstSev[0] === 'CRITICAL', firstSev.slice(0, 4).join(' '));
  const rowCount = firstSev.length;
  log('every issue is a row', rowCount > 0, `${rowCount} rows`);
  await page.screenshot({ path: path.join(OUT, '02-table.png'), fullPage: true });

  // Sorting: clicking Rule reorders, clicking again reverses.
  await page.click('#issueTable th[data-sort="rule_id"]');
  const asc = await page.$$eval('#issueTable tbody tr[data-idx] td:nth-child(2)', e => e.map(x => x.textContent.trim()));
  await page.click('#issueTable th[data-sort="rule_id"]');
  const desc = await page.$$eval('#issueTable tbody tr[data-idx] td:nth-child(2)', e => e.map(x => x.textContent.trim()));
  log('a heading sorts the table', asc[0] !== desc[0], `${asc[0]} then ${desc[0]}`);
  log('sorting keeps every row', asc.length === rowCount && desc.length === rowCount);

  // A row opens its detail in place.
  await page.click('#issueTable tbody tr[data-idx="0"]');
  const openDetail = await page.isVisible('#issueTable tr.rowdetail[data-for="0"]');
  log('a row opens its own detail', openDetail);
  await page.screenshot({ path: path.join(OUT, '03-row-open.png'), fullPage: true });

  // The severity filter still drives the table.
  await page.click('#sevChips .chip[data-sev="CRITICAL"]');
  const filtered = await page.$$eval('#issueTable tbody tr[data-idx] .sev', e => e.map(x => x.textContent.trim()));
  log('the severity filter applies to the table',
      filtered.length > 0 && filtered.every(s => s === 'CRITICAL'), `${filtered.length} critical`);
  await page.click('#sevChips .chip[data-sev="CRITICAL"]');

  // Detail view still reachable.
  await page.click('#btnViewDetail');
  log('the detail view still opens', await page.isVisible('#issueAcc'));
  log('and hides the table', !(await page.isVisible('#issueTable')));
  await page.screenshot({ path: path.join(OUT, '04-detail.png'), fullPage: true });
  await page.click('#btnViewTable');

  // ---- the download, for real ---------------------------------------------
  const dl = await Promise.all([page.waitForEvent('download'), page.click('#btnCsv')]);
  const file = path.join(OUT, 'assessment.csv');
  await dl[0].saveAs(file);
  log('the CSV downloads', fs.existsSync(file), dl[0].suggestedFilename());
  const csv = fs.readFileSync(file, 'utf8');
  log('it is named for the run', /^dbshift-assessment-[0-9a-f]{8}\.csv$/.test(dl[0].suggestedFilename()));
  log('it opens cleanly in Excel (BOM)', csv.charCodeAt(0) === 0xFEFF);
  const lines = csv.replace(/^﻿/, '').trim().split('\r\n');
  log('it has a header and a row per finding', lines.length > 1, `${lines.length - 1} rows`);
  log('the header names the columns',
      /^Rule,Severity,Applies to this migration/.test(lines[0]), lines[0].slice(0, 60));
  log('a not-applicable finding keeps its reason column', /Why not applicable/.test(lines[0]));

  // PDF and Excel -- the SCT-shaped artefacts a client circulates.
  const xdl = await Promise.all([page.waitForEvent('download'), page.click('#btnXlsx')]);
  const xfile = path.join(OUT, 'assessment.xlsx');
  await xdl[0].saveAs(xfile);
  const xbuf = fs.readFileSync(xfile);
  log('the Excel workbook downloads', fs.existsSync(xfile), xdl[0].suggestedFilename());
  log('it is a real .xlsx (zip container)', xbuf[0] === 0x50 && xbuf[1] === 0x4B);
  log('and is named for the estate and run',
      /^dbshift-assessment-.+-[0-9a-f]{8}\.xlsx$/.test(xdl[0].suggestedFilename()));

  const pdl = await Promise.all([page.waitForEvent('download'), page.click('#btnPdf')]);
  const pfile = path.join(OUT, 'assessment.pdf');
  await pdl[0].saveAs(pfile);
  const pbuf = fs.readFileSync(pfile);
  log('the PDF downloads', fs.existsSync(pfile), pdl[0].suggestedFilename());
  log('it is a real PDF', pbuf.slice(0, 5).toString() === '%PDF-');
  log('the screen says what these files are',
      /not\s+generated\s+by\s+AWS\s+SCT/i.test(await page.textContent('#view-assess')));

  const jdl = await Promise.all([page.waitForEvent('download'), page.click('#btnJson')]);
  const jfile = path.join(OUT, 'assessment.json');
  await jdl[0].saveAs(jfile);
  const rec = JSON.parse(fs.readFileSync(jfile, 'utf8'));
  log('the JSON record downloads too', Array.isArray(rec.findings), `${(rec.findings || []).length} findings`);
  log('CSV rows match the record', lines.length - 1 === rec.findings.length);

  // ---- phone width ---------------------------------------------------------
  await page.setViewportSize({ width: 400, height: 900 });
  await page.waitForTimeout(300);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 2);
  log('no horizontal overflow at phone width', !overflow);
  log('the table scrolls in its own box at phone width',
      await page.evaluate(() => { const w = document.querySelector('#issueTable');
        return !!w && getComputedStyle(w).overflowX === 'auto'; }));
  await page.screenshot({ path: path.join(OUT, '05-phone.png'), fullPage: true });

  log('no page errors', errors.length === 0, errors.slice(0, 3).join(' | '));
  await browser.close();
  console.log(`\n${pass}/${pass + fail} checks passed`);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
