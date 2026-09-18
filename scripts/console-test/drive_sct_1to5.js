// Drives the console through Phases 1-5 on the AWS SCT path, in one run.
//
// This is the drive that matters for a demo: Connect -> Discover -> Assess
// (AWS SCT) -> Remediate (AI drafts, gates decide) -> Blocker gate (split by
// where the fix belongs). Everything a client sees before anything bills.
//
// What only a browser can settle here:
//
//   1. **The phases chain.** An SCT run must arm Phases 4 and 5. Previously
//      only the 50-rule run advanced the rail, which would leave the SCT
//      screens complete and every later stage locked.
//   2. **A HALT leaves Phase 6 shut.** That is the entire purpose of the gate,
//      and it is a rendering fact as much as a logic one.
//   3. **The gate separates source from target work.** A single "halted" list
//      sends people to fix the wrong thing; whether the split actually renders
//      is only visible here.
//   4. **A rejected fix is shown as a gate working, not as a crash.** The live
//      model wrote Oracle syntax for a PostgreSQL target and the dry run caught
//      it -- a client must see that as the system doing its job.
//   5. **Reload keeps it.** A refresh must not leave complete screens with dead
//      buttons.
//
// Run: node drive_sct_1to5.js
//   DBSHIFT_URL, DBSHIFT_COLLECTOR_PASSWORD
//   DBSHIFT_PG_DSN/_USER/_PASSWORD  -- for the target-side dry run to be real
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const BASE = process.env.DBSHIFT_URL || 'http://127.0.0.1:8765';
const PASSWORD = process.env.DBSHIFT_COLLECTOR_PASSWORD || '';
const OUT = path.join(__dirname, 'shots-sct-1to5');
fs.mkdirSync(OUT, { recursive: true });

let pass = 0, fail = 0;
function log(name, ok, detail = '') {
  ok ? pass++ : fail++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  -- ' + detail : ''}`);
}

// **Measured, not guessed.** A cached SCT run returns in seconds, but a fresh
// one against DBMIG_APP took over 25 minutes in this console -- the CLI's ~5
// minutes is CreateReport alone, and a console run adds JVM start, metadata
// load for every schema, and project save. 50 minutes leaves real headroom
// without hanging a CI run forever; the drive reports a timeout as a timeout
// rather than letting it read as an SCT failure.
const SCT_TIMEOUT = 3000000;
const REM_TIMEOUT = 900000;    // a live model draft is seconds per item

