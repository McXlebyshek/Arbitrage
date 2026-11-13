const {expect, test} = require('@playwright/test');

test('updates through SSE and reuses a compact snapshot until it changes', async ({page, request}) => {
  await page.goto('/');
  const btc = page.getByRole('row').filter({hasText: 'BTC'});
  await expect(btc).toHaveCount(1);
  const entry = btc.locator('td').nth(7);
  await expect(entry).not.toHaveText('—');
  const first = await request.get('/api/markets?compact=1');
  const unchanged = await request.get('/api/markets?compact=1', {headers: {'If-None-Match': first.headers().etag}});
  expect(unchanged.status()).toBe(304);
  const refresh = page.waitForResponse(response => response.url().includes('/api/markets?compact=1') && response.status() === 200);
  await request.post('/fixture/publish?expired=1');
  await refresh;
  await expect(entry).toHaveText('—');
  await request.post('/fixture/publish');
  await expect(entry).not.toHaveText('—');
});

test('sorts, searches and opens a card on a narrow viewport', async ({page, request}) => {
  await request.post('/fixture/publish');
  await page.setViewportSize({width: 390, height: 844});
  await page.goto('/');
  const rows = page.locator('#market-table tbody tr');
  await expect(rows).toHaveCount(2);
  const direction = await rows.first().innerText();
  await page.getByRole('button', {name: 'Спред входа'}).click();
  await expect(rows.first()).not.toHaveText(direction);
  await page.getByRole('button', {name: 'Поиск монеты'}).click();
  await page.locator('#coin-search').fill('BTC');
  await expect(rows).toHaveCount(1);
  const bodyFits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
  expect(bodyFits).toBe(true);
  await rows.first().getByRole('link', {name: 'BTC'}).click();
  await expect(page.getByRole('heading', {name: 'BTC'})).toBeVisible();
  await expect(page.getByRole('tab', {name: 'Открытый интерес'})).toHaveCount(0);
  await expect(page.locator('#chart-frame')).toBeVisible();
  await expect(page.locator('#chart-panel')).toContainText('История накапливается');
});
