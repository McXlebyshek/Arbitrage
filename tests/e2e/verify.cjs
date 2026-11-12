const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {mkdtempSync, rmdirSync, unlinkSync, writeFileSync} = require('node:fs');
const {join, resolve} = require('node:path');
const {tmpdir} = require('node:os');
const {spawn} = require('node:child_process');
const assert = require('node:assert/strict');
const {NOW, snapshot, quotes} = require('../fixtures/performance.cjs');
const data = snapshot(200);
const exchanges = ['Binance', 'Bybit', 'OKX', 'Bitget', 'Gate', 'MEXC'];
for (const legs of Object.values(data.markets)) for (const leg of legs) leg.exchange = exchanges[Number(leg.exchange.slice(5))];

(async () => {
  const temp = mkdtempSync(join(tmpdir(), 'arbitrage-e2e-'));
  const fixture = join(temp, 'markets.json');
  writeFileSync(fixture, JSON.stringify(data));
  const python = process.env.ARBITRAGE_PYTHON || resolve('.venv', 'Scripts', 'python.exe');
  const child = spawn(python, ['-B', join(__dirname, 'server.py'), fixture], {windowsHide: true});
  let browser, url;
  const errors = [], statuses = [];
  let metadataRequests = 0;
  child.stderr.on('data', data => process.stderr.write(data));
  try {
    const port = await new Promise((resolve, reject) => {
      child.on('error', reject); child.on('exit', code => reject(new Error('server exited ' + code)));
      child.stdout.on('data', data => {const match = /PORT=(\d+)/.exec(data.toString()); if (match) resolve(match[1]);});
    });
    url = 'http://127.0.0.1:' + port;
    browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined});
    const page = await browser.newPage({viewport: {width: 1440, height: 960}});
    await page.addInitScript(now => {window.testNow = now; Date.now = () => window.testNow;}, NOW);
    page.on('pageerror', error => errors.push(error.message));
    page.on('response', response => {
      const path = new URL(response.url()).pathname;
      if (path === '/api/markets') statuses.push(response.status());
      if (path.startsWith('/api/meta/')) metadataRequests++;
    });
    const home = await page.goto(url + '/#/arbitrage?mode=spread&sort=entry');
    assert.equal(home.headers()['x-content-type-options'], 'nosniff');
    assert.match(home.headers()['content-security-policy'], /default-src 'self'/);
    const invalidHost = await page.request.get(url + '/api/health', {headers: {Host: 'example.test'}});
    assert.equal(invalidHost.status(), 400);
    await page.waitForFunction(() => live.pairs.length === 13200 && document.querySelectorAll('#market-table tbody tr').length > 1);
    const initial = await page.locator('#result-count').innerText();
    assert.match(initial, /200/);
    await page.selectOption('#nominal', '100');
    await page.getByRole('button', {name: 'Спред по глубине'}).click();
    await page.waitForFunction(() => [...document.querySelectorAll('[data-executable]')]
      .some(cell => cell.textContent.includes('%')));
    await page.evaluate(() => {window.untouchedPair = live.fromId('TEST0199.2~TEST0199.3');});
    await page.request.post(url + '/__test/quotes', {data: quotes(data, 30)});
    await page.waitForFunction(() => live.fromId('TEST0000.0~TEST0000.1')?.long.quoteSource === 'ws');
    assert.equal(await page.evaluate(() => live.fromId('TEST0199.2~TEST0199.3') === window.untouchedPair), true);
    await page.evaluate(async () => {await refreshMarkets();});
    assert.equal(statuses.at(-1), 304);
    const focused = page.locator('#market-table tbody [data-coin-row] .coin-cell a')
      .filter({hasText: /^TEST(?:00[3-9]\d|01\d\d)$/}).first();
    await focused.focus();
    await page.evaluate(() => {window.focusedBeforePublish = document.activeElement;});
    const beforePublish = statuses.length;
    await page.request.post(url + '/__test/publish');
    await page.waitForFunction(now => live.generatedAt === now + 1, NOW);
    assert.ok(statuses.slice(beforePublish).includes(200));
    assert.equal(await page.evaluate(() => live.generatedAt), NOW + 1);
    assert.equal(await page.evaluate(() => document.activeElement === window.focusedBeforePublish), true);
    assert.equal(await page.evaluate(() => live.fromId('TEST0000.0~TEST0000.1').long.quoteSource), undefined);
    // Exercise native UI sorting and grouped/expanded routes.
    await page.locator('[data-action="sort"][data-key="entry"]').click();
    assert.equal(await page.evaluate(() => state.direction), 'asc');
    await page.evaluate(() => {location.hash = '/arbitrage?mode=spread&coin=TEST0001';});
    await page.waitForFunction(() => state.query === 'TEST0001');
    assert.match(await page.locator('#result-count').innerText(), /1$/);
    await page.locator('#market-table tbody .coin-cell a').first().click();
    await page.waitForFunction(() => activePair?.coin === 'TEST0001');
    await page.waitForFunction(() => live.details.size >= 2);
    await page.evaluate(() => showStatus(activePair, activePair.long));
    assert.equal(await page.locator('#arbitrage-network-probe').count(), 0);
    assert.match(await page.locator('#dialog').innerText(), /<b id="arbitrage-network-probe">NET<\/b>/);
    await page.evaluate(() => dialog.close());
    await page.evaluate(() => {location.hash = '/arbitrage?mode=funding';});
    await page.waitForFunction(() => state.mode === 'funding' && !activePair);
    assert.equal(await page.evaluate(() => visiblePairs().length), 200);
    await page.evaluate(now => {window.testNow = now + 30000; expireLiveData();}, NOW);
    assert.equal(await page.evaluate(() => visiblePairs().length), 0);
    await page.setViewportSize({width: 390, height: 844});
    await page.evaluate(now => {window.testNow = now; rebuildPairs(); renderRoute();}, NOW);
    const mobileOverflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    assert.equal(mobileOverflow, false);
    assert.deepEqual(errors, []);
    assert.equal(metadataRequests, 1);
    await page.evaluate(() => {
      localStorage.setItem('arbitrage:favorites', JSON.stringify(['TEST0001', '<b id="arbitrage-escape-probe">BAD</b>']));
    });
    await page.reload();
    await page.waitForFunction(() => live.generatedAt !== null);
    await page.evaluate(() => showFavorites());
    assert.equal(await page.locator('#arbitrage-escape-probe').count(), 0);
    assert.equal(await page.locator('input[name=coin][value=TEST0001]').isChecked(), true);
    const report = {synthetic: true, browser: await browser.version(), initial, statuses,
      preservedUnchangedPair: true, sorting: true, search: true, card: true,
      fundingExpiry: true, mobileOverflow, errors};
    console.log(JSON.stringify(report, null, 2));
  } finally {
    if (browser) await browser.close();
    if (url) await fetch(url + '/__test/stop', {method: 'POST'}).catch(() => {});
    child.kill();
    unlinkSync(fixture);
    rmdirSync(temp);
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
