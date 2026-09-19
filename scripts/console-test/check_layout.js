// Measures the console shell and the action-item row geometry at several
// widths. Loads index.html as a file:// page -- no server, no database: the
// question is CSS, and an unpopulated page still lays out its grid.
const { chromium } = require('playwright-core');
const path = require('path');

const HTML = 'file:///' + path.resolve(process.argv[2]).replace(/\\/g, '/');

(async () => {
  const b = await chromium.launch({ channel: 'msedge' });
  for (const w of [400, 860, 1180, 1440, 1920]) {
    const pg = await b.newPage({ viewport: { width: w, height: 900 } });
    await pg.goto(HTML);

    // Build one action-item row with realistic content and measure the columns.
    const r = await pg.evaluate(() => {
      const cs = getComputedStyle(document.documentElement);
      const main = document.querySelector('main');

      const probe = document.createElement('div');
      probe.className = 'sctrow';
      probe.innerHTML =
        '<span class="sev HIGH">a person</span>' +
        '<span class="mono">5581</span>' +
        '<span>PostgreSQL doesn\'t support index-organized tables</span>' +
        '<span class="mono obj">COMM_LOG.IX_COMM_NOTES_CTX</span>' +
        '<span class="act">Assign to an engineer</span>';
      main.appendChild(probe);
      const cols = getComputedStyle(probe).gridTemplateColumns;
      const titleEl = probe.children[2];
      const titleH = titleEl.getBoundingClientRect().height;
      const lineH = parseFloat(getComputedStyle(titleEl).lineHeight) || 20;
      probe.remove();

      return {
        shell: cs.getPropertyValue('--shell').trim() || '(undefined)',
        mainW: Math.round(main.getBoundingClientRect().width),
        cols,
        titleLines: Math.round(titleH / lineH),
        hOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
        // The content pane is overflow-x:hidden now, so anything too wide is
        // clipped rather than pushing the document out -- the check above
        // would stay green while a row was cut off. Ask the pane directly.
        paneOverflow: main.scrollWidth > main.clientWidth + 1,
      };
    });

    console.log(
      `${String(w).padStart(5)}px  shell=${r.shell.padEnd(8)} main=${String(r.mainW).padEnd(5)}` +
      ` titleLines=${r.titleLines}  h-overflow=${r.hOverflow}  pane-overflow=${r.paneOverflow}`);
    console.log(`         columns: ${r.cols}`);
    await pg.close();
  }
  await b.close();
})();
