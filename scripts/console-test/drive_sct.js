// Drives the AWS SCT panel on the Assess screen.
//
// What only a browser can settle here, and why each one is worth a test:
//
//   1. **The dropdown lists the out-of-scope targets and marks them.** Hiding
//      Aurora and Redshift would look like SCT could not assess them; offering
//      them unmarked would imply DBShift migrates to them. Both are wrong, and
//      the difference is only visible rendered.
//   2. **The preflight names a missing prerequisite.** Three manual installs;
//      a missing one must say which and how to fix it, not fail as a Java
//      stack trace.
//   3. **SCT is the assessment, and the rules engine is off the screen.** The
//      50-rule engine still runs headlessly -- Phases 3, 7, 9 and 10 read its
//      record -- but a client must not see two competing verdicts. Whether its
//      panels are actually hidden is only answerable rendered.
//   4. **The routing is shown, and shown as deterministic.** "Where the fix
//      belongs" is the screen's new claim, and it must say the model does not
//      decide it -- otherwise two runs could disagree about what blocks a phase.
//   5. **No horizontal overflow at phone width.** The SCT table now has seven
//      columns on a screen that already has one.
//
// Run: node drive_sct.js    (DBSHIFT_URL, DBSHIFT_COLLECTOR_PASSWORD)
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const BASE = process.env.DBSHIFT_URL || 'http://127.0.0.1:8765';
const PASSWORD = process.env.DBSHIFT_COLLECTOR_PASSWORD || '';
const OUT = path.join(__dirname, 'shots-sct');
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
  page.on('console', m => {
    if (m.type() === 'error' && !/40\d \(/.test(m.text())) errors.push('console: ' + m.text());
  });

  await page.goto(BASE, { waitUntil: 'domcontentloaded' });

  // ---- the panel exists on the Assess screen, before anything is run -------
  await page.evaluate(() => show('assess'));
  log('SCT panel is on the Assess screen',
      await page.locator('#sctTarget').count() === 1);
  log('the run button exists', await page.locator('#btnSct').count() === 1);
  log('it is disabled until the prerequisites are known',
      await page.locator('#btnSct').isDisabled());

  // ---- targets, straight from the API -------------------------------------
  const t = await page.evaluate(() => fetch('/api/sct/targets').then(r => r.json()));
  log('four targets offered', t.targets.length === 4, `${t.targets.length}`);
  log('PostgreSQL is the default', t.default === 'rds-postgresql', t.default);
  const inScope = t.targets.filter(x => x.in_scope).map(x => x.id).sort();
  log('exactly two are in scope', JSON.stringify(inScope) ===
      JSON.stringify(['rds-oracle', 'rds-postgresql']), inScope.join(','));
  log('Aurora is listed, not hidden', t.targets.some(x => x.id === 'aurora-postgresql'));
  log('Redshift is listed, not hidden', t.targets.some(x => x.id === 'redshift'));
  log('every out-of-scope target explains itself',
      t.targets.filter(x => !x.in_scope).every(x => (x.scope_note || '').length > 20));

  // ---- the dropdown renders that distinction ------------------------------
  await page.evaluate(() => loadSctTargets());
  await page.waitForFunction(() => $('#sctTarget').options.length === 4, { timeout: 5000 });
  const opts = await page.$$eval('#sctTarget option', els =>
    els.map(e => ({ v: e.value, label: e.textContent.trim() })));
  log('the dropdown shows all four', opts.length === 4);
  log('out-of-scope options are marked in the dropdown',
      opts.filter(o => /out of scope/i.test(o.label)).length === 2,
      opts.map(o => o.label).join(' | '));
  log('in-scope options carry no scope warning',
      opts.filter(o => /out of scope/i.test(o.label))
          .every(o => o.v === 'aurora-postgresql' || o.v === 'redshift'));

  // ---- selecting an out-of-scope target warns, in the page ---------------
  await page.selectOption('#sctTarget', 'redshift');
  await page.waitForTimeout(150);
  const warn = (await page.locator('#sctScopeNote').innerText()).toLowerCase();
  log('choosing Redshift warns it is out of scope', warn.includes('out of'), warn.slice(0, 70));
  log('...and says it is not a migration path',
      warn.includes('not a migration path') || warn.includes('comparison'));

  await page.selectOption('#sctTarget', 'rds-postgresql');
  await page.waitForTimeout(150);
  const ok = (await page.locator('#sctScopeNote').innerText()).toLowerCase();
  log('choosing an in-scope target does not warn', !ok.includes('out of scope'), ok.slice(0, 60));

  // ---- preflight ---------------------------------------------------------
  const pf = await page.evaluate(() => fetch('/api/sct/preflight').then(r => r.json()));
  log('three prerequisites are checked', (pf.checks || []).length === 3);
  log('the host is named', pf.host === 'local', pf.host);
  log('the host note explains the customer path',
      /EC2|VPC/.test(pf.host_note || ''), (pf.host_note || '').slice(0, 60));
  log('every unmet prerequisite says how to fix it',
      (pf.checks || []).filter(c => c.status !== 'ok').every(c => c.remedy));

  await page.evaluate(() => loadSctPreflight());
  await page.waitForTimeout(400);
  if (pf.ready) {
    log('prerequisites met, so the run button is armed',
        !(await page.locator('#btnSct').isDisabled()));
    log('the prerequisite panel is hidden when all three pass',
        !(await page.locator('#sctPre').isVisible()));
  } else {
    log('prerequisites missing, so the run button stays disabled',
        await page.locator('#btnSct').isDisabled());
    log('the prerequisite panel is shown when one is missing',
        await page.locator('#sctPre').isVisible());
    const txt = await page.locator('#sctPreList').innerText();
    log('the panel names the missing prerequisite', /\[--\]|\[XX\]/.test(txt), txt.slice(0, 80));
  }

  // ---- the endpoints gate on state, not on nothing ------------------------
  //
  // These must 409 before a run and 200 after one. Which applies depends on
  // whether this server already holds a result -- a long-running console
  // usually does, and asserting 409 unconditionally made the test fail for the
  // wrong reason. So the expectation is derived from the server's own state,
  // and both directions are still checked.
  const already = await page.evaluate(() =>
    fetch('/api/sct/assessment?target=rds-postgresql').then(r => r.ok));
  const want = already ? 200 : 409;
  for (const route of ['/api/sct/assessment?target=rds-postgresql',
                       '/api/sct/report.pdf?target=rds-postgresql',
                       '/api/sct/report.xlsx?target=rds-postgresql',
                       '/api/sct/report.csv?target=rds-postgresql']) {
    const code = await page.evaluate(r => fetch(r).then(x => x.status), route);
    log(`${route.split('?')[0]} ${already ? 'serves a stored result' : 'refuses before a run'}`,
        code === want, `HTTP ${code}`);
  }
  // An unknown target must be refused whatever state the server is in.
  const badTarget = await page.evaluate(() =>
    fetch('/api/sct/assessment?target=mysql').then(r => r.status));
  log('an unknown target is refused', badTarget >= 400, `HTTP ${badTarget}`);

  // ---- the two provenance claims must not contradict ---------------------
  //
  // Read `textContent`, not `innerText`. Both provenance notes live inside
  // result panels that stay hidden until their phase has run, and innerText
  // skips hidden subtrees -- so an innerText assertion here passes or fails on
  // whether a run has happened, which is not what is being tested. The claim
  // being checked is that the markup carries both statements and they do not
  // contradict; that is true before any run, and must stay true.
  const body = (await page.locator('#view-assess').evaluate(el => el.textContent))
    .replace(/\s+/g, ' ');
  log('the SCT downloads claim to be SCT’s own',
      /AWS SCT'?s own files/i.test(body));
  log('...and say they are not re-rendered',
      /byte for byte|not.{0,20}re-rendered/i.test(body));
  log('the panel explains complexity is not severity',
      /work a person must do/i.test(body));

  // **SCT is the assessment now.** The 50-rule engine still runs headlessly --
  // Phases 3, 7, 9 and 10 read its record -- but it must not be on this screen.
  log('the rules-engine run button is hidden',
      !(await page.locator('#rulesRunRow').isVisible()));
  log('the rules-engine result panel is hidden',
      !(await page.locator('#assessResult').isVisible()));
  log('the rules-engine empty state is hidden',
      !(await page.locator('#assessEmpty').isVisible()));
  // The wording was trimmed on 2026-09-17 ("I don't need more text"), so assert
  // the claim rather than the sentence: the heading names SCT, and it says whose
  // verdict it is.
  log('the screen leads with SCT',
      /AWS Schema Conversion Tool/i.test(body) && /AWS's own verdict/i.test(body));

  // The routing is the new claim on this screen, and it must say it is not the
  // model's decision -- that is what makes two runs agree.
  log('the screen explains where-the-fix-belongs', /Where the fix belongs/i.test(body));
  // "the model never decides it" moved out of the page body when the
  // paragraphs were trimmed. It is still asserted where it is enforced --
  // sct.selftest checks route.py imports no model client -- so here only check
  // the routing is presented as a property of the item, not a model output.
  log('...and routes each item by where the fix belongs',
      /routed by where the fix belongs/i.test(body));

  await page.screenshot({ path: path.join(OUT, '01-sct-panel.png'), fullPage: true });

  // ---- the real run, only if we can connect ------------------------------
  if (PASSWORD) {
    await page.evaluate(() => show('connect'));
    await page.fill('#password', PASSWORD);
    await page.click('#btnConnect');
    const connected = await page.waitForFunction(
      () => $('#connStatus').className.includes('ok'), { timeout: 60000 }
    ).then(() => true).catch(() => false);
    log('connected to the source', connected);

    if (connected) {
      // Connecting auto-advances to Discover after ~550ms. Wait that out before
      // navigating, or the timer fires afterwards and moves the page off Assess
      // -- which leaves #btnSct present but not visible.
      await page.waitForTimeout(1200);
      await page.evaluate(() => show('assess'));
      await page.waitForFunction(
        () => $('#view-assess').classList.contains('on') ||
              getComputedStyle($('#view-assess')).display !== 'none',
        { timeout: 10000 }
      );
      await page.locator('#btnSct').waitFor({ state: 'visible', timeout: 10000 });
      log('the run button is armed once connected',
          !(await page.locator('#btnSct').isDisabled()));

      await page.click('#btnSct');
      // SCT takes minutes even on the 90-object demo estate: ~2.5 min in
      // CreateReport alone, plus JVM start and metadata load. Measured at
      // roughly 5 minutes end to end on DBMIG_APP, so 25 gives real headroom
      // without hanging a CI run forever. Wait for either outcome -- a result,
      // or an error the panel renders. Silence is neither, which is why the
      // condition covers both and the failure is reported rather than assumed.
      const done = await page.waitForFunction(
        () => $('#sctResult').style.display !== 'none' ||
              $('#sctLive').querySelector('.note.bad') !== null,
        { timeout: 1500000, polling: 2000 }
      ).then(() => true).catch(() => false);
      log('SCT finished with a rendered outcome', done);
      if (!done) {
        // Do not fall through into assertions that read SCT -- they would
        // throw on null and hide the real result, which is that it timed out.
        console.log('note: SCT did not finish in 25 minutes; skipping result assertions');
        await page.screenshot({ path: path.join(OUT, '02-sct-timeout.png'), fullPage: true });
      }
      if (done) {

      const failed = await page.locator('#sctLive .note.bad').count() > 0;
      if (failed) {
        const msg = await page.locator('#sctLive .note.bad').innerText();
        // A failure is a legitimate outcome to render -- what must not happen
        // is an empty result presented as a clean estate.
        log('a failure names what SCT said', msg.length > 20, msg.slice(0, 120));
        log('a failure is not shown as a result',
            !(await page.locator('#sctResult').isVisible()));
        await page.screenshot({ path: path.join(OUT, '02-sct-failed.png'), fullPage: true });
      } else {
        const summary = await page.evaluate(() => SCT);
        log('the record names the target', !!(summary.target || {}).label);
        log('action items were parsed', typeof summary.action_item_count === 'number');
        log('complexity buckets are present',
            Object.keys(summary.by_complexity || {}).length === 4);

        // Every issue must carry SCT's own complexity and be flagged as
        // having a *derived* severity -- the axis distinction, asserted.
        const issues = summary.issues || [];
        if (issues.length) {
          log('every issue says it came from SCT',
              issues.every(i => i.source === 'aws-sct'));
          log('every issue marks its severity as derived',
              issues.every(i => i.severity_is_derived === true));
          log('every issue keeps SCT’s complexity',
              issues.every(i => ['simple','medium','complex','decision'].includes(i.complexity)));
        }

        // Download SCT's own files for real and check the magic bytes, the
        // way drive_counts_export.js does for the CSV.
        if (summary.has_pdf) {
          const dl = await Promise.all([
            page.waitForEvent('download', { timeout: 60000 }),
            page.click('#btnSctPdf'),
          ]).then(([d]) => d).catch(() => null);
          if (dl) {
            const p = path.join(OUT, 'aws-sct.pdf');
            await dl.saveAs(p);
            const head = fs.readFileSync(p).slice(0, 5).toString('latin1');
            log('SCT’s PDF downloads and is a real PDF', head === '%PDF-', head);
          } else { log('SCT’s PDF downloads', false, 'no download event'); }
        }
        if (summary.has_csv) {
          const dl = await Promise.all([
            page.waitForEvent('download', { timeout: 60000 }),
            page.click('#btnSctXlsx'),
          ]).then(([d]) => d).catch(() => null);
          if (dl) {
            const p = path.join(OUT, 'aws-sct.xlsx');
            await dl.saveAs(p);
            const head = fs.readFileSync(p).slice(0, 2).toString('latin1');
            log('SCT’s Excel downloads and is a real workbook', head === 'PK', head);
          } else { log('SCT’s Excel downloads', false, 'no download event'); }
        }
        await page.screenshot({ path: path.join(OUT, '02-sct-result.png'), fullPage: true });
      }
      }  // if (done)
    }
  } else {
    console.log('note: DBSHIFT_COLLECTOR_PASSWORD not set -- skipped the live SCT run');
  }

  // ---- phone width -------------------------------------------------------
  await page.setViewportSize({ width: 400, height: 900 });
  await page.evaluate(() => show('assess'));
  await page.waitForTimeout(300);
  const overflow = await page.evaluate(() =>
    document.documentElement.scrollWidth - document.documentElement.clientWidth);
  log('no horizontal overflow at 400px', overflow <= 1, `${overflow}px`);
  await page.screenshot({ path: path.join(OUT, '03-sct-phone.png'), fullPage: true });

  log('no page errors', errors.length === 0, errors.slice(0, 3).join(' | '));

  await browser.close();
  console.log(`\n${pass}/${pass + fail} passed${fail ? `, ${fail} FAILED` : ''}`);
  process.exit(fail ? 1 : 0);
})();
