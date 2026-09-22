// The demo you run in front of people: a visible browser, paced so a room can
// follow, with a caption naming the phase and what to look at.
//
// drive_demo.js is the headless proof (18 assertions, screenshots). This one is
// the same walkthrough slowed down and narrated. Nothing here spends money:
// Provision renders, Migrate plans, Cutover builds a certificate. The two writes
// that bill -- deploy and cutover --execute -- are never clicked.
//
//   node drive_live_demo.js            full run
//   node drive_live_demo.js --fast     half the pauses
//   DEMO_PAUSE=6000 node ...           set the beat yourself
const { chromium } = require('playwright-core');

const BASE = process.env.DBSHIFT_URL || 'http://127.0.0.1:8765';
const PASSWORD = process.env.DBSHIFT_COLLECTOR_PASSWORD || '';
const ESTATE = process.env.DBSHIFT_DEMO_ESTATE || 'DBMIG_TELCO';
const FAST = process.argv.includes('--fast');
// Long enough for a sentence out loud, short enough that nobody drifts.
const BEAT = Number(process.env.DEMO_PAUSE || (FAST ? 1800 : 3600));

(async () => {
  const browser = await chromium.launch({
    channel: 'msedge',
    headless: false,
    args: ['--start-maximized'],
  });
  const page = await browser.newPage({ viewport: null });

  // The caption lives in the page so it is visible on a projector and in any
  // screen recording. It is removed before each navigation and redrawn, so it
  // never survives into a screenshot of the next phase.
  async function say(phase, title, detail = '') {
    await page.evaluate(({ phase, title, detail }) => {
      let el = document.getElementById('__demo_caption');
      if (!el) {
        el = document.createElement('div');
        el.id = '__demo_caption';
        el.style.cssText = [
          'position:fixed', 'left:0', 'right:0', 'bottom:0', 'z-index:2147483647',
          'background:#0B1F24', 'color:#fff', 'padding:14px 22px',
          'font:600 17px/1.45 Archivo,system-ui,sans-serif',
          'box-shadow:0 -2px 18px rgba(0,0,0,.28)',
          'display:flex', 'gap:16px', 'align-items:baseline',
        ].join(';');
        document.body.appendChild(el);
      }
      el.innerHTML =
        `<span style="background:#15C5A8;color:#04252B;border-radius:3px;
           padding:3px 10px;font-size:14px;white-space:nowrap">${phase}</span>
         <span>${title}</span>
         <span style="font-weight:400;opacity:.82;font-size:15px">${detail}</span>`;
    }, { phase, title, detail });
    console.log(`${String(phase).padEnd(9)} ${title}${detail ? '  — ' + detail : ''}`);
  }

  const beat = (n = 1) => page.waitForTimeout(BEAT * n);
  const stageDone = (id, t) => page.waitForSelector(`.stage[data-stage="${id}"].done`, { timeout: t });
  const text = sel => page.textContent(sel).then(t => (t || '').trim());

  await page.goto(BASE, { waitUntil: 'networkidle' });

  // A previous run leaves the console connected; start from a clean rail so the
  // room sees every phase light up rather than a half-finished board.
  if (await page.isVisible('#btnDisconnect')) {
    await say('setup', 'Starting from a clean board', 'clearing the previous run');
    await page.click('#btnDisconnect');
    await page.waitForSelector('#btnDisconnect', { state: 'hidden', timeout: 30000 });
    await page.reload({ waitUntil: 'networkidle' });
    await beat();
  }

  await say('DBShift', 'Oracle → AWS migration console',
            'eleven phases, left to right. Nothing is applied without a person.');
  await beat(1.5);

  // ---- connect
  await say('Connect', 'Read-only login to the Oracle source',
            `${ESTATE} on localhost:1521/XEPDB1`);
  await page.fill('#password', PASSWORD);
  await page.fill('#schema', ESTATE);
  await beat(0.6);
  await page.click('#btnConnect');
  await page.waitForSelector('#connStatus.ok', { timeout: 60000 });
  await say('Connect', 'Connected', await text('#connText'));
  await beat();

  // ---- phase 1
  await page.click('.stage[data-stage="discover"]');
  await say('Phase 1', 'Discover — read the estate',
            'catalogue only: no data is read, nothing is written');
  await beat(0.8);
  await page.click('#btnDiscover');
  await stageDone('discover', 300000);
  await say('Phase 1', 'Discovered', await text('#discMeta'));
  await beat();

  // ---- phase 2
  await page.click('.stage[data-stage="assess"]');
  await say('Phase 2', 'Assess — deterministic rules over the facts',
            'every finding cites the evidence it came from');
  await beat(0.8);
  await page.click('#btnAssess');
  await stageDone('assess', 300000);
  await say('Phase 2', 'Assessed', await text('#assessMeta'));
  await beat(1.4);

  // ---- phase 4b, before sizing: the effort figure must be measured, not guessed
  await page.click('.stage[data-stage="convert"]');
  await say('Phase 4b', 'Convert PL/SQL to PL/pgSQL',
            'each object is compiled on a real PostgreSQL, then rolled back');
  await beat();
  await page.fill('#pgPassword', process.env.DBSHIFT_PG_PASSWORD || 'dbshift-local-only');
  await page.click('#btnPgTarget');
  await page.waitForFunction(
    () => /PostgreSQL|registered/.test(document.querySelector('#pgState').textContent),
    null, { timeout: 60000 });
  await page.click('#btnConvert');
  await stageDone('convert', 300000);
  await say('Phase 4b', 'Compiled for real — nothing applied', await text('#convMeta'));
  await beat(1.6);

  // ---- phase 3
  await page.click('.stage[data-stage="target"]');
  await say('Phase 3', 'Target — the client chooses the path',
            'Oracle stays Oracle, or Oracle leaves for PostgreSQL and the licence ends');
  await beat(1.2);
  await page.click('.pathopt[data-engine="POSTGRESQL"]');
  await page.waitForFunction(
    () => document.querySelector('.pathopt[data-engine="POSTGRESQL"]').getAttribute('aria-pressed') === 'true',
    null, { timeout: 15000 });
  await say('Phase 3', 'PostgreSQL chosen', 'the effort figure comes from Phase 4b, measured not estimated');
  await beat();
  await page.click('#btnSize');
  await stageDone('target', 180000);
  await page.waitForFunction(
    () => /PostgreSQL/.test(document.querySelector('#targetSpec').textContent),
    null, { timeout: 30000 });
  await say('Phase 3', 'Sized for PostgreSQL', 'a capacity floor, and it says so');
  await beat(1.4);

  // ---- phase 4
  await page.click('.stage[data-stage="remediate"]');
  await say('Phase 4', 'Remediate — a fix for every finding',
            'templates where there is one right answer, the model where judgement is needed');
  await beat();
  await page.click('#btnRemediate');
  await stageDone('remediate', 300000);
  await say('Phase 4', 'Planned — nothing applied', await text('#remMeta'));
  await beat(0.8);
  await page.evaluate(() => {
    const el = document.querySelector('#genStatusText');
    if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  });
  await say('Phase 4', 'The model proposed; the gates decided',
            await text('#genStatusText').catch(() => ''));
  await beat(1.8);

  // ---- phase 5
  await page.click('.stage[data-stage="gate"]');
  await say('Phase 5', 'Blocker gate — the last deterministic step',
            'before anything costs money or touches a target');
  await beat();
  await page.click('#btnGate');
  await page.waitForTimeout(4500);
  const gate = (await text('#view-gate')).match(/halt|proceed/i);
  await say('Phase 5', `Verdict: ${gate ? gate[0] : 'reached'}`,
            'per phase, not one blanket refusal — and a waiver needs a named person');
  await beat(1.8);

  // ---- phase 6
  await page.click('.stage[data-stage="provision"]');
  await say('Phase 6', 'Provision — render the target and check it',
            'renders and verifies only; the deploy is a separate, explicit yes');
  await beat();
  await page.click('#btnProvision');
  await page.waitForFunction(
    () => /dbshift-target-[\w-]+/.test(document.querySelector('#view-provision').innerText),
    null, { timeout: 240000 });
  await say('Phase 6', 'Rendered, with provenance',
            'every value says which phase decided it');
  await beat(1.8);

  // ---- phase 7
  await page.evaluate(() => show('migrate'));
  await beat(0.5);
  await say('Phase 7', 'Migrate — AWS DMS, not Data Pump',
            'Data Pump writes an Oracle-only format, so the PostgreSQL path must use DMS');
  await beat();
  await page.click('#btnDmsPlan');
  await page.waitForFunction(
    () => document.querySelector('#dmsTiles').style.display !== 'none',
    null, { timeout: 120000 });
  await say('Phase 7', 'Planned — and it names what DMS leaves behind',
            'sequences, views, stored code: routed to a rule, the model, or a person');
  await beat(1.8);

  // ---- phase 8
  await page.evaluate(() => show('validate'));
  await beat(0.5);
  await say('Phase 8', 'Validate — five levels against the live target',
            'every statement is a SELECT; nothing is written to either database');
  await beat(1.2);
  await page.click('#btnValidate');
  await page.waitForFunction(
    () => !document.querySelector('.stage[data-stage="validate"]').className.includes('busy'),
    null, { timeout: 600000 });
  await beat(0.5);
  await say('Phase 8', 'It reports a mismatch — and that is correct',
            'the target is empty: DMS cannot reach an on-premises source from AWS');
  await beat(2);

  // ---- phase 9
  await page.evaluate(() => show('cutover'));
  await beat(1.2);
  await say('Phase 9', 'Cutover — nine requirements, each with a remedy',
            'it refuses, and every unmet line says exactly what would clear it');
  await beat(2.2);

  // ---- phase 10
  await page.evaluate(() => show('report'));
  await beat(0.5);
  await say('Phase 10', 'Report — built from this run only',
            'nothing in it is written by hand');
  await beat(0.8);
  await page.click('#btnReport');
  await page.waitForSelector('#reportResult', { state: 'visible', timeout: 120000 });
  await say('Phase 10', 'Built', await text('#reportMeta'));
  await beat(2);

  await say('Done', 'Ten phases, end to end',
            'AI proposes · deterministic rules decide · a person approves anything irreversible');

  console.log('\nThe browser stays open. Close the window when you are finished.');
  // Deliberately not closing: the room is still looking at it, and questions
  // usually come after the last phase rather than during it.
  await new Promise(() => {});
})().catch(e => { console.error('ABORT', e); process.exit(2); });
