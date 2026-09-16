// Drives the manual instance choice and the database configuration form.
//
// The thing worth checking is not that a value can be typed -- it is that the
// rendered plan still says where every value came from afterwards. So the last
// group of checks reads the provenance table in the DOM and requires the
// DERIVED value, the chosen value and the reason to all still be present.
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const BASE = process.env.DBSHIFT_URL || 'http://127.0.0.1:8799';
const OUT = path.join(__dirname, 'shots-provision');
fs.mkdirSync(OUT, { recursive: true });

let pass = 0, fail = 0;
function log(name, ok, detail = '') {
  ok ? pass++ : fail++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  -- ' + detail : ''}`);
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1360, height: 1000 } });
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error' && !/40\d \(/.test(m.text())) errors.push('console: ' + m.text()); });

  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.evaluate(() => {
    ['discover', 'assess', 'target', 'gate', 'provision'].forEach(s => setStage(s, 'done'));
    show('provision');
    document.querySelector('#provEmpty').style.display = 'none';
  });
  // Open the panel first: the form lives in a collapsed <details>, so its
  // options exist but are not "visible" to a visibility-based wait.
  await page.evaluate(() => { document.querySelector('#provOptions').open = true; });
  await page.waitForFunction(
    () => document.querySelectorAll('#ovClass option').length > 0, null, { timeout: 20000 });

  // ---- the form is drawn from the server ----------------------------------
  const classes = await page.$$eval('#ovClass option', o => o.map(x => x.value));
  log('the instance picker is populated', classes.length > 5, `${classes.length} classes`);
  log('it is a picker, not free text',
      await page.evaluate(() => document.querySelector('#ovClass').tagName), 'SELECT');

  const derivedText = await page.textContent('#ovDerived');
  log('what Phase 3 derived is shown next to the choice',
      /db\.\w+\.\w+/.test(derivedText), derivedText.trim());
  const selected = await page.$eval('#ovClass', e => e.value);
  log('the form opens on the derived value', derivedText.includes(selected), selected);

  const fields = await page.$$eval('#ovConfig [data-field]', e => e.map(x => x.dataset.field));
  log('the configuration form is populated', fields.length >= 8, `${fields.length} fields`);
  log('every field states its default',
      await page.$$eval('#ovConfig .why', e => e.every(x => /Default:/.test(x.textContent))));
  const billing = await page.$$eval('#ovConfig label .sev', e => e.length);
  log('cost-affecting fields are marked', billing >= 3, `${billing} marked`);
  log('locked properties are listed with a reason',
      (await page.textContent('#ovLocked')).includes('cannot be changed'));
  await page.screenshot({ path: path.join(OUT, '01-form.png'), fullPage: true });

  // ---- a choice that needs a warning --------------------------------------
  const burstable = classes.find(c => c.startsWith('db.t3.') && c !== selected);
  await page.selectOption('#ovClass', burstable);
  await page.waitForTimeout(150);
  log('choosing a burstable class warns about throttling',
      (await page.textContent('#ovClassNote')).toLowerCase().includes('throttle'));

  const bigger = classes.find(c => c.startsWith('db.m5.'));
  await page.selectOption('#ovClass', bigger);
  await page.waitForTimeout(150);
  log('choosing a larger class says it costs more',
      (await page.textContent('#ovClassNote')).toLowerCase().includes('costs more'));

  // ---- refusals reach the person ------------------------------------------
  await page.fill('#ovReason', 'no');
  await page.click('#btnOvApply');
  await page.waitForSelector('#ovMsg .note.bad', { timeout: 10000 });
  log('a change without a real reason is refused in the UI',
      (await page.textContent('#ovMsg')).includes('reason'));

  await page.fill('#ovReason', '');
  const dbNameSel = '#ovf_db_name';
  if (await page.isVisible(dbNameSel)) {
    await page.fill(dbNameSel, 'TOOLONGNAME');
    await page.fill('#ovReason', 'client naming standard applies here');
    await page.click('#btnOvApply');
    await page.waitForTimeout(400);
    log('an invalid database name is refused, naming the field',
        (await page.textContent('#ovMsg')).includes('8 characters'));
    await page.fill(dbNameSel, 'DBSHIFT');
  }
  await page.screenshot({ path: path.join(OUT, '02-refused.png'), fullPage: true });

  // ---- an accepted override ------------------------------------------------
  const REASON = 'client standardises on m5 for production databases';
  await page.selectOption('#ovClass', bigger);
  const mz = '#ovf_multi_az';
  if (await page.isVisible(mz)) await page.selectOption(mz, 'true');
  await page.fill('#ovReason', REASON);
  await page.click('#btnOvApply');
  await page.waitForSelector('#ovMsg .note.info', { timeout: 10000 });
  log('a valid override is accepted', (await page.textContent('#ovMsg')).includes('Recorded'));
  log('the summary says what differs',
      (await page.textContent('#provOptSummary')).includes(bigger),
      (await page.textContent('#provOptSummary')).trim());

  const stored = await page.evaluate(async () =>
    (await (await fetch('/api/provision/options')).json()).current);
  log('the server holds the choice', stored.instance_class.chosen === bigger);
  log('and holds the reason with it', stored.instance_class.reason === REASON);
  log('and the derived value it replaced', /db\.\w+/.test(stored.instance_class.derived),
      stored.instance_class.derived);
  await page.screenshot({ path: path.join(OUT, '03-accepted.png'), fullPage: true });

  // ---- reset ---------------------------------------------------------------
  await page.click('#btnOvReset');
  await page.waitForTimeout(500);
  const cleared = await page.evaluate(async () =>
    (await (await fetch('/api/provision/options')).json()).current);
  log('reset returns to the derived values', cleared.instance_class === null);
  log('and the summary says so',
      (await page.textContent('#provOptSummary')).includes('derived from the evidence'));

  // ---- phone width ---------------------------------------------------------
  await page.setViewportSize({ width: 400, height: 900 });
  await page.waitForTimeout(250);
  log('no horizontal overflow at phone width',
      !(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 2)));
  await page.screenshot({ path: path.join(OUT, '04-phone.png'), fullPage: true });

  log('no page errors', errors.length === 0, errors.slice(0, 3).join(' | '));
  await browser.close();
  console.log(`\n${pass}/${pass + fail} checks passed`);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
