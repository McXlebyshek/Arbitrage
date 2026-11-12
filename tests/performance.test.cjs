const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {readFileSync} = require('node:fs');
const {NOW, snapshot, quotes} = require('./fixtures/performance.cjs');
const liveCode = readFileSync('frontend/live.js', 'utf8');

function model(demoMode = false) {
  let now = NOW;
  const context = vm.createContext({location: {protocol: 'http:', search: demoMode ? '?demo=1' : ''}, URLSearchParams,
    demo: {exchanges: {}}, Date: class extends Date {static now() {return now;}}});
  const api = vm.runInContext(liveCode + '\n({live,updateLive,setEarlyPair,setFuturesQuotes,rebuildPairs,refreshQuotePairs,selectPairs})', context);
  return {...api, context, time: at => {now = at;}};
}
function comparable(api) {
  return JSON.stringify({pairs: api.live.pairs, deadline: api.live.nextDataChange,
    ids: [...api.live.pairIndex.keys()], markets: api.live.markets});
}

test('incremental quotes match a full rebuild through replacement, clearing and all deadlines', () => {
  const fast = model(), full = model(), data = snapshot(20);
  const check = () => {
    fast.refreshQuotePairs(); full.rebuildPairs();
    assert.equal(comparable(fast), comparable(full));
  };
  for (const api of [fast, full]) api.updateLive(data);
  const unchanged = fast.live.fromId('TEST0019.2~TEST0019.3');
  for (let i = 0; i < 5; i++) {
    const event = quotes(data, 10, NOW + i * 100);
    for (const api of [fast, full]) {api.time(NOW + i * 100); api.setFuturesQuotes(event);}
    check();
    assert.equal(fast.live.fromId(unchanged.id), unchanged);
  }
  for (const api of [fast, full]) api.setFuturesQuotes({baseAt: NOW, pairs: []});
  check();
  for (const api of [fast, full]) {
    api.setFuturesQuotes(quotes(data));
    api.setEarlyPair({...quotes(data).pairs[0], baseAt: NOW}, NOW);
  }
  check();
  for (const at of [NOW + 3001, NOW + 30000, NOW + 45000, NOW + 60001]) {
    for (const api of [fast, full]) api.time(at);
    check();
  }
  for (const api of [fast, full]) {api.time(NOW + 61000); api.updateLive(snapshot(5, NOW + 61000));}
  check();
});

test('stream-only pairs are removed without holes or duplicate entries', () => {
  const fast = model(), full = model(), data = snapshot(1);
  // This pair fails the baseline price-scale guard, while other pairs still exist.
  data.markets.TEST0000[1] = {...data.markets.TEST0000[1], bid: 500, ask: 501, asset: null};
  for (const api of [fast, full]) {api.updateLive(data); api.setFuturesQuotes(quotes(data));}
  fast.refreshQuotePairs(); full.rebuildPairs();
  assert.equal(comparable(fast), comparable(full));
  assert.equal(fast.live.fromId('TEST0000.0~TEST0000.1').baseline, undefined);
  for (const api of [fast, full]) api.setFuturesQuotes({baseAt: NOW, pairs: []});
  fast.refreshQuotePairs(); full.rebuildPairs();
  assert.equal(comparable(fast), comparable(full));
  assert.equal(fast.live.fromId('TEST0000.0~TEST0000.1'), undefined);
  assert.equal(fast.live.pairs.length, fast.live.pairIndex.size);
});

test('detail expiry and later snapshots invalidate the incremental baseline', () => {
  const fast = model(), full = model(), data = snapshot(2);
  for (const api of [fast, full]) {
    api.live.details.set('TEST0000.0', {refreshAfter: NOW + 1000, totals: {'1': 8}});
    api.updateLive(data); api.setFuturesQuotes(quotes(data));
  }
  fast.refreshQuotePairs(); full.rebuildPairs();
  assert.equal(comparable(fast), comparable(full));
  for (const api of [fast, full]) api.time(NOW + 1000);
  fast.refreshQuotePairs(); full.rebuildPairs();
  assert.equal(comparable(fast), comparable(full));
  for (const api of [fast, full]) api.updateLive({...data, generatedAt: NOW + 1000, markets: {}});
  assert.equal(fast.live.pairs.length, 0);
  assert.equal(comparable(fast), comparable(full));
});

