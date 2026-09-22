// Walks every phase against a live console and looks for layout that is
// actually broken, not layout that is merely ugly:
//
//   * an element painting on top of unrelated text
//   * a container whose children have collapsed to zero size -- this is what
//     a missing CSS rule looks like, and it spills its text over whatever is
//     behind it
//   * text running outside the box that is supposed to hold it
//   * the page or the pane scrolling sideways
//
// Needs the console up on 127.0.0.1:8765 with a run already done, because the
// empty states hide every one of these faults.
const { chromium } = require('playwright-core');
const path = require('path');
const fs = require('fs');

const URL = process.env.DBSHIFT_URL || 'http://127.0.0.1:8765';
const OUT = path.join(__dirname, 'shots-overlap');
const W = +(process.env.W || 1440), H = +(process.env.H || 900);

let PASS = 0, FAIL = 0;
const log = (label, ok, detail) => {
  (ok ? PASS++ : FAIL++);
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? '  -- ' + detail : ''}`);
};

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const b = await chromium.launch({ channel: 'msedge' });
  const p = await b.newPage({ viewport: { width: W, height: H } });
  const jsErrors = [];
  p.on('pageerror', e => jsErrors.push(e.message));
  p.on('console', m => { if (m.type() === 'error') jsErrors.push(m.text()); });

  await p.goto(URL);
  await p.waitForTimeout(1200);

  // Unlock everything so each phase can be opened without re-running the
  // pipeline. The data that is already there stays there.
  const views = await p.evaluate(() => {
    Object.keys(stageState).forEach(k => { if (stageState[k] === 'idle') stageState[k] = 'done'; });
    renderRail();
    return [...document.querySelectorAll('.view')].map(v => v.id.replace('view-', ''));
  });

  for (const view of views) {
    await p.evaluate(v => show(v), view);
    await p.waitForTimeout(450);

    const r = await p.evaluate(() => {
      // An element inside a display:none subtree is zero-sized on purpose --
      // every view here hides its result panels until a phase has run. Only
      // things the eye can actually reach are worth reporting.
      // Content inside a closed <details> still reports a box in Chromium --
      // it is not rendered, but it measures as if it were, and every such
      // element looks like it is lying on top of its neighbours. Only the
      // <summary> of a closed accordion is actually on screen.
      const inClosedDetails = el => {
        for (let d = el.closest('details'); d; d = d.parentElement?.closest('details')) {
          if (!d.open && !el.closest('summary')) return true;
        }
        return false;
      };
      const painted = el =>
        !!(el.offsetParent || el.getClientRects().length) && !inClosedDetails(el);

      // True when an element lies outside the visible area of a scrolling or
      // clipping ancestor -- it is measured, but nobody can see it there.
      const clippedAway = (el, box) => {
        for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
          const s = getComputedStyle(a);
          if (s.overflow === 'visible' && s.overflowY === 'visible' &&
              s.overflowX === 'visible') continue;
          const c = a.getBoundingClientRect();
          if (c.width < 2 || c.height < 2) continue;
          if (box.bottom <= c.top + 1 || box.top >= c.bottom - 1 ||
              box.right <= c.left + 1 || box.left >= c.right - 1) return true;
        }
        return false;
      };
      const vis = el => {
        if (!painted(el)) return false;
        const s = getComputedStyle(el);
        if (s.visibility === 'hidden' || +s.opacity === 0) return false;
        const b = el.getBoundingClientRect();
        return b.width > 0 || b.height > 0 || el.textContent.trim().length > 0;
      };
      const on = document.querySelector('.view.on');
      const name = el => el.id ? '#' + el.id
        : el.className && typeof el.className === 'string'
          ? el.tagName.toLowerCase() + '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.')
          : el.tagName.toLowerCase();

      // 1. A container whose visible children are all zero-sized. That is a
      //    missing layout rule, and the text inside escapes its box.
      const collapsed = [];
      for (const el of on.querySelectorAll('*')) {
        const kids = [...el.children];
        if (kids.length < 1 || !painted(el)) continue;
        // The container must itself be on screen with a real box, or this is
        // just a hidden panel and its children are meant to be zero.
        const eb = el.getBoundingClientRect();
        if (eb.width < 2 && eb.height < 2) continue;
        const withText = kids.filter(k => k.textContent.trim().length > 0 && painted(k));
        if (!withText.length) continue;
        const allZero = withText.every(k => {
          const b = k.getBoundingClientRect();
          return b.width < 1 && b.height < 1;
        });
        if (allZero) collapsed.push({ el: name(el), kids: withText.length,
                                      display: getComputedStyle(el).display });
      }

      // 2. Text that paints outside its own parent's box.
      // Sideways only. A block growing taller than its parent is ordinary
      // flow -- an open <details>, a list inside a scroller -- but text
      // reaching outside the left or right edge is always wrong.
      const escaping = [];
      for (const el of on.querySelectorAll('*')) {
        if (!vis(el) || !el.textContent.trim()) continue;
        const par = el.parentElement;
        if (!par || par === on) continue;
        const ps = getComputedStyle(par);
        if (ps.overflow !== 'visible' || ps.position === 'absolute') continue;
        const a = el.getBoundingClientRect(), b = par.getBoundingClientRect();
        if (b.width < 2 || b.height < 2) continue;
        const outX = Math.max(0, b.left - a.left) + Math.max(0, a.right - b.right);
        if (outX > 12) {
          escaping.push({ el: name(el), parent: name(par),
                          outX: Math.round(outX), outY: 0,
                          text: el.textContent.trim().slice(0, 40) });
        }
      }

      // 3. Two leaf texts painting over each other. Only leaves are compared,
      //    so a parent legitimately containing a child is not reported.
      const leaves = [...on.querySelectorAll('*')].filter(el =>
        vis(el) && painted(el) && el.children.length === 0 &&
        el.textContent.trim().length > 1);
      const boxes = leaves.map(el => ({ el, b: el.getBoundingClientRect() }))
        .filter(x => x.b.width > 4 && x.b.height > 4);
      const overlaps = [];
      for (let i = 0; i < boxes.length; i++) {
        for (let j = i + 1; j < boxes.length; j++) {
          const A = boxes[i], B = boxes[j];
          if (A.el.contains(B.el) || B.el.contains(A.el)) continue;
          // Cells of one table share edges by construction; a sticky header
          // sliding over its own body is the feature, not a fault.
          const tA = A.el.closest('table'), tB = B.el.closest('table');
          if (tA && tA === tB) continue;
          // A row scrolled out of a clipping box still measures where it
          // would have been, so it appears to lie over whatever follows the
          // box. Compare against the clip, not the ghost.
          if (clippedAway(A.el, A.b) || clippedAway(B.el, B.b)) continue;
          const ox = Math.min(A.b.right, B.b.right) - Math.max(A.b.left, B.b.left);
          const oy = Math.min(A.b.bottom, B.b.bottom) - Math.max(A.b.top, B.b.top);
          if (ox > 6 && oy > 6) {
            overlaps.push({ a: name(A.el), b: name(B.el),
                            area: Math.round(ox * oy),
                            at: A.el.textContent.trim().slice(0, 28) + ' / ' +
                                B.el.textContent.trim().slice(0, 28) });
          }
        }
      }
      overlaps.sort((x, y) => y.area - x.area);

      const main = document.querySelector('main');
      return {
        collapsed, escaping, overlaps: overlaps.slice(0, 6),
        paneH: main.scrollWidth > main.clientWidth + 1,
        docH: document.documentElement.scrollWidth > window.innerWidth + 1,
      };
    });

    const bad = r.collapsed.length || r.escaping.length || r.overlaps.length ||
                r.paneH || r.docH;
    log(`${view}`, !bad,
        bad ? [
          r.collapsed.length ? `${r.collapsed.length} collapsed` : '',
          r.overlaps.length ? `${r.overlaps.length} overlap` : '',
          r.escaping.length ? `${r.escaping.length} escaping` : '',
          r.paneH ? 'pane scrolls sideways' : '',
          r.docH ? 'page scrolls sideways' : '',
        ].filter(Boolean).join(', ') : '');

    for (const c of r.collapsed)
      console.log(`        collapsed: ${c.el} (display:${c.display}) has ${c.kids} child(ren) at zero size`);
    for (const o of r.overlaps)
      console.log(`        overlap:   ${o.a} over ${o.b} -- "${o.at}"`);
    for (const e of r.escaping)
      console.log(`        escaping:  ${e.el} out of ${e.parent} by ${e.outX}x${e.outY}px -- "${e.text}"`);

    if (bad) await p.screenshot({ path: path.join(OUT, `bad-${view}.png`), fullPage: false });
  }

  log('no browser console errors', jsErrors.length === 0, jsErrors.slice(0, 2).join(' | '));
  console.log(`\n${PASS}/${PASS + FAIL} phases clean at ${W}x${H}`);
  await b.close();
  process.exit(FAIL ? 1 : 0);
})();
