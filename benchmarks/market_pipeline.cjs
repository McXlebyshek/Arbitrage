const assert = require('node:assert/strict');
const {execFileSync, spawn} = require('node:child_process');
const {request} = require('node:http');
const {cpus, platform, arch} = require('node:os');
const {join, resolve} = require('node:path');
const {performance} = require('node:perf_hooks');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const target = resolve(process.env.ARBITRAGE_TARGET || '.');
const python = process.env.ARBITRAGE_PYTHON || join(target, '.venv', 'Scripts', 'python.exe');
const iterations = 21;
const warmups = 5;
const now = 1800000000000;

function stats(samples) {
  const sorted = [...samples].sort((a, b) => a - b);
  assert.equal(sorted.length, iterations);
  const at = fraction => Number(sorted[Math.ceil(sorted.length * fraction) - 1].toFixed(3));
  return {p50: at(.5), p95: at(.95)};
}

function http(url, path, {method = 'GET', headers} = {}) {
  const start = performance.now();
  return new Promise((resolveResponse, reject) => {
    const req = request(url + path, {method, headers, timeout: 20000}, res => {
      const chunks = [];
      res.on('data', chunk => chunks.push(chunk));
      res.on('error', reject);
      res.on('end', () => resolveResponse({
        status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks),
        ms: performance.now() - start,
      }));
    });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error('Timed out: ' + path)));
    req.end();
  });
}

async function measureHttp(url) {
  const rows = [];
  for (let i = 0; i < iterations + warmups; i++) {
    const published = await http(url, '/__bench/publish', {method: 'POST'});
    assert.equal(published.status, 200);
    const {generatedAt, publicationMs} = JSON.parse(published.body);
    const first = await http(url, '/api/markets');
    assert.equal(first.status, 200);
    assert.equal(JSON.parse(first.body).generatedAt, generatedAt);
    const warm = await http(url, '/api/markets');
    assert.equal(warm.status, 200);
    assert.ok(warm.body.equals(first.body));
    const conditional = await http(url, '/api/markets', {headers: {'If-None-Match': first.headers.etag}});
    assert.equal(conditional.status, 304);
    assert.equal(conditional.body.length, 0);
    const gzip = await http(url, '/api/markets', {headers: {'Accept-Encoding': 'gzip'}});
    assert.equal(gzip.headers['content-encoding'], 'gzip');
    rows.push({publicationMs, first: first.ms, warm: warm.ms, conditional: conditional.ms, gzip: gzip.ms,
      identityBytes: first.body.length, gzipBytes: gzip.body.length});
  }
  return rows.slice(warmups);
}

async function measureBrowser(url) {
  const browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 960}});
    await page.addInitScript(clock => {Date.now = () => clock;}, now);
    await page.goto(url + '/#/arbitrage?mode=spread&sort=entry');
    await page.waitForFunction(() => live.pairs.length === 66000 &&
      document.querySelectorAll('#market-table tbody tr').length === 80);
    const render = await page.evaluate(({iterations, warmups}) => {
      const samples = [];
      for (let i = 0; i < iterations + warmups; i++) {
        const started = performance.now();
        renderTable();
        samples.push(performance.now() - started);
      }
      return samples.slice(warmups);
    }, {iterations, warmups});
    const e2e = await page.evaluate(async ({iterations, warmups}) => {
      const samples = [];
      for (let i = 0; i < iterations + warmups; i++) {
        const started = performance.now();
        const response = await fetch('/__bench/publish', {method: 'POST'});
        const {generatedAt} = await response.json();
        while (live.generatedAt !== generatedAt) {
          if (performance.now() - started > 5000) throw new Error('Snapshot missed in browser');
          await new Promise(requestAnimationFrame);
        }
        samples.push(performance.now() - started);
      }
      return samples.slice(warmups);
    }, {iterations, warmups});
    return {version: await browser.version(), tableRenderMs: stats(render), publicationToTableMs: stats(e2e)};
  } finally {
    await browser.close();
  }
}

async function main() {
  const child = spawn(python, ['-B', join(__dirname, 'market_pipeline.py')], {
    env: {...process.env, ARBITRAGE_TARGET: target}, windowsHide: true,
  });
  child.stderr.pipe(process.stderr);
  let url;
  try {
    const port = await new Promise((resolvePort, reject) => {
      child.on('error', reject);
      child.on('exit', code => reject(new Error('Fixture server exited: ' + code)));
      child.stdout.on('data', output => {
        const match = /PORT=(\d+)/.exec(output.toString());
        if (match) resolvePort(match[1]);
      });
    });
    url = 'http://127.0.0.1:' + port;
    const buildResponse = await http(url, '/__bench/build');
    assert.equal(buildResponse.status, 200);
    const {cold, warm, ...versions} = JSON.parse(buildResponse.body);
    const rows = await measureHttp(url);
    const browser = await measureBrowser(url);
    console.log(JSON.stringify({
      revision: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: target, encoding: 'utf8'}).trim(),
      environment: {os: platform(), arch: arch(), cpu: cpus()[0]?.model, node: process.version, ...versions},
      fixture: {coins: 1000, legs: 12000, directions: 66000, renderedRows: 80, iterations, warmups},
      buildMs: {cold: stats(cold), warm: stats(warm)},
      httpMs: Object.fromEntries(['publicationMs', 'first', 'warm', 'conditional', 'gzip']
        .map(field => [field, stats(rows.map(row => row[field]))])),
      bytes: {identity: rows[0].identityBytes, gzip: rows[0].gzipBytes},
      browser,
    }, null, 2));
  } finally {
    if (url) await http(url, '/__bench/stop', {method: 'POST'}).catch(() => {});
    if (child.exitCode === null) child.kill();
  }
}

main().catch(error => {console.error(error); process.exitCode = 1;});