(async () => {
  if (!PASSWORD) {
    console.log('DBSHIFT_COLLECTOR_PASSWORD is not set -- this drive needs the source.');
    process.exit(2);
  }

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const ctx = await browser.newContext({
    viewport: { width: 1360, height: 1000 }, acceptDownloads: true,
  });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => {
    if (m.type() === 'error' && !/40\d \(/.test(m.text())) errors.push('console: ' + m.text());
  });
  const stageClass = id =>
    page.getAttribute(`.stage[data-stage="${id}"]`, 'class');

  await page.goto(BASE, { waitUntil: 'domcontentloaded' });

  // ---- Phase 1a: Connect --------------------------------------------------
  await page.fill('#password', PASSWORD);
  await page.click('#btnConnect');
  const connected = await page.waitForFunction(
    () => $('#connStatus').className.includes('ok'), { timeout: 90000 }
  ).then(() => true).catch(() => false);
  log('Connect: the preflight passed', connected);
  if (!connected) { await browser.close(); process.exit(1); }
  await page.waitForTimeout(1300);   // the auto-advance to Discover

  // ---- Phase 1b: Discover -------------------------------------------------
  await page.evaluate(() => show('discover'));
  await page.click('#btnDiscover');
  const discovered = await page.waitForSelector('.stage[data-stage="discover"].done',
    { timeout: 600000 }).then(() => true).catch(() => false);
  log('Discover: the collector finished', discovered);
  if (discovered) {
    // The stage is marked done before renderDiscovery() has painted, so wait
    // for the tiles themselves rather than asserting on the stage class. The
    // first version read 8 tiles on one run and 0 on the next -- a flake, not
    // a feature, and exactly the kind a demo would hit in front of a client.
    const tiles = await page.waitForSelector('#discTiles .tile', { timeout: 60000 })
      .then(async () => page.$$eval('#discTiles .tile', els => els.length))
      .catch(() => 0);
    log('Discover: the counts rendered', tiles > 0, `${tiles} tiles`);
  }

  // ---- Phase 2: Assess, with AWS SCT -------------------------------------
  await page.evaluate(() => show('assess'));
  await page.locator('#btnSct').waitFor({ state: 'visible', timeout: 15000 });
  log('Assess: the screen leads with AWS SCT',
      !(await page.locator('#rulesRunRow').isVisible()));
  log('Assess: the rules-engine result panel is hidden',
      !(await page.locator('#assessResult').isVisible()));
  log('Assess: the run button is armed', !(await page.locator('#btnSct').isDisabled()));

  await page.click('#btnSct');
  // Polled from the test side, for the same reason as Remediate below: the page
  // holds an open EventSource and a page-side polling predicate proved
  // unreliable against one.
  let assessed = false;
  for (const started = Date.now(); Date.now() - started < SCT_TIMEOUT; ) {
    const st = await page.evaluate(() => ({
      rendered: $('#sctResult').style.display !== 'none',
      failed: !!$('#sctLive').querySelector('.note.bad'),
    }));
    if (st.rendered || st.failed) { assessed = true; break; }
    await page.waitForTimeout(3000);
  }
  log('Assess: AWS SCT produced a rendered outcome', assessed);

  if (!assessed || await page.locator('#sctLive .note.bad').count()) {
    const why = await page.locator('#sctLive .note.bad').count()
      ? (await page.locator('#sctLive .note.bad').innerText()).slice(0, 200)
      : 'timed out';
    log('Assess: SCT succeeded', false, why);
    await page.screenshot({ path: path.join(OUT, 'assess-failed.png'), fullPage: true });
    await browser.close();
    console.log(`\n${pass}/${pass + fail} passed, ${fail} FAILED`);
    process.exit(1);
  }

  const sct = await page.evaluate(() => SCT);
  log('Assess: action items parsed', (sct.action_item_count || 0) > 0,
      `${sct.action_item_count} items, ${sct.occurrence_count} occurrences`);
  log('Assess: no column left unmapped',
      (sct.unmapped_columns || []).length === 0,
      (sct.unmapped_columns || []).join(',') || 'none');
  log('Assess: the work is segregated',
      (sct.segregation.groups || []).length === 4);
  log('Assess: the rail advanced on the SCT run',
      (await stageClass('assess')).includes('done'));
  await page.screenshot({ path: path.join(OUT, '01-assess.png'), fullPage: true });

  // SCT's own files, downloaded for real.
  for (const [id, label, magic] of [['#btnSctPdf', 'PDF', '%PDF'],
                                    ['#btnSctXlsx', 'Excel', 'PK']]) {
    const dl = await Promise.all([
      page.waitForEvent('download', { timeout: 60000 }), page.click(id),
    ]).then(([d]) => d).catch(() => null);
    if (!dl) { log(`Assess: ${label} downloads`, false, 'no download event'); continue; }
    const name = dl.suggestedFilename();
    const dest = path.join(OUT, name);
    await dl.saveAs(dest);
    const head = fs.readFileSync(dest).slice(0, magic.length).toString('latin1');
    const unsafe = /[ ()<>@,;:\\"/\[\]?={}]/.test(name);
    log(`Assess: ${label} downloads as a real file`, head === magic && !unsafe,
        `${name}${unsafe ? ' -- UNSAFE NAME' : ''}`);
  }

  // ---- Phase 4: Remediate, AI drafts and the gates decide ----------------
  await page.evaluate(() => show('remediate'));
  await page.locator('#btnSctRemediate').waitFor({ state: 'visible', timeout: 15000 });
  log('Remediate: armed by the SCT run',
      !(await page.locator('#btnSctRemediate').isDisabled()));

  await page.check('#sctRemApprove');
  await page.fill('#sctRemApprover', 'drive@example.com');
  await page.click('#btnSctRemediate');
  // **Poll from the test, not with waitForFunction.** The page holds an open
  // EventSource for the whole run, and a page-side polling predicate proved
  // unreliable against it -- this same wait reported a timeout while a probe
  // watching the identical expression from the test side saw it render at
  // t+58s. Polling from here also lets a failure report how far it got, which
  // "timed out" alone never did.
  const remState = async () => page.evaluate(() => ({
    rendered: $('#sctRemResult').style.display !== 'none',
    failed: !!$('#sctRemLive').querySelector('.note.bad'),
    ticks: $('#sctRemTicker').children.length,
  }));
  let remediated = false, remTicks = 0;
  for (const started = Date.now(); Date.now() - started < REM_TIMEOUT; ) {
    const st = await remState();
    remTicks = st.ticks;
    if (st.rendered || st.failed) { remediated = true; break; }
    await page.waitForTimeout(2000);
  }
  log('Remediate: produced a rendered outcome', remediated,
      remediated ? '' : `gave up with ${remTicks} items ticked`);

  if (remediated && !(await page.locator('#sctRemLive .note.bad').count())) {
    const rem = await page.evaluate(() => SCTREM);
    const t = rem.totals || {};
    log('Remediate: every item is planned', t.items === sct.action_item_count,
        `${t.items} of ${sct.action_item_count}`);
    log('Remediate: nothing was applied', rem.applied === false);
    log('Remediate: both policies are named in the record',
        !!(rem.policies && rem.policies.oracle && rem.policies.postgresql));
    log('Remediate: the Oracle policy is recorded as unchanged',
        /unchanged/.test(rem.policies.oracle || ''));

    // Every entry must carry its route, and no source item may be automatic.
    const entries = rem.entries || [];
    log('Remediate: every entry carries where and who',
        entries.every(e => e.where && e.who));
    log('Remediate: no source fix is automatic',
        entries.filter(e => e.where === 'source').every(e => e.who !== 'auto'));
    log('Remediate: a decision is never drafted',
        entries.filter(e => e.where === 'decision').every(e => !e.sql));

    const drafted = entries.filter(e => e.sql);
    const rejected = entries.filter(e => e.status === 'REJECTED');
    console.log(`      drafted ${drafted.length}, rejected ${rejected.length}, ` +
                `need a person ${t.needs_a_person}`);
    // A rejection is the gates working. The live model wrote Oracle DDL for a
    // PostgreSQL target once already, and the dry run caught it.
    for (const r of rejected) {
      const failedGate = (r.gates || []).find(g => g.status === 'fail');
      log(`Remediate: ${r.issue_code} rejected with a named gate`, !!failedGate,
          failedGate ? `${failedGate.gate}: ${failedGate.detail.slice(0, 70)}` : 'none');
    }
    log('Remediate: the groups rendered',
        await page.$$eval('#sctRemGroups .eyebrow', e => e.length) > 0);
  } else if (remediated) {
    log('Remediate: succeeded', false,
        (await page.locator('#sctRemLive .note.bad').innerText()).slice(0, 160));
  }
  await page.screenshot({ path: path.join(OUT, '02-remediate.png'), fullPage: true });

  // ---- Phase 5: the gate, split by where the fix belongs -----------------
  await page.evaluate(() => show('gate'));
  await page.locator('#btnSctGate').waitFor({ state: 'visible', timeout: 15000 });
  log('Gate: armed by the SCT run', !(await page.locator('#btnSctGate').isDisabled()));

  const provisionBefore = await stageClass('provision');
  await page.click('#btnSctGate');
  let gated = false;
  for (const started = Date.now(); Date.now() - started < 120000; ) {
    if (await page.evaluate(() => $('#sctGateResult').style.display !== 'none')) {
      gated = true; break;
    }
    await page.waitForTimeout(1000);
  }
  log('Gate: produced a verdict', gated);

  if (gated) {
    const gd = await page.evaluate(() => SCTGATE);
    log('Gate: the verdict is one of the three',
        ['HALT', 'PROCEED', 'PROCEED_WITH_WAIVERS'].includes(gd.verdict), gd.verdict);
    log('Gate: the findings are SCT’s', gd.source_of_findings === 'aws-sct');
    log('Gate: the work is split four ways', (gd.groups || []).length === 4);
    log('Gate: the source group is separate from the target group',
        gd.groups[0].where === 'source' && gd.groups[1].where === 'target');

    // CDC readiness must never claim clear without evidence.
    const cdc = gd.cdc_readiness || {};
    log('Gate: CDC readiness is reported', !!cdc.status, cdc.status);
    log('Gate: CDC readiness explains why SCT cannot answer it',
        cdc.status !== 'blocked' || /never reads redo/.test(cdc.why_not_from_sct || ''));
    if (cdc.status === 'clear') {
      log('Gate: a clear CDC verdict rests on real evidence',
          !!(cdc.readiness && cdc.readiness.log_mode));
    }

    // Most items are work, not blockers -- and the summary must say so.
    const totalItems = (gd.groups || []).reduce((a, g) => a + g.item_count, 0);
    log('Gate: not everything halts', gd.blockers.length < totalItems,
        `${gd.blockers.length} blocking of ${totalItems}`);
    log('Gate: the summary says work remains',
        gd.blockers.length === totalItems || /remain as work/.test(gd.summary));

    // Every blocker must name its phases and how it clears.
    log('Gate: every blocker names the phases it blocks',
        gd.blockers.every(b => (b.blocks_in_scope || []).length > 0));
    log('Gate: every blocker says what clears it',
        gd.blockers.every(b => (b.clears_when || '').length > 10));

    // The point of the gate.
    const provisionAfter = await stageClass('provision');
    if (gd.verdict === 'HALT') {
      log('Gate: a HALT leaves Phase 6 shut',
          !provisionAfter.includes('done') && provisionAfter === provisionBefore,
          provisionAfter);
    } else {
      log('Gate: a PROCEED opens Phase 6', !provisionAfter.includes('idle'),
          provisionAfter);
    }
    log('Gate: the phase list rendered',
        await page.$$eval('#sctPhaseList .check', e => e.length) === 5);
  }
  await page.screenshot({ path: path.join(OUT, '03-gate.png'), fullPage: true });

  // ---- Reload: the run must survive a refresh ----------------------------
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);
  await page.evaluate(() => show('assess'));
  const restored = await page.evaluate(() => SCT && SCT.action_item_count);
  log('Reload: the SCT assessment is restored', (restored || 0) > 0, `${restored} items`);
  await page.evaluate(() => show('remediate'));
  log('Reload: Phase 4 is still armed',
      !(await page.locator('#btnSctRemediate').isDisabled()));
  await page.evaluate(() => show('gate'));
  log('Reload: Phase 5 is still armed',
      !(await page.locator('#btnSctGate').isDisabled()));

  // ---- Phone width -------------------------------------------------------
  await page.setViewportSize({ width: 400, height: 900 });
  for (const view of ['assess', 'remediate', 'gate']) {
    await page.evaluate(v => show(v), view);
    await page.waitForTimeout(250);
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    log(`Phone: ${view} does not overflow at 400px`, overflow <= 1, `${overflow}px`);
  }
  await page.screenshot({ path: path.join(OUT, '04-phone.png'), fullPage: true });

  log('no page errors', errors.length === 0, errors.slice(0, 3).join(' | '));

  await browser.close();
  console.log(`\n${pass}/${pass + fail} passed${fail ? `, ${fail} FAILED` : ''}`);
  process.exit(fail ? 1 : 0);
})();
