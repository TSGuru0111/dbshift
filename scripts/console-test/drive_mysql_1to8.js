// Phases 1 -> 8 on a MySQL source, driven through the console in headless Edge.
//
//   node drive_mysql_1to8.js                         # MySQL -> RDS for PostgreSQL, nothing bills
//   node drive_mysql_1to8.js --pair mysql            # MySQL -> RDS for MySQL, to the Phase 6 and 7 plans
//   node drive_mysql_1to8.js --execute --confirm 280646578374 --approver you@company.com
//   node drive_mysql_1to8.js --pair mysql --execute --confirm 280646578374 --approver you@company.com
//   node drive_mysql_1to8.js --skip-phone            # desktop pass only
//   node drive_mysql_1to8.js --strict                # known server defects fail the run too
//
// Default (no --execute): Connect -> Discover -> AWS SCT (cached) -> Target &
// Sizing -> Remediate -> 4b -> 4c -> 4d -> Gate -> Phase 6 render (read-only AWS
// checks) -> Phase 7 plan -> Phase 8 screen, then the same Phase 4c/6/7/8
// screens again at phone width, then an in-page Oracle spot check. On the MySQL
// pair it also reads the schema copy plan, which is SHOW CREATE on the source.
// NOTHING IN THE DEFAULT RUN CREATES OR WRITES TO AN AWS RESOURCE.
//
// --execute --confirm <account> adds the steps that bill or write, each one
// idempotent-friendly (an existing stack is used, a loaded target is not loaded
// twice, and what it would do is printed first):
//   PostgreSQL pair: deploy -> verify -> 4c zero-date decision (if pending) ->
//     regenerate 4c -> apply pre-load -> DMS create and run -> 4c post-load ->
//     residue -> validate
//   MySQL pair: deploy -> verify -> schema copy pre -> DMS -> schema copy post ->
//     residue -> validate. The cutover stage (enable events) is Phase 9's and is
//     never run here.
// The kill switch is never touched. Everything created keeps billing until it is
// destroyed -- the run ends by saying so.
//
// The collector password is read from provision/output/mysql-ec2-password.txt
// and never printed. Needs the console up on 127.0.0.1:8765 (DBSHIFT_URL).
const { chromium } = require('playwright-core');
const path = require('path');
const fs = require('fs');

// ---------------------------------------------------------------- arguments
const argv = process.argv.slice(2);
const flag = n => argv.includes(n);
const opt = (n, d) => { const i = argv.indexOf(n); return i >= 0 && argv[i + 1] ? argv[i + 1] : d; };
const PAIR = /^my/i.test(opt('--pair', 'postgresql')) ? 'MYSQL' : 'POSTGRESQL';
const EXECUTE = flag('--execute');
const CONFIRM = opt('--confirm', '');
const APPROVER = opt('--approver', process.env.DBSHIFT_APPROVER || '');
const STRICT = flag('--strict');
const PHONE = !flag('--skip-phone');
if (EXECUTE && !/^\d{12}$/.test(CONFIRM)) {
  console.error('--execute needs --confirm <the 12-digit AWS account id>. Nothing was run.');
  process.exit(2);
}
if (EXECUTE && !APPROVER.includes('@')) {
  console.error('--execute needs --approver <email>: every write is recorded against a person. Nothing was run.');
  process.exit(2);
}

const URL = process.env.DBSHIFT_URL || 'http://127.0.0.1:8765';
const ROOT = path.resolve(__dirname, '..', '..');
const OUT = path.join(__dirname, 'shots-mysql-1to8');
const SCT_ID = {POSTGRESQL: 'rds-postgresql', MYSQL: 'rds-mysql'}[PAIR];
const CONN = {dsn: process.env.DBSHIFT_MYSQL_DSN || '3.108.190.1:3306/dbmig_mysql_app',
              user: 'dbmig_collector', schema: 'dbmig_mysql_app'};
const PG_COMPILE = {dsn: process.env.DBSHIFT_PG_DSN || 'localhost:5432/dbshift',
                    user: process.env.DBSHIFT_PG_USER || 'dbshift',
                    password: process.env.DBSHIFT_PG_PASSWORD || 'dbshift-local-only'};
const MIN = 60000;

let pass = 0, fail = 0, known = 0;
const ok = (n, c, d = '') => { c ? pass++ : fail++; console.log(`  [${c ? 'ok' : 'FAIL'}] ${n}${c ? '' : ' -- ' + d}`); return c; };
const note = t => console.log(`  ..  ${t}`);

// Oracle-only vocabulary. "PL/pgSQL" is PostgreSQL's and does not match.
const ORACLE_WORDS = /PL\/SQL|\bOracle\b|ARCHIVELOG|Data Pump|v\$database|\bDBA_|\bredo\b|ROWNUM|\b5200\b|\b5659\b|Enterprise Edition|Standard Edition|NOVALIDATE|\b1521\b/;
// Deliberate contrasts, not leaks -- each names Oracle to say what does NOT
// carry over. Kept narrow so a real leak on the same line still fails.
const CONTRAST = /the Oracle rule|Oracle's list does not transfer|An Oracle-only behaviour|not a Data Pump dump/g;

// Server defects found by this harness and reported, not fixed here (the
// console's server is another owner's). Printed on every run as KNOWN; they
// fail the run only with --strict. Remove a row once its defect is fixed.
const KNOWN_API = [
  // Emptied 2026-09-30: all three were fixed in web/server.py and dms/run.py
  // (decisions endpoints' import name; a deleted task is 409). A recurrence fails.
];

// Oracle wording that arrives in a server payload rather than the page, so it
// cannot be fixed in index.html. Reported as KNOWN, like KNOWN_API.
const KNOWN_WORDING = [
  // Emptied 2026-09-30: the Discover tile says "Stored routines" on MySQL and the
  // migration-mode detail comes from collector/mode.DETAIL_MYSQL. A recurrence fails.
];

