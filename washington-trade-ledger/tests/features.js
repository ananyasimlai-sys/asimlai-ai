// Browser checks for the site: search chips, the bought/sold quick view, person pages and downloads.
//
//   cd washington-trade-ledger && python3 -m http.server 8000      (in one terminal)
//   npm install playwright && npx playwright install chromium       (once)
//   node tests/features.js                                          (in another)
//
// Environment:
//   WTL_URL     page to test (default http://127.0.0.1:8000/index.html)
//   WTL_VIEWER  set to 1 to imitate the claude.ai viewer's download capability instead of plain browser downloads
//   WTL_VENDOR  folder holding node_modules/jspdf and node_modules/jspdf-autotable, to test PDFs without the CDN
//   WTL_SHOTS   folder to save screenshots in
// The expectations are written against the data snapshot in data/; most hold for any day's data.
const { chromium } = require('playwright'); const fs = require('fs'), path = require('path');
const URL = process.env.WTL_URL || 'http://127.0.0.1:8000/index.html';
const VIEWER = !!process.env.WTL_VIEWER, VENDOR = process.env.WTL_VENDOR || '', SHOTS = process.env.WTL_SHOTS || '';
const out = [], fail = [];
const ok = (cond, msg) => { (cond ? out : fail).push((cond ? 'ok   ' : 'FAIL ') + msg); };
(async () => {
  const b = await chromium.launch();
  for (const c of [{ n: 'desk', w: 1280, h: 900, s: 'light' }, { n: 'phone', w: 390, h: 844, s: 'light' }, { n: 'phone-dark', w: 390, h: 844, s: 'dark' }, { n: 'desk-dark', w: 1280, h: 900, s: 'dark' }]) {
    const ctx = await b.newContext({ viewport: { width: c.w, height: c.h }, colorScheme: c.s, hasTouch: c.n.startsWith('phone'), acceptDownloads: true });
    if (VENDOR) await ctx.route('**/cdnjs.cloudflare.com/**', r => r.fulfill({ status: 200, contentType: 'application/javascript', body: fs.readFileSync(r.request().url().includes('autotable') ? path.join(VENDOR, 'node_modules/jspdf-autotable/dist/jspdf.plugin.autotable.min.js') : path.join(VENDOR, 'node_modules/jspdf/dist/jspdf.umd.min.js')) }));
    await ctx.route('**/fonts.googleapis.com/**', r => r.abort());
    const p = await ctx.newPage(); const errs = [], files = [];
    if (VIEWER) await p.addInitScript(() => { window.__saved = []; window.claude = { use: async n => n === 'downloads' ? { save: async o => { window.__saved.push(o.filename); return { status: 'saved' }; } } : null }; });
    else p.on('download', d => files.push(d.suggestedFilename()));
    const lastSaved = async () => VIEWER ? p.evaluate(() => window.__saved.slice(-1)[0]) : files[files.length - 1];
    const shot = async (name, opts) => { if (SHOTS) await p.screenshot(Object.assign({ path: path.join(SHOTS, name + '-' + c.n + '.png') }, opts || {})); };
    p.on('console', m => { if (m.type() === 'error' && !/ERR_FAILED|Failed to load resource/.test(m.text())) errs.push(m.text()); }); p.on('pageerror', e => errs.push('PAGEERROR ' + e.message));
    const T = c.n + ': ';
    await p.goto(URL + '#overview', { waitUntil: 'networkidle' }); await p.waitForTimeout(400);
    const hash = () => p.evaluate(() => location.hash);
    const overflow = async where => { const o = await p.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]); ok(o[0] <= o[1], T + where + ' no sideways scroll ' + o.join('/')); };
    ok(await p.title() === 'Washington Trade Ledger', T + 'title');
    await overflow('overview');
    await shot('overview');
    // scope chips under the search box
    const scopes = await p.$$eval('#q-scopes .chip', a => a.map(x => [x.textContent, x.getAttribute('aria-pressed')]));
    ok(scopes.length === 5 && scopes[0][1] === 'true', T + 'scope chips ' + JSON.stringify(scopes));
    // focus opens the suggestion chips
    await p.click('#q'); await p.waitForTimeout(150);
    const groups = await p.$$eval('#q-sugg .sg', g => g.map(x => [x.querySelector('h4').textContent, x.querySelectorAll('.chip').length]));
    ok(!(await p.$eval('#q-panel', x => x.hidden)) && groups.length === 4, T + 'panel on focus ' + JSON.stringify(groups));
    ok(await p.$eval('#q', x => x.getAttribute('aria-expanded')) === 'false', T + 'aria-expanded false while only chips show');
    await shot('search');
    const pw = await p.$eval('#q-panel', x => { const r = x.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.right), innerWidth]; });
    ok(pw[0] >= 0 && pw[1] <= pw[2], T + 'panel inside the window ' + pw.join('/'));
    // keyboard: Tab from the box reaches the chips; Enter on a chip navigates
    await p.focus('#q'); await p.waitForTimeout(100);
    for (let k = 0; k < 6; k++) await p.keyboard.press('Tab');
    const kf = await p.evaluate(() => [document.activeElement.className, document.activeElement.textContent, !document.querySelector('#q-panel').hidden]);
    ok(kf[0] === 'chip' && kf[2], T + 'Tab reaches a suggestion chip: ' + JSON.stringify(kf));
    await p.keyboard.press('Enter'); await p.waitForTimeout(400);
    ok((await hash()).startsWith('#p-') && await p.$eval('#q-panel', x => x.hidden), T + 'Enter on chip -> ' + await hash());
    // People scope
    await p.click('#q'); await p.waitForTimeout(120);
    await p.click('#q-scopes .chip[data-s="p"]'); await p.waitForTimeout(150);
    const pg = await p.$$eval('#q-sugg .sg', g => g.map(x => [x.querySelector('h4').textContent, Array.from(x.querySelectorAll('.chip')).map(c => c.textContent)]));
    ok(pg.length === 2 && pg[0][1].length === 12 && pg[1][1].includes('Senators'), T + 'people scope ' + JSON.stringify(pg).slice(0, 160));
    ok((await p.$eval('#q', x => x.placeholder)).startsWith('Search people'), T + 'placeholder follows scope');
    const firstName = pg[0][1][0];
    await p.click('#q-sugg .sg .chip'); await p.waitForTimeout(400);
    ok((await hash()).startsWith('#p-') && (await p.$eval('h1', x => x.textContent)) === firstName, T + 'person chip opens ' + firstName);
    ok(await p.$eval('#q-panel', x => x.hidden) && await p.$eval('#q-scopes .chip[data-s="all"]', x => x.getAttribute('aria-pressed')) === 'true', T + 'panel closed and scope reset after pick');
    // person page: what they bought | what they sold
    const ps = await p.$$eval('.bs-c', cs => cs.map(c => [c.querySelector('h3').textContent, c.querySelectorAll('.bs-i').length]));
    ok(ps.length === 2 && ps[0][0] === 'What they bought' && ps[1][0] === 'What they sold' && ps[0][1] <= 10, T + 'person page lists ' + JSON.stringify(ps));
    const more = await p.$('.bs-more:not([hidden])');
    if (more) { const before = await p.$$eval('.bs-i', r => r.length); await more.click(); await p.waitForTimeout(150); const after = await p.$$eval('.bs-i', r => r.length); ok(after > before, T + 'show more ' + before + ' -> ' + after); }
    await overflow('person page');
    // typing: exact ticker first, Enter opens it
    await p.click('#q'); await p.fill('#q', 'nvda'); await p.waitForTimeout(150);
    ok(await p.$eval('#q', x => x.getAttribute('aria-expanded')) === 'true', T + 'aria-expanded true with matches');
    const r1 = await p.$$eval('#q-results .opt', a => a.map(x => x.textContent));
    ok(/Nvidia/.test(r1[0] || '') && /Search every trade/.test(r1[r1.length - 1]), T + 'nvda results ' + JSON.stringify(r1).slice(0, 160));
    await p.keyboard.press('Enter'); await p.waitForTimeout(500);
    ok((await hash()) === '#s-NVDA', T + 'Enter opens NVDA');
    // a sector opens the Stocks tab filtered to it
    await p.click('#q'); await p.fill('#q', 'energy'); await p.waitForTimeout(150);
    const r2 = await p.$$eval('#q-results .opt', a => a.map(x => x.textContent));
    const si = r2.findIndex(t => /^Energy.*Sector/.test(t));
    ok(si >= 0, T + 'energy shows the sector');
    if (si >= 0) { await p.click(`#q-opt-${si}`); await p.waitForTimeout(400); ok((await hash()) === '#stocks' && await p.$eval('#stocks-sec', x => x.value) === 'Energy' && (await p.$$eval('tbody tr', r => r.length)) > 0, T + 'sector opens Stocks filtered'); }
    // "search every trade", chosen with the arrow keys
    await p.click('#q'); await p.fill('#q', 'lockheed'); await p.waitForTimeout(150);
    const n3 = await p.$$eval('#q-results .opt', a => a.length);
    for (let k = 0; k < n3; k++) await p.keyboard.press('ArrowDown');
    ok(await p.$eval('#q', x => x.getAttribute('aria-activedescendant')) === 'q-opt-' + (n3 - 1), T + 'arrow keys reach the last option');
    await p.keyboard.press('Enter'); await p.waitForTimeout(500);
    ok((await hash()) === '#trades' && await p.$eval('#trades-q', x => x.value) === 'lockheed', T + 'search every trade -> Trades tab');
    // a quick filter applied while already on the Trades tab
    await p.click('#q'); await p.waitForTimeout(120);
    await p.click('#q-sugg .chip:text("Over $1M")'); await p.waitForTimeout(400);
    ok(await p.$eval('#trades-min', x => x.value) === '1000001' && await p.$eval('#trades-q', x => x.value) === '', T + 'Over $1M quick filter');
    // a scope with no match offers to search everything
    await p.click('#q-scopes .chip[data-s="s"]'); await p.fill('#q', 'pelosi'); await p.waitForTimeout(150);
    ok(/No stock matches/.test(await p.$eval('#q-results .none', x => x.textContent).catch(() => '')), T + 'no-match message');
    await p.click('#q-results .none .linkish'); await p.waitForTimeout(150);
    ok(/Pelosi/.test((await p.$$eval('#q-results .opt', a => a.map(x => x.textContent)))[0] || ''), T + 'search everything finds Pelosi');
    await p.keyboard.press('Escape'); await p.waitForTimeout(100);
    ok(await p.$eval('#q-panel', x => x.hidden), T + 'Escape closes the panel');
    await p.click('#q'); await p.waitForTimeout(100); await p.mouse.click(5, c.h - 5); await p.waitForTimeout(100);
    ok(await p.$eval('#q-panel', x => x.hidden), T + 'click outside closes the panel');
    await p.fill('#q', ''); await p.keyboard.press('Escape');

    // quick view from Most active
    await p.goto(URL + '#overview', { waitUntil: 'networkidle' }); await p.waitForTimeout(400);
    await p.evaluate(() => { const b = document.querySelector('#f-period-all'); if (b) b.click(); });
    await p.waitForTimeout(200);
    const rows = await p.$$('.dv-row.click');
    ok(rows.length === 12, T + 'most active rows clickable: ' + rows.length);
    const nm = await rows[2].$eval('.dv-lab', x => x.firstChild.textContent);
    await rows[2].scrollIntoViewIfNeeded(); await rows[2].click(); await p.waitForTimeout(300);
    const dlg = await p.evaluate(() => { const d = document.querySelector('#pv'); return { open: d.open, name: d.querySelector('h2').textContent, tiles: d.querySelectorAll('.tile').length, cols: Array.from(d.querySelectorAll('.bs-c')).map(c => c.querySelectorAll('.bs-i').length), modal: document.documentElement.classList.contains('modal') }; });
    ok(dlg.open && dlg.name === nm && dlg.tiles === 3 && dlg.cols.length === 2 && dlg.modal, T + 'quick view for ' + nm + ' ' + JSON.stringify(dlg));
    await shot('quick-view');
    const box = await p.$eval('#pv', d => { const r = d.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.width)]; });
    ok(box[0] >= 0 && box[1] <= c.w, T + 'dialog fits the window ' + box.join('/'));
    const bars = await p.$$eval('#pv .bs .bar', b => b.map(x => +getComputedStyle(x).getPropertyValue('--w')));
    ok(bars.length > 0 && Math.max(...bars) === 1, T + 'bars share one scale');
    await p.click('#pv .seg button:text("6 months")'); await p.waitForTimeout(250);
    const d2 = await p.evaluate(() => ({ sub: document.querySelector('#pv .pv-b .sub, #pv .pv-b .empty').textContent, pressed: document.querySelector('#pv .seg button[aria-pressed="true"]').textContent }));
    ok(d2.pressed === '6 months' && /six months/.test(d2.sub), T + 'period switch in dialog');
    const dl = await p.$('#pv .pv-f .dl button');
    ok(!!dl, T + 'dialog offers a CSV download');
    if (dl) { await dl.click(); await p.waitForTimeout(800); const saved = await lastSaved(); ok(/^trades-.*\.csv$/.test(saved || ''), T + 'dialog CSV ' + saved); }
    await p.keyboard.press('Escape'); await p.waitForTimeout(300);
    const after = await p.evaluate(() => ({ open: document.querySelector('#pv').open, key: document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.key : null, bar: document.querySelector('#filters [data-f="period"] button[aria-pressed="true"]').textContent, modal: document.documentElement.classList.contains('modal'), tip: !document.querySelector('#tip').hidden }));
    ok(!after.open && after.key && after.bar === '6 months' && !after.modal && !after.tip, T + 'closed: focus back on the row, period kept ' + JSON.stringify(after));
    await p.focus('.dv-row.click'); await p.keyboard.press('Enter'); await p.waitForTimeout(250);
    ok(await p.$eval('#pv', d => d.open) && await p.evaluate(() => document.activeElement.closest('#pv') !== null), T + 'Enter opens, focus inside dialog');
    if (!c.n.startsWith('phone')) { await p.mouse.click(8, 8); await p.waitForTimeout(250); ok(!(await p.$eval('#pv', d => d.open)), T + 'backdrop click closes'); }
    else { await p.click('#pv .pv-x'); await p.waitForTimeout(250); ok(!(await p.$eval('#pv', d => d.open)), T + 'close button closes'); }
    await p.focus('.dv-row.click'); await p.keyboard.press(' '); await p.waitForTimeout(250);
    const pname = await p.$eval('#pv h2', x => x.textContent);
    await p.click('#pv .pv-f a.btn.primary'); await p.waitForTimeout(500);
    ok(!(await p.$eval('#pv', d => d.open)) && (await hash()).startsWith('#p-') && (await p.$eval('h1', x => x.textContent)) === pname, T + 'Open full profile');

    // daily reports: read and download one
    await p.goto(URL + '#reports', { waitUntil: 'networkidle' }); await p.waitForTimeout(400);
    const csvBtn = await p.$('figure .dl button:text("CSV")');
    ok(!!csvBtn && await csvBtn.isVisible(), T + 'latest report offers downloads');
    if (csvBtn) { await csvBtn.click(); await p.waitForTimeout(800); ok(/^washington-trade-ledger-\d{4}-\d{2}-\d{2}\.csv$/.test(await lastSaved() || ''), T + 'report CSV ' + await lastSaved()); }
    if (VENDOR || !process.env.WTL_OFFLINE) {
      const pdfBtn = await p.$('figure .dl button:text("PDF")');
      if (pdfBtn) { await pdfBtn.click(); await p.waitForTimeout(2500); ok(/^washington-trade-ledger-\d{4}-\d{2}-\d{2}\.pdf$/.test(await lastSaved() || ''), T + 'report PDF ' + await lastSaved()); }
    }
    ok(await p.$eval('.nodl', n => n.hidden).catch(() => true), T + 'no "downloads unavailable" note');
    await p.evaluate(() => { try { localStorage.clear(); } catch (e) {} });
    ok(errs.length === 0, T + 'console errors ' + JSON.stringify(errs));
    await ctx.close();
  }
  await b.close();
  console.log(out.join('\n')); if (fail.length) { console.log(fail.join('\n')); process.exitCode = 1; } else console.log('ALL OK');
})();
