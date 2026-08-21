// DISPOSABLE RECON PROBE - not production code.
// Loads Rowan Announcer pages in Chromium and records all network activity.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const OUT = '/home/sedlock/src/DailyMail/artifacts/reconnaissance';
const BASE = 'https://apps.rowan.edu/RowanAnnouncer';

// TLS note: apps.rowan.edu serves an incomplete chain (missing InCommon
// intermediate). Chain was independently verified genuine via openssl +
// AIA-fetched intermediate before enabling this flag.
const CTX = { ignoreHTTPSErrors: true };

async function visit(browser, label, url, opts = {}) {
  const ctx = await browser.newContext(CTX);
  const page = await ctx.newPage();
  const events = [];

  page.on('request', r => {
    events.push({ phase: 'request', method: r.method(), url: r.url(),
      resourceType: r.resourceType(), postData: r.postData() || null,
      headers: r.headers() });
  });
  page.on('response', async r => {
    const rec = { phase: 'response', status: r.status(), url: r.url(),
      headers: r.headers(), body: null };
    const ct = (r.headers()['content-type'] || '');
    if (ct.includes('json') || ct.includes('text/plain')) {
      try { rec.body = await r.text(); } catch (e) { rec.body = '<unavailable>'; }
    }
    events.push(rec);
  });

  const resp = await page.goto(url, { waitUntil: 'networkidle', timeout: 90000 }).catch(e => { console.log('GOTO ERR', e.message); return null; });
  await page.waitForTimeout(opts.wait || 4000);

  const html = await page.content();
  fs.writeFileSync(path.join(OUT, 'html', `${label}.html`), html);
  fs.writeFileSync(path.join(OUT, 'network', `${label}.json`), JSON.stringify({ url, events }, null, 2));
  if (opts.screenshot) {
    await page.screenshot({ path: path.join(OUT, 'screenshots', `${label}.png`), fullPage: true });
  }

  // Concise summary
  const xhr = events.filter(e => e.phase === 'request' && ['xhr','fetch'].includes(e.resourceType));
  console.log(`\n===== ${label} =====`);
  console.log(`doc status: ${resp ? resp.status() : 'n/a'}  html bytes: ${html.length}`);
  console.log(`total events: ${events.length}  XHR/fetch requests: ${xhr.length}`);
  const seen = new Set();
  for (const r of xhr) {
    const u = r.url.replace(BASE, '');
    if (seen.has(r.method + u)) continue;
    seen.add(r.method + u);
    console.log(`  ${r.method} ${u}`);
  }
  const title = await page.title().catch(()=>'');
  console.log(`title: ${title}`);
  const text = await page.evaluate(() => document.body.innerText).catch(()=>'');
  fs.writeFileSync(path.join(OUT, 'html', `${label}.txt`), text);
  console.log(`--- visible text (first 1200) ---\n${text.slice(0,1200)}`);
  await ctx.close();
  return { events, html, text };
}

(async () => {
  const browser = await chromium.launch();
  const targets = [
    ['employee-today', `${BASE}/Home?Audience=Employees`, { screenshot: true }],
    ['student-today',  `${BASE}/Home?Audience=Students`, { screenshot: true }],
  ];
  for (const [label, url, opts] of targets) {
    await visit(browser, label, url, opts);
  }
  await browser.close();
})();