function oracleHits(text) {
  return text.split('\n').filter(l => ORACLE_WORDS.test(l.replace(CONTRAST, '')))
    .map(l => l.trim());
}

async function viewText(p, id) {
  return p.evaluate(v => {
    const el = document.getElementById('view-' + v);
    el.querySelectorAll('details').forEach(d => { d.open = true; });
    const titles = [...el.querySelectorAll('[title]')].filter(x => x.offsetParent !== null).map(x => x.title);
    const bar = document.getElementById('pagenav');
    const barTitles = [...bar.querySelectorAll('[title]')].map(x => x.title);
    return [el.innerText, ...titles, bar.innerText, ...barTitles,
            document.getElementById('crumbTitle').textContent].join('\n');
  }, id);
}

// Polled from the test side: the page holds open EventSources during long
// runs and a page-side waitForFunction proved unreliable against them.
async function until(p, fn, arg, timeout, every = 2000) {
  const t0 = Date.now();
  for (;;) {
    const v = await p.evaluate(fn, arg).catch(() => null);
    if (v) return v;
    if (Date.now() - t0 > timeout) return null;
    await p.waitForTimeout(every);
  }
}

async function api(p, method, url, body) {
  const r = method === 'GET' ? await p.request.get(URL + url, {timeout: 15 * MIN})
                             : await p.request.post(URL + url, {data: body || {}, timeout: 15 * MIN});
  let json = null; const text = await r.text();
  try { json = JSON.parse(text); } catch (e) { /* stream or html */ }
  return {status: r.status(), json, text};
}

function watch(p) {
  const w = {errs: [], bad: [], knownHits: []};
  p.on('pageerror', e => w.errs.push(String(e)));
  p.on('console', m => {
    if (m.type() !== 'error') return;
    if (/Failed to load resource/.test(m.text())) return;   // recorded with its URL below
    w.errs.push(m.text());
  });
  p.on('response', x => {
    const u = x.url().replace(/^https?:\/\/[^/]+/, '');
    if (!u.startsWith('/api/') || x.status() < 400 || x.status() === 409) return;
    const line = `${x.status()} ${x.request().method()} ${u}`;
    const k = KNOWN_API.find(([re]) => re.test(line));
    (k ? w.knownHits : w.bad).push(k ? `${line}  [${k[1]}]` : line);
  });
  return w;
}
function drain(w, tag) {
  ok(`${tag}: no JS errors`, !w.errs.length, w.errs.slice(0, 3).join(' | '));
  ok(`${tag}: no failing /api/ calls`, !w.bad.length, [...new Set(w.bad)].slice(0, 5).join(', '));
  for (const k of new Set(w.knownHits)) {
    if (STRICT) ok(`${tag}: known server defect`, false, k);
    else { known++; console.log(`  [KNOWN] ${k}`); }
  }
  w.errs.length = 0; w.bad.length = 0; w.knownHits.length = 0;
}

async function show(p, v) {
  await p.evaluate(x => show(x), v);
  await p.waitForTimeout(250);
}
async function noSideways(p, tag, v) {
  const over = await p.evaluate(() => {
    const m = document.querySelector('main');
    return {page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
            pane: m.scrollWidth - m.clientWidth};
  });
  ok(`${tag}: ${v} does not scroll sideways`, over.page <= 1 && over.pane <= 1, JSON.stringify(over));
}
async function wording(p, tag, v) {
  const all = oracleHits(await viewText(p, v));
  const hits = all.filter(h => !KNOWN_WORDING.some(([re]) => re.test(h)));
  ok(`${tag}: ${v} carries no Oracle-only wording`, !hits.length, hits.slice(0, 4).map(h => h.slice(0, 140)).join(' | '));
  for (const [re, why] of KNOWN_WORDING) {
    if (!all.some(h => re.test(h))) continue;
    if (STRICT) ok(`${tag}: ${v} known server wording`, false, why);
    else { known++; console.log(`  [KNOWN] ${v}: ${why}`); }
  }
}
async function shot(p, name) {
  await p.screenshot({path: path.join(OUT, `${PAIR.toLowerCase()}-${name}.png`)}).catch(() => {});
}

// Phase 7's plan rewrites views on Bedrock when the model tier is live, which
// takes tens of seconds; the button reads "Planning…" until it is back.
async function planDms(p) {
  await show(p, 'migrate');
  await p.click('#btnDmsPlan');
  return until(p, () => $('#btnDmsPlan').textContent === 'Plan' && !$('#btnDmsPlan').disabled
                        && (DMSPLAN || $('#dmsMode .note.bad')), null, 5 * MIN, 1500);
}