test('clock rollback and initially future quotes cannot preserve an invalid cached spread', () => {
  const fast = model(), full = model(), data = snapshot(1);
  for (const api of [fast, full]) api.updateLive(data);
  for (const at of [NOW - 2000, NOW - 1000, NOW, NOW + 100]) {
    for (const api of [fast, full]) api.time(at);
    fast.refreshQuotePairs(); full.rebuildPairs();
    assert.equal(comparable(fast), comparable(full));
  }
});

// Straightforward full sort is an independent oracle for the grouped selection.
function oldSelection(pairs, match, key, direction, group) {
  const rows = pairs.filter(match).sort((a, b) => {
    if (key !== 'coin' && Number.isFinite(a[key]) !== Number.isFinite(b[key])) return Number.isFinite(a[key]) ? -1 : 1;
    return (key === 'coin' ? a.coin.localeCompare(b.coin) : a[key] - b[key]) * (direction === 'asc' ? 1 : -1);
  });
  if (!group) return rows.map(pair => [pair, 0]);
  const groups = new Map();
  for (const pair of rows) {
    if (!groups.has(pair.coin)) groups.set(pair.coin, [pair, 0]);
    else groups.get(pair.coin)[1]++;
  }
  return [...groups.values()];
}

test('group-before-sort preserves winners, counts, ties, nulls and both directions', () => {
  const api = model();
  const pairs = Array.from({length: 2500}, (_, i) => ({id: i, coin: 'C' + (i * 17 % 137),
    entry: i % 7 ? (i * 13 % 21 - 10) / 10 : null,
    funding: i % 3 ? i % 9 / 10 : undefined, apr: i % 5 ? 0 : NaN, delta: i % 11}));
  for (const key of ['coin', 'entry', 'funding', 'apr', 'delta']) for (const direction of ['asc', 'desc']) {
    for (const group of [true, false]) for (const match of [() => true, pair => pair.id % 3 === 0, () => false]) {
      assert.equal(JSON.stringify(api.selectPairs(pairs, match, key, direction, group)),
        JSON.stringify(oldSelection(pairs, match, key, direction, group)));
    }
  }
});

test('incremental updates create only affected directions', () => {
  const api = model(), data = snapshot(100);
  api.updateLive(data);
  vm.runInContext('let calls=0; const makeOriginal=makePair; makePair=(...args)=>{calls++; return makeOriginal(...args);}', api.context);
  api.setFuturesQuotes(quotes(data, 30)); api.refreshQuotePairs();
  assert.equal(vm.runInContext('calls', api.context), 60);
  assert.equal(api.live.pairs.length, 6600);
});

test('prepared filters preserve all modes, aliases, thresholds, favorites and sorting', () => {
  const appCode = readFileSync('frontend/app.js', 'utf8');
  const optimized = appCode.slice(appCode.indexOf('function createMatcher()'), appCode.indexOf('function status('));
  for (const demoMode of [false, true]) {
    const fast = model(demoMode), old = model(demoMode), data = snapshot(12);
    data.markets.TEST0001[0].nativeBase = '哈基米';
    for (const api of [fast, old]) {
      api.updateLive(data);
      vm.runInContext('const dataSource=live; const favorites=new Set(["TEST0001","TEST0004"]);', api.context);
    }
    const selectFast = vm.runInContext(optimized + '\nvisiblePairs', fast.context);
    const selectOld = vm.runInContext(require('./fixtures/selection-reference.cjs') + '\nvisiblePairs', old.context);
    const overrides = [{}, {query: ' 哈基米 '}, {query: 'test0001'}, {favorites: true},
      {exchanges: ['Venue0', 'Venue1']}, {categories: ['other']}, {quotes: ['USDC']},
      {expand: 'TEST0001'}, {spread: '0', operator: 'lte'}, {spread: '-2'},
      {volume: '1'}, {capMin: '1', fdvMin: '2', capMax: '8', fdvMax: '10'},
      {funding: '0', apr: '0', delta: '-0.01', delta3: '0', delta7: '0'}];
    for (const mode of ['funding', 'spread', 'spot', 'spotSpread', 'negative']) {
      for (const group of [false, true]) for (const override of overrides) {
        for (const sort of ['coin', 'entry', 'funding', 'apr', 'delta']) {
          const state = {mode, sort, direction: group ? 'desc' : 'asc', group, categories: [], quotes: [],
            exchanges: Array.from({length: 6}, (_, i) => 'Venue' + i), query: '', ...override};
          fast.context.state = old.context.state = state;
          assert.equal(JSON.stringify(selectFast().map(([pair, more]) => [pair.id, more])),
            JSON.stringify(selectOld().map(([pair, more]) => [pair.id, more])), JSON.stringify(state));
        }
      }
    }
  }
});
