const {expect, test} = require('@playwright/test');

function largeSnapshot() {
  const now = Date.now();
  const markets = {};
  for (let index = 0; index < 160; index++) {
    const coin = `TEST${String(index).padStart(3, '0')}`;
    markets[coin] = ['Binance', 'Bybit'].map((exchange, leg) => ({
      id: `${exchange}.swap.${coin}.USDT`, exchange, type: 'swap', quote: 'USDT',
      symbol: `${coin}/USDT:USDT`, bid: 100 + leg, ask: 101 + leg, last: 100 + leg,
      asset: coin, size: 1, sourceAt: now, receivedAt: now, oi: null, index: 100,
      volume: 10, totals: {}, networks: [], rate: leg * 0.01, hours: 8, next: now + 60000,
    }));
  }
  return {state: 'ready', markets, venues: {}, valuations: {}, generatedAt: now};
}

test('migrates legacy settings and keeps unrelated values when one key is invalid', async ({page}) => {
  await page.addInitScript(() => {
    localStorage.setItem('arbitrage:favorites', '{}');
    localStorage.setItem('arbitrage:theme', JSON.stringify('light'));
    localStorage.setItem('arbitrage:layout', JSON.stringify('compact'));
  });
  await page.goto('/?demo=1');
  await expect(page.getByRole('heading', {name: 'Арбитраж'})).toBeVisible();
  const state = await page.evaluate(() => ({
    theme: JSON.parse(localStorage.getItem('arbitrage:theme')),
    layout: JSON.parse(localStorage.getItem('arbitrage:layout')),
    favorites: localStorage.getItem('arbitrage:favorites'),
    applied: document.documentElement.dataset.theme,
  }));
  expect(state.theme).toEqual({schemaVersion: 1, value: 'light'});
  expect(state.layout).toEqual({schemaVersion: 1, value: 'compact'});
  expect(state.favorites).toBe('{}');
  expect(state.applied).toBe('light');
});

test('reuses unchanged rows and cells during a snapshot update', async ({page, request}) => {
  await page.goto('/');
  const btc = page.getByRole('row').filter({hasText: 'BTC'});
  await expect(btc).toHaveCount(1);
  const link = btc.getByRole('link', {name: 'BTC'});
  await link.focus();
  await page.evaluate(() => {
    const row = [...document.querySelectorAll('#market-table tbody tr')].find(item => item.textContent.includes('BTC'));
    window.originalRow = row;
    window.originalCoinCell = row.cells[0];
    window.originalSpreadCell = row.cells[7];
  });
  const refresh = page.waitForResponse(response => response.url().includes('/api/markets?compact=1') && response.status() === 200);
  await request.post('/fixture/publish?expired=1');
  await refresh;
  await expect(btc.locator('td').nth(7)).toHaveText('—');
  const identity = await page.evaluate(() => {
    const row = [...document.querySelectorAll('#market-table tbody tr')].find(item => item.textContent.includes('BTC'));
    return {row: row === window.originalRow, coin: row.cells[0] === window.originalCoinCell,
      spread: row.cells[7] === window.originalSpreadCell};
  });
  expect(identity).toEqual({row: true, coin: true, spread: false});
  await expect(link).toBeFocused();
});

test('virtualizes rows and retains a focused row when the page scrolls', async ({page}) => {
  await page.setViewportSize({width: 390, height: 844});
  await page.route('**/api/markets?compact=1', route => route.fulfill({json: largeSnapshot()}));
  await page.goto('/');
  const rows = page.locator('#market-table tbody tr[data-index]');
  await expect(rows.first()).toBeVisible();
  await expect(page.locator('#result-count')).toContainText('160');
  expect(await rows.count()).toBeLessThan(40);
  const first = rows.first().getByRole('link');
  await first.focus();
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await expect.poll(async () => Number(await rows.last().getAttribute('data-index'))).toBeGreaterThan(60);
  await expect(first).toBeFocused();
  expect(await rows.count()).toBeLessThan(40);
  await page.getByRole('button', {name: 'Показать ещё'}).click();
  await expect(page.locator('#market-table table')).toHaveAttribute('aria-rowcount', '161');
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await expect.poll(async () => Number(await rows.last().getAttribute('data-index'))).toBeGreaterThan(140);
  expect(await rows.count()).toBeLessThan(40);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('opens a concrete demo fallback from file protocol', async ({page}) => {
  const url = new URL('../../frontend/index.html', `file:///${__filename.replaceAll('\\', '/')}`);
  await page.goto(url.href);
  await expect(page.locator('#app')).toContainText('Start-UI.cmd');
  await expect(page.locator('#app')).toContainText('/?demo=1');
});