// ======================================================================
(async () => {
  fs.mkdirSync(OUT, {recursive: true});
  const pwFile = path.join(ROOT, 'provision', 'output', 'mysql-ec2-password.txt');
  if (!fs.existsSync(pwFile)) { console.error(`missing ${pwFile}`); process.exit(2); }
  const PASSWORD = fs.readFileSync(pwFile, 'utf8').trim();

  console.log(`MySQL -> ${PAIR === 'MYSQL' ? 'RDS for MySQL' : 'RDS for PostgreSQL'} · ${EXECUTE
    ? `EXECUTE against account ${CONFIRM} as ${APPROVER} -- THIS BILLS` : 'read-only (no --execute)'}`);

  const b = await chromium.launch({channel: 'msedge', headless: true});
  const p = await b.newPage({viewport: {width: 1440, height: 900}});
  p.on('dialog', d => d.accept());   // the source-engine switch asks before clearing a session
  const w = watch(p);
  await p.goto(URL, {waitUntil: 'networkidle'});
  await p.waitForTimeout(1500);

  // ---------------------------------------------------------------- Connect
  console.log('\nConnect');
  await show(p, 'connect');
  await p.click('#srcPick .pathopt[data-src="MYSQL"]');
  await until(p, () => SRC === 'MYSQL', null, 20000, 300);
  ok('the source engine is MySQL', await p.evaluate(() => SRC) === 'MYSQL');
  ok('the DSN label is MySQL-shaped', /host:port\/database/.test(await p.$eval('#dsnLabel', e => e.textContent)));
  await p.fill('#dsn', CONN.dsn);
  await p.fill('#user', CONN.user);
  await p.fill('#schema', CONN.schema);
  await p.fill('#password', PASSWORD);
  await p.click('#btnConnect');
  const conn = await until(p, () => /Connected|failed|Not connected|Request failed/.test($('#connText').textContent)
                                    && $('#connText').textContent !== 'Connecting…' && $('#connText').textContent,
                           null, 2 * MIN, 500);
  if (!ok('Connect: the preflight connects', /^Connected/.test(conn || ''), conn || 'timed out')) {
    await shot(p, 'connect-failed'); await b.close(); process.exit(1);
  }
  ok('Connect: no failed preflight check', !(await p.$$('#checks .check.fail')).length);
  await shot(p, '1-connect');
  drain(w, 'Connect');

  // ---------------------------------------------------------------- Discover
  console.log('\nPhase 1 · Discover');
  await show(p, 'discover');
  await p.click('#modeOpts .pathopt[data-mode="full-load"]').catch(() => {});
  await p.waitForTimeout(500);
  await p.click('#btnDiscover');
  const disc = await until(p, () => stageState.discover === 'done' ? 'done'
                                    : /failed|lost|error/.test(stageSub.discover || '') ? stageSub.discover : null,
                           null, 10 * MIN);
  ok('Discover: discovery completes', disc === 'done', disc || 'timed out');
  await until(p, () => $('#discTiles .tile'), null, MIN, 500);
  ok('Discover: the counts rendered', (await p.$$('#discTiles .tile')).length > 0);
  ok('Discover: the MySQL binlog panel is shown', await p.$eval('#mysqlBinlogPanel', e => e.style.display !== 'none'));
  await wording(p, 'Discover', 'discover');
  await shot(p, '2-discover');
  drain(w, 'Discover');

  // ---------------------------------------------------------------- Assess (AWS SCT)
  console.log(`\nPhase 2 · Assess (AWS SCT, ${SCT_ID}; cached)`);
  await show(p, 'assess');
  await p.selectOption('#sctTarget', SCT_ID);
  await p.click('#btnSct');
  const assessed = await until(p, () => $('#sctResult').style.display !== 'none' ? 'ok'
                                        : $('#sctLive .note.bad') ? $('#sctLive .note.bad').innerText.slice(0, 200) : null,
                               null, 40 * MIN, 3000);
  ok('Assess: AWS SCT produced a result', assessed === 'ok', assessed || 'timed out');
  const items = await p.evaluate(() => (SCT || {}).action_item_count);
  if (PAIR === 'MYSQL') ok('Assess: same-engine SCT reports zero action items', items === 0, String(items));
  else ok('Assess: SCT reports action items for PostgreSQL', items > 0, String(items));
  await wording(p, 'Assess', 'assess');
  drain(w, 'Assess');

  // ---------------------------------------------------------------- Target & Sizing
  console.log('\nPhase 3 · Target & Sizing');
  await show(p, 'target');
  await p.click(`#pathPick .pathopt[data-engine="${PAIR}"]`);
  await until(p, e => ENGINE === e, PAIR, 20000, 300);
  ok(`Target: ${PAIR} chosen`, await p.evaluate(() => ENGINE) === PAIR);
  await p.click('#btnSize');
  const sized = await until(p, () => stageState.target === 'done' ? 'done'
                                     : /failed|lost/.test(stageSub.target || '') ? stageSub.target : null, null, 10 * MIN);
  ok('Target: sizing completes', sized === 'done', sized || 'timed out');
  const spec = await p.$eval('#targetSpec', e => e.innerText).catch(() => '');
  ok('Target: names the chosen RDS engine', spec.includes(PAIR === 'MYSQL' ? 'RDS for MySQL' : 'RDS for PostgreSQL'), spec.slice(0, 120));
  await wording(p, 'Target', 'target');
  drain(w, 'Target');

  // ---------------------------------------------------------------- Remediate
  console.log('\nPhase 4 · Remediate');
  await show(p, 'remediate');
  await p.fill('#sctRemApprover', APPROVER || 'drive@example.com').catch(() => {});
  await p.click('#btnSctRemediate');
  const rem = await until(p, () => $('#sctRemResult').style.display !== 'none' && stageState.remediate !== 'busy' ? 'ok'
                                   : $('#sctRemLive .note.bad') ? $('#sctRemLive .note.bad').innerText.slice(0, 200) : null,
                          null, 20 * MIN, 3000);
  ok('Remediate: a plan rendered', rem === 'ok', rem || 'timed out');
  const groups = await p.$eval('#sctRemGroups', e => e.innerText).catch(() => '');
  ok('Remediate: the outcome is explained', PAIR === 'MYSQL' ? /expected result/.test(groups)
                                                            : /Needs a decision|Needs a person/.test(groups), groups.slice(0, 160));
  await wording(p, 'Remediate', 'remediate');
  drain(w, 'Remediate');

  // ---------------------------------------------------------------- 4b / 4c / 4d
  console.log('\nPhases 4b, 4c, 4d');
  if (PAIR === 'POSTGRESQL') {
    await show(p, 'convert');
    // The compile-target form sits in a disclosure that is closed by default.
    await p.evaluate(() => { const d = $('#convSetup').closest('details'); if (d) d.open = true; });
    await p.fill('#pgDsn', PG_COMPILE.dsn); await p.fill('#pgUser', PG_COMPILE.user);
    await p.fill('#pgPassword', PG_COMPILE.password);
    await p.click('#btnPgTarget');
    await until(p, () => !/checking/.test($('#pgState').textContent), null, MIN, 500);
    ok('4b: the compile target is registered', await p.evaluate(() => PG_DSN === $('#pgDsn').value.trim()),
       await p.$eval('#pgState', e => e.textContent));
    await p.click('#btnConvert');
    const conv = await until(p, () => stageState.convert === 'done' ? 'done'
                                      : /failed|lost|error/.test(stageSub.convert || '') ? stageSub.convert : null,
                             null, 20 * MIN, 3000);
    ok('4b: routines convert', conv === 'done', conv || 'timed out');
    await wording(p, '4b', 'convert');

    await show(p, 'schemaddl');
    await p.click('#btnSchemaDdl');
    const ddl = await until(p, () => /compiled and rolled back|not compiled/.test($('#ddlMeta').textContent)
                                     ? $('#ddlMeta').textContent : null, null, 5 * MIN);
    ok('4c: the DDL generates and compiles', /compiled and rolled back/.test(ddl || ''), ddl || 'timed out');
    await until(p, () => DECISIONS, null, 20000, 500);
    const zd = await p.evaluate(() => ({shown: $('#zeroDatePanel').style.display !== 'none',
                                        text: $('#zeroDatePanel').innerText, err: (DECISIONS || {}).error}));
    ok('4c: the zero-date panel explains itself (pending, decided, or unreadable)',
       zd.shown && /(hold zero dates|Decided:|could not be read)/.test(zd.text), zd.text.slice(0, 200));
    if (zd.err) note(`4c: /api/decisions unreadable (${zd.err}) -- the panel says so`);
    ok('4c: the apply panel says which database it writes to',
       /No target is deployed yet|Writes to the RDS instance/.test(await p.$eval('#ddlApplyTarget', e => e.innerText)));
    await wording(p, '4c', 'schemaddl');

    await show(p, 'appsql');
    await p.fill('#appsqlRoot', 'scripts/demo-app-mysql/mappers');
    if (await p.$eval('#appsqlModel', e => e.checked)) await p.uncheck('#appsqlModel');
    await p.click('#btnAppsql');
    const app = await until(p, () => stageState.appsql === 'done' ? $('#appsqlMeta').textContent
                                     : /failed/.test(stageSub.appsql || '') ? 'failed' : null, null, 10 * MIN);
    ok('4d: application SQL converts', / statements/.test(app || ''), app || 'timed out');
    await wording(p, '4d', 'appsql');
  } else {
    for (const [v, banner, btn] of [['convert', '#naConvert', '#btnConvert'], ['schemaddl', '#naSchemaDdl', '#btnSchemaDdl'],
                                    ['appsql', '#naAppSql', '#btnAppsql']]) {
      await show(p, v);
      ok(`${v}: states it is not applicable`, /Not applicable/.test(await p.$eval(banner, e => e.innerText)));
      ok(`${v}: its run button is disabled`, await p.$eval(btn, e => e.disabled));
      await wording(p, v, v);
    }
    await show(p, 'schemaddl');
    ok('4c: promises no compile on this path',
       /Nothing is compiled on this path/.test(await p.$eval('#ddlTargetStatus', e => e.innerText)));
    ok('4c: says Phase 7 copies the schema',
       /copies the schema from the source/.test(await p.$eval('#naSchemaDdl', e => e.innerText)));
  }
  drain(w, '4b-4d');

  // ---------------------------------------------------------------- Gate
  console.log('\nPhase 5 · Blocker gate');
  await show(p, 'gate');
  await p.click('#btnSctGate');
  await until(p, () => SCTGATE && $('#sctGateResult').style.display !== 'none', null, 2 * MIN, 1000);
  const gate = await p.evaluate(() => SCTGATE && SCTGATE.verdict);
  ok('Gate: PROCEED', gate === 'PROCEED', gate);
  ok('Gate: names the MySQL source', /source MySQL/.test(await p.$eval('#sctGateMeta', e => e.textContent)));
  await wording(p, 'Gate', 'gate');
  drain(w, 'Gate');

  // ---------------------------------------------------------------- Provision (render)
  console.log('\nPhase 6 · Provision (render and read-only checks)');
  await show(p, 'provision');
  await until(p, () => !$('#btnProvision').disabled, null, 20000, 500);
  await p.click('#btnProvision');
  const rendered = await until(p, () => PROV_PLAN && $('#provResult').style.display !== 'none' && !$('#btnProvision').disabled ? 'ok'
                                        : $('#provLive .note.bad') ? $('#provLive .note.bad').innerText : null,
                               null, 5 * MIN);
  ok('Provision: the plan renders', rendered === 'ok', rendered || 'timed out');
  await p.evaluate(() => provTab('plan'));
  const prov = await p.evaluate(() => ({
    tile: $('#provTiles .tile .v').innerText, target: PROV_PLAN.rendered.target,
    checks: Object.fromEntries(PROV_PLAN.checks.map(c => [c.name, [c.status, c.detail]])),
    params: $('#provParams').style.display !== 'none' ? $('#provParams').innerText : '',
    port: ($('#ovf_port') || {}).value, portDisabled: ($('#ovf_port') || {}).disabled,
    chipPlan: $('#provChipPlan').textContent, stack: PROV_PLAN.stack_name, account: PROV_PLAN.checks.find(c => c.name === 'aws_identity'),
  }));
  ok('Provision: renders the chosen engine', prov.target === PAIR, prov.target);
  ok('Provision: the engine tile names the engine and its version, not 19c',
     new RegExp(PAIR === 'MYSQL' ? 'MySQL' : 'PostgreSQL').test(prov.tile) && !/19c/.test(prov.tile), prov.tile);
  ok('Provision: the form shows the port the render uses, read-only',
     prov.port === (PAIR === 'MYSQL' ? '3306' : '5432') && prov.portDisabled, `${prov.port} disabled=${prov.portDisabled}`);
  if (PAIR === 'MYSQL') {
    ok('Provision: the parameter group is shown', /sql_mode/.test(prov.params) && /forced/.test(prov.params), prov.params.slice(0, 120));
    ok('Provision: mysql_support warns of the 8.0 -> 8.4 upgrade', (prov.checks.mysql_support || [])[0] === 'warn',
       JSON.stringify(prov.checks.mysql_support));
    ok('Provision: parameter_group validated', (prov.checks.parameter_group || [])[0] === 'pass', JSON.stringify(prov.checks.parameter_group));
    ok('Provision: prepared artefacts not applicable', /not applicable/.test((prov.checks.prepared_artefacts || [])[1] || ''));
  } else {
    ok('Provision: no parameter-group panel on PostgreSQL', !prov.params);
  }
  ok('Provision: the Plan chip carries its state', /ready/.test(prov.chipPlan), prov.chipPlan);
  await wording(p, 'Provision', 'provision');
  await noSideways(p, 'Provision', 'provision');
  await shot(p, '6-provision');
  drain(w, 'Provision');

  const acct = ((prov.account || {}).detail || '').match(/account (\d{12})/);
  if (EXECUTE && (!acct || acct[1] !== CONFIRM)) {
    ok('Execute: --confirm matches the account the console is signed in to', false,
       `console reports ${acct ? acct[1] : 'no account'}, --confirm ${CONFIRM}. Nothing was created.`);
    await b.close(); process.exit(1);
  }

  // ---------------------------------------------------------------- the billing steps
  let live = false;
  if (EXECUTE) {
    console.log(`\nPhase 6 · Deploy (${prov.stack}) -- BILLS`);
    let st = (await api(p, 'GET', '/api/provision/status')).json || {};
    if (st.status === 'CREATE_COMPLETE') note(`stack ${prov.stack} already exists and is running; using it`);
    else if (/_IN_PROGRESS$/.test(st.status || '')) note(`stack ${prov.stack} is ${st.status}; waiting for it`);
    else if (st.exists) {
      ok('Deploy: the existing stack is usable', false, `${prov.stack} is ${st.status}. Clean it up by hand or with the kill switch; not touched here.`);
    } else {
      const rate = await p.evaluate(() => PROV_PLAN.cost.estimate.instance_per_hour);
      note(`would deploy ${prov.stack} at $${rate}/h -- deploying`);
      await p.evaluate(() => provTab('deploy'));
      await p.fill('#dAcct', CONFIRM);
      await p.fill('#dRate', String(rate));
      if (await p.$eval('#dReasonWrap', e => e.style.display !== 'none'))
        await p.fill('#dReason', `drive_mysql_1to8 by ${APPROVER}`);
      await p.click('#btnDeploy');
      const started = await until(p, () => /Every check passed|Refused/.test($('#deployErr').innerText) && $('#deployErr').innerText,
                                  null, 5 * MIN, 2000);
      ok('Deploy: accepted', /Every check passed/.test(started || ''), (started || 'timed out').slice(0, 200));
    }
    for (const t0 = Date.now(); Date.now() - t0 < 50 * MIN; ) {
      st = (await api(p, 'GET', '/api/provision/status')).json || {};
      if (st.status === 'CREATE_COMPLETE' || /FAILED|ROLLBACK/.test(st.status || '') || !st.exists) break;
      await p.waitForTimeout(30000);
    }
    live = ok('Deploy: the target is CREATE_COMPLETE', st.status === 'CREATE_COMPLETE', st.status);
    await p.evaluate(() => provTab('live'));
    await p.click('#btnStatus');
    await p.waitForTimeout(3000);
    if (live) {
      await p.click('#btnVerify');
      const v = await until(p, () => $('#verifyChecks .check') && !$('#btnVerify').disabled
                                     ? [...document.querySelectorAll('#verifyChecks .check')].map(c => c.className + ' ' + c.innerText.split('\n')[1]) : null,
                            null, 5 * MIN);
      ok('Verify: every check on the live target passes', v && v.every(x => !/fail/.test(x)), (v || ['timed out']).filter(x => /fail/.test(x)).join(' | '));
    }
    drain(w, 'Deploy');
  }

  // ---------------------------------------------------------------- 4c on the live target (PostgreSQL)
  if (EXECUTE && live && PAIR === 'POSTGRESQL') {
    console.log('\nPhase 4c · zero-date decision and pre-load apply -- WRITES TO THE TARGET');
    await show(p, 'schemaddl');
    await p.evaluate(() => loadDecisions());
    const pending = await p.evaluate(() => ((DECISIONS || {}).zero_dates_pending || []));
    if (pending.length) {
      note(`recording the zero-date decision for ${pending.join(', ')} as ${APPROVER}`);
      await p.fill('#zeroDateApprover', APPROVER);
      await p.click('#btnZeroDateDecide');
      const r = await until(p, () => $('#zeroDateResult').innerText, null, MIN, 500);
      ok('4c: the decision is recorded', /Recorded against/.test(r || ''), r || 'timed out');
      await p.click('#btnSchemaDdl');
      const ddl = await until(p, () => /compiled and rolled back|not compiled/.test($('#ddlMeta').textContent) && !DDL_STALE
                                       ? $('#ddlMeta').textContent : null, null, 5 * MIN);
      ok('4c: regenerated with the decision', /compiled and rolled back/.test(ddl || ''), ddl || 'timed out');
    } else note('no zero-date decision pending');
    ok('4c: the DDL carries the decision', await p.evaluate(() => (DDL.notes || []).some(n => n.kind === 'nullable_by_decision'))
       || !(await p.evaluate(() => !!zeroDecision())));

    await planDms(p);
    const has = await p.evaluate(() => (DMSPLAN.checks.find(c => c.name === 'target_has_tables') || {}).status);
    if (has === 'pass') note('the target already has the tables; pre-load apply skipped');
    else {
      await show(p, 'schemaddl');
      await p.fill('#ddlApprover', APPROVER);
      if (await p.$eval('#ddlPostLoad', e => e.checked)) await p.uncheck('#ddlPostLoad');
      await p.click('#btnDdlApply');
      const r = await until(p, () => $('#ddlApplyResult').innerText, null, 5 * MIN);
      ok('4c: pre-load schema applied', /applied to/.test(r || ''), (r || 'timed out').slice(0, 200));
    }
    drain(w, '4c apply');
  }
  if (EXECUTE && live && PAIR === 'MYSQL') {
    console.log('\nPhase 7 · schema copy, before the load -- WRITES TO THE TARGET');
    await planDms(p);
    await p.click('#scSteps [data-sc="plan"]');
    await until(p, () => SCPLAN && !$('#scResult').innerText.includes('reading'), null, 2 * MIN, 1000);
    const has = await p.evaluate(() => (DMSPLAN.checks.find(c => c.name === 'target_has_tables') || {}).status);
    if (has === 'pass') note('the target already has the tables; the pre-load copy is skipped');
    else {
      await p.fill('#scApprover', APPROVER);
      await p.click('#scSteps [data-sc="pre"]');
      const r = await until(p, () => /applied|Stopped|Refused/.test($('#scResult').innerText) && $('#scResult').innerText, null, 5 * MIN);
      ok('Schema copy: pre-load applied', /statement\(s\) applied/.test(r || ''), (r || 'timed out').slice(0, 200));
    }
    drain(w, 'Schema copy pre');
  }

  // ---------------------------------------------------------------- Phase 7 plan (always) and run (--execute)
  console.log('\nPhase 7 · Migrate (plan)');
  const planned = await planDms(p);
  ok('Migrate: the DMS plan renders', !!planned && await p.evaluate(() => !!DMSPLAN));
  const mig = await p.evaluate(() => ({
    from: $('#dmsFromName').textContent, to: $('#dmsToName').textContent, head: $('#migHeadSub').textContent,
    res: $('#dmsResources').innerText, pwRow: $('#dmsPwRow').style.display, pwNote: $('#dmsPwNote').innerText,
    mode: $('#dmsMode').innerText, verdict: $('#dmsVerdict').innerText,
    checks: Object.fromEntries((DMSPLAN.checks || []).map(c => [c.name, c.status])),
    zd: [...document.querySelectorAll('#dmsChecks .check')].find(c => /zero dates/.test(c.innerText))?.innerText || '',
    residueShown: $('#residuePanel').style.display !== 'none', residueBtn: $('#btnResidueApply').disabled,
    residueTitle: $('#btnResidueApply').title,
    kinds: [...document.querySelectorAll('#dmsResidue summary')].map(s => s.innerText),
    sc: $('#schemaCopyPanel').style.display !== 'none',
    cdc: [...document.querySelectorAll('#dmsChecks .check')].map(c => c.innerText).filter(t => /cdc|binlog|binary log/.test(t)).join(' | '),
  }));
  ok('Migrate: the source node reads MySQL', mig.from === 'MySQL', mig.from);
  ok('Migrate: the target node names the pair', PAIR === 'MYSQL' ? mig.to === 'RDS for MySQL' : /PostgreSQL/.test(mig.to), mig.to);
  ok('Migrate: the heading is not Data Pump\'s', /both MySQL paths/.test(mig.head), mig.head);
  ok('Migrate: the source endpoint is described as MySQL', /MySQL, on its private address/.test(mig.res), mig.res.slice(0, 200));
  if (PAIR === 'MYSQL') ok('Migrate: the target endpoint is RDS for MySQL, not "PostgreSQL, SSL required"',
                           /RDS for MySQL/.test(mig.res) && !/SSL required/.test(mig.res));
  ok('Migrate: no password fields on MySQL, and a note says why', mig.pwRow === 'none' && /No passwords to type/.test(mig.pwNote));
  ok('Migrate: says what DMS changes on the way', PAIR === 'MYSQL' ? /carried unchanged/.test(mig.mode)
                                                                    : /order → order_tbl/.test(mig.mode), mig.mode.slice(0, 160));
  ok('Migrate: names the generated columns DMS does not write', /line_total/.test(mig.mode));
  ok('Migrate: the zero-date check is shown', !!mig.zd, JSON.stringify(mig.checks));
  if (PAIR === 'POSTGRESQL') ok('Migrate: the zero-date check links to the 4c decision', /Phase 4c/.test(mig.zd), mig.zd.slice(0, 160));
  ok('Migrate: CDC wording is the binary log\'s', /binary log/.test(mig.cdc) && !/archivelog|supplemental/i.test(mig.cdc), mig.cdc.slice(0, 200));
  ok('Migrate: identity residue is named for the target', mig.kinds.some(k => PAIR === 'MYSQL' ? /AUTO_INCREMENT/.test(k) : /identity/.test(k)),
     mig.kinds.slice(0, 2).join(' | '));
  ok('Migrate: the ready-residue panel is shown', mig.residueShown);
  if (!live) {
    ok('Migrate: without a target the verdict says deploy Phase 6 first', /deploy\s+Phase 6|Deploy Phase 6/.test(mig.verdict), mig.verdict.slice(0, 300));
    ok('Migrate: residue apply waits for a target, and says so', mig.residueBtn && /Deploy Phase 6 first/.test(mig.residueTitle));
    ok('Migrate: Create and run is not offered without a target', await p.$eval('#btnDmsRun', e => e.disabled || e.offsetParent === null));
  }
  if (PAIR === 'MYSQL') {
    ok('Migrate: the schema copy sequence is shown', mig.sc);
    ok('Migrate: seven steps, in order', (await p.$$('#scSteps .check')).length === 7);
    await p.click('#scSteps [data-sc="plan"]');   // SHOW CREATE on the source: read-only
    await until(p, () => SCPLAN && !/reading/.test($('#scResult').innerText), null, 2 * MIN, 1000);
    const step1 = await p.$eval('#sc-plan', e => e.innerText);
    ok('Migrate: step 1 reads the schema (19 tables)', /19 tables/.test(step1), step1.slice(0, 160));
    ok('Migrate: the copy says what it changes (DEFINER, MyISAM, triggers, events)',
       /DEFINER/.test(await p.$eval('#scNotes', e => e.innerText)) && /InnoDB/.test(await p.$eval('#scNotes', e => e.innerText)));
    if (!live) {
      const locked = await p.$$eval('#scSteps [data-sc="pre"], #scSteps [data-sc="post"], #scSteps [data-sc="cutover"]',
                                    bs => bs.map(x => x.disabled && /Deploy Phase 6 first/.test(x.title)));
      ok('Migrate: the steps that write to the target wait for one, and say so', locked.length === 3 && locked.every(Boolean));
    }
  }
  await wording(p, 'Migrate', 'migrate');
  await noSideways(p, 'Migrate', 'migrate');
  await shot(p, '7-migrate');
  drain(w, 'Migrate');

  if (EXECUTE && live) {
    console.log('\nPhase 7 · DMS create and run -- BILLS');
    const ds = await api(p, 'GET', '/api/dms/status');
    const empty = await p.evaluate(() => (DMSPLAN.checks.find(c => c.name === 'target_empty') || {}).status);
    let loaded = false;
    if (ds.status === 200 && /stopped/i.test((ds.json || {}).status || '') && ((ds.json || {}).tables || []).length) {
      note('a DMS task has already finished its full load; not loading twice'); loaded = true;
    } else if (empty === 'fail') {
      note('the target already holds rows; not loading twice (DMS would duplicate them)'); loaded = true;
    } else if (!(await p.evaluate(() => DMSPLAN.ready))) {
      ok('DMS: the preflight is ready', false, (await p.evaluate(() => DMSPLAN.refused_because || [])).join(', '));
    } else {
      note('creating the replication instance, endpoints and task, then running it');
      await p.fill('#dmsConfirm', CONFIRM);
      await p.click('#btnDmsRun');
      const done = await until(p, () => /refused|error|·/.test($('#dmsMeta').textContent) && !/creating/.test($('#dmsMeta').textContent)
                                        && $('#dmsMeta').textContent, null, 120 * MIN, 15000);
      ok('DMS: the full load completes', done && !/refused|error|suspended/.test(done), done || 'timed out');
      loaded = !!done && !/refused|error/.test(done);
    }
    drain(w, 'DMS');

    if (loaded && PAIR === 'POSTGRESQL') {
      console.log('\nPhase 4c · post-load keys and indexes -- WRITES TO THE TARGET');
      await show(p, 'schemaddl');
      await p.fill('#ddlApprover', APPROVER);
      await p.check('#ddlPostLoad');
      await p.click('#btnDdlApply');
      const r = await until(p, () => $('#ddlApplyResult').innerText, null, 10 * MIN);
      ok('4c: post-load keys and indexes applied', /applied to/.test(r || ''),
         (r || 'timed out').slice(0, 300) + (/already exists/.test(r || '') ? ' (already applied on an earlier run)' : ''));
      drain(w, '4c post-load');
    }
    if (loaded && PAIR === 'MYSQL') {
      console.log('\nPhase 7 · schema copy, after the load -- WRITES TO THE TARGET');
      await show(p, 'migrate');
      if (await p.evaluate(() => scDone('post') === 'done')) note('the post-load copy has already run; skipped');
      else {
        await p.fill('#scApprover', APPROVER);
        await p.click('#scSteps [data-sc="post"]');
        const r = await until(p, () => /applied|Stopped|Refused/.test($('#scResult').innerText) && $('#scResult').innerText, null, 5 * MIN);
        ok('Schema copy: post-load applied (triggers, events disabled)', /statement\(s\) applied/.test(r || ''), (r || 'timed out').slice(0, 200));
      }
      note('the cutover stage (enable events) is Phase 9\'s and is not run');
    }
    if (loaded) {
      console.log('\nPhase 7 · ready residue -- WRITES TO THE TARGET');
      await planDms(p);
      await p.fill('#residueApprover', APPROVER);
      await p.click('#btnResidueApply');
      const r = await until(p, () => /applied ·|Refused/.test($('#residueMeta').textContent + $('#residueResult').innerText)
                                     && ($('#residueMeta').textContent || $('#residueResult').innerText), null, 5 * MIN);
      ok('Residue: every ready item applied', / 0 failed/.test(r || ''), r || 'timed out');
      ok('Residue: the result line survives the apply (not cleared by gateResidue)',
         await p.evaluate(() => / applied · /.test($('#residueMeta').textContent)), await p.evaluate(() => $('#residueMeta').textContent));
      ok('Residue: no item marked failed', await p.evaluate(() => !$('#residueResult .check.fail')), 'a failed item is listed');
      drain(w, 'Residue');
    }
  }

  // ---------------------------------------------------------------- Phase 8
  console.log('\nPhase 8 · Validate');
  if (EXECUTE) {
    // Reach Validate the way a person does: from Migrate, by its own button, after a
    // reload. show('validate') alone hid a lock that kept a hand-run stuck on Migrate.
    await p.reload(); await p.waitForTimeout(3000);
    await show(p, 'migrate');
    const unlocked = await until(p, () => { const b = document.querySelector('#pagenav .btn:not(.ghost)');
      return b && /Validate/.test(b.textContent) && !b.disabled; }, null, 60000);
    ok("Validate: the Migrate screen's own button unlocks it after a reload", !!unlocked, 'still disabled');
  }
  await show(p, 'validate');
  await p.waitForTimeout(1500);
  if (EXECUTE && live) {
    await until(p, () => !$('#btnValidate').disabled, null, 20000, 500);
    await p.click('#btnValidate');
    const done = await until(p, () => stageState.validate !== 'busy' && /mismatch|validated|error|stopped/.test($('#valMeta').textContent)
                                      && $('#valMeta').textContent, null, 60 * MIN, 5000);
    ok('Validate: completes', !!done && !/error/.test(done), done || 'timed out');
    await p.waitForTimeout(3000);
  }
  const val = await p.evaluate(() => ({
    btn: $('#btnValidate').disabled, empty: $('#valEmpty').style.display !== 'none' ? $('#valEmpty').innerText : '',
    cross: $('#valCrossEngine').style.display !== 'none' ? $('#valCrossEngine').innerText : '',
    verdict: $('#valVerdict').style.display !== 'none' ? $('#valVerdict').innerText : '',
    status: (((window.__last) || {}).status), dec: !!zeroDecision(),
  }));
  if (!live) ok('Validate: locked without a running target, and says why',
                val.btn && /target running|Migrate first/.test(val.empty), val.empty.slice(0, 160));
  if (PAIR === 'POSTGRESQL') ok('Validate: the cross-engine note speaks MySQL', /MySQL and PostgreSQL/.test(val.cross), val.cross.slice(0, 120));
  else ok('Validate: no cross-engine note on MySQL -> MySQL', !val.cross);
  if (val.verdict && PAIR === 'POSTGRESQL' && /contract_term/.test(await p.evaluate(() => $('#valLevels').textContent))) {
    if (val.dec) ok('Validate: the contract_term mismatch is presented as the decided data loss',
                    /decided data loss/.test(val.verdict) && /decision, not a fault/.test(val.verdict), val.verdict.slice(0, 300));
    else note('the decision could not be read, so the contract_term mismatch cannot be linked to it');
  }
  await wording(p, 'Validate', 'validate');
  await noSideways(p, 'Validate', 'validate');
  await shot(p, '8-validate');
  drain(w, 'Validate');

  // ---------------------------------------------------------------- phone width
  if (PHONE) {
    console.log('\nPhone width (390x844): Phases 4c, 6, 7, 8 after a reload');
    const q = await b.newPage({viewport: {width: 390, height: 844}});
    const wq = watch(q);
    await q.goto(URL, {waitUntil: 'networkidle'});
    await until(q, () => SRC === 'MYSQL' && stageState.gate !== 'idle', null, 30000, 500);
    await q.waitForTimeout(1500);
    for (const v of ['schemaddl', 'provision', 'migrate', 'validate']) {
      if (v === 'migrate') await planDms(q); else await show(q, v);
      await q.waitForTimeout(600);
      await noSideways(q, 'phone', v);
      await q.screenshot({path: path.join(OUT, `${PAIR.toLowerCase()}-phone-${v}.png`)});
      await wording(q, 'phone', v);
    }
    drain(wq, 'phone');
    await q.close();
  }

  // ---------------------------------------------------------------- Oracle spot check
  console.log('\nOracle wording is unchanged (in-page; the server is not touched)');
  const ora = await p.evaluate(() => {
    SRC = 'ORACLE'; applyPairWording(); renderRail();
    const out = {
      head: $('#migHeadSub').textContent, from: $('#dmsFromName').textContent, to: $('#dmsToName').textContent,
      cross: $('#valCrossEngine .note').innerText, pwRow: $('#dmsPwRow').style.display,
      pwNote: $('#dmsPwNote').style.display,
      tile: provEngineTile({engine: 'oracle-ee', provenance: []}),
      cdc: dmsCheckView({name: 'cdc_archivelog', status: 'pass', detail: 'source log mode ARCHIVELOG'}).detail,
      kind: residueKind({kind: 'sequence'}), hint: $('#dmsHint').title,
      ddlTarget: (renderDdlApplyTarget(), $('#ddlApplyTarget').innerHTML),
    };
    SRC = 'MYSQL'; applyPairWording(); renderRail(); renderDdlApplyTarget();
    return out;
  });
  ok('Oracle: the Migrate heading names Data Pump', /Data Pump for Oracle/.test(ora.head), ora.head);
  ok('Oracle: the source node reads Oracle', ora.from === 'Oracle', ora.from);
  ok('Oracle: the target node reads RDS PostgreSQL', ora.to === 'RDS PostgreSQL', ora.to);
  ok('Oracle: the Phase 8 note is Oracle and PostgreSQL', /Oracle and PostgreSQL/.test(ora.cross), ora.cross.slice(0, 80));
  ok('Oracle: the password fields are back', ora.pwRow === '' && ora.pwNote === 'none');
  ok('Oracle: the engine tile is EE 19c', ora.tile === 'EE<small>19c</small>', ora.tile);
  ok('Oracle: CDC checks keep the server\'s words', ora.cdc === 'source log mode ARCHIVELOG');
  ok('Oracle: a sequence is a sequence', ora.kind === 'sequence');
  ok('Oracle: the DMS hint still contrasts Data Pump', /Data Pump/.test(ora.hint));
  ok('Oracle: the 4c apply panel has no MySQL target note', ora.ddlTarget === '');
  drain(w, 'Oracle spot check');

  await b.close();
  console.log(`\n${pass} passed, ${fail} failed${known ? `, ${known} known server defect(s) reported` : ''}`);
  if (EXECUTE) console.log('\nThe RDS target and the DMS replication instance BILL until destroyed. '
    + 'Check Phase 6 -> Deploy -> Kill switch when you are done; this harness never destroys anything.');
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
