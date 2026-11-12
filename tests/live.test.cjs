const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {readFileSync} = require('node:fs');
const {live, makePair, enrichLeg, depthValue, slipValue, setEarlyPair, setFuturesQuotes, updateLive, rebuildPairs} = vm.runInNewContext(
  readFileSync('frontend/live.js', 'utf8') + '\n({live, makePair, enrichLeg, depthValue, slipValue, setEarlyPair, setFuturesQuotes, updateLive, rebuildPairs})',
  {location: {protocol: 'http:', search: ''}, URLSearchParams, demo: {exchanges: {}}},
);
const now = 1800000000000;
const long = {id: 'a', exchange: 'A', type: 'swap', quote: 'USDT', asset: 'btc',
  bid: 99, ask: 100, sourceAt: now, receivedAt: now, rate: 0, hours: 8, totals: {1: 0}};
const short = {...long, id: 'b', exchange: 'B', bid: 101, ask: 102, rate: 0.02, hours: 4, totals: {1: 0.07}};

test('normalizes funding intervals and computes directional spreads from executable sides', () => {
  const pair = makePair('BTC', long, short, now);
  assert.equal(pair.entry, 200 / 201);
  assert.equal(makePair('BTC', short, long, now).entry, -600 / 201);
  assert.equal(pair.exit, -600 / 201);
  assert.equal(pair.exit, makePair('BTC', short, long, now).entry);
  assert.equal(pair.funding, 0.04);
  assert.equal(pair.delta, 0.07);
  assert.equal(pair.verified, true);
});

test('blocks incompatible and stale prices, while preserving unknown funding as null', () => {
  assert.equal(makePair('BTC', long, {...short, quote: 'USDC'}, now), null);
  assert.equal(makePair('BTC', long, {...short, asset: 'other'}, now), null);
  assert.equal(makePair('BTC', long, short, now + 60001).entry, null);
  assert.equal(makePair('BTC', long, {...short, sourceAt: now - 1501}, now).entry, null);
  assert.equal(makePair('BTC', long, {...short, rate: null}, now).funding, null);
});

test('compares scaled prices only in the same verified quote unit', () => {
  const scaledLong = {...long, priceUnits: 1000};
  const scaledShort = {...short, priceUnits: 1000};
  assert.equal(makePair('1000CAT', scaledLong, scaledShort, now).entry, 200 / 201);
  assert.equal(makePair('1000CAT', scaledLong, short, now), null);
  assert.equal(makePair('1000CAT', scaledLong, {...scaledShort, asset: null}, now), null);
});

test('requires both verified legs when a registered alias joins a group', () => {
  const aliased = {...long, nativeBase: '哈基米'};
  assert.equal(makePair('HAJIMI', aliased, short, now).verified, true);
  assert.equal(makePair('HAJIMI', aliased, {...short, asset: null}, now), null);
  assert.equal(makePair('HAJIMI', {...long, asset: null}, {...short, nativeBase: '哈基米'}, now), null);
});

test('early pair expires and cannot reappear after a newer full snapshot', () => {
  const at = Date.now();
  const legs = [long, short].map(leg => ({...leg, networks: [], sourceAt: at, receivedAt: at}));
  const snapshot = {generatedAt: at, markets: {BTC: [legs[0], {...legs[1], sourceAt: at - 2000}]}};
  updateLive(snapshot);
  const event = {baseAt: at, expiresAt: at + 3000, coin: 'BTC', legs};
  assert.equal(setEarlyPair({...event, expiresAt: at}, at), false);
  assert.equal(setEarlyPair(event, at), true);
  rebuildPairs();
  assert.equal(live.fromId('a~b').early, true);
  assert.equal(live.nextDataChange, at + 3000);
  updateLive({...snapshot, generatedAt: at + 1});
  assert.equal(live.fromId('a~b').early, undefined);
  assert.equal(setEarlyPair(event, at), false);
});

test('expires the old funding rate and spread exactly at settlement', () => {
  const expiring = {...short, next: now, networks: []};
  assert.equal(makePair('BTC', long, expiring, now - 1).funding, 0.04);
  assert.equal(makePair('BTC', long, expiring, now).funding, null);
  const enriched = enrichLeg(expiring, now);
  assert.equal(enriched.fundingPending, true);
  assert.equal(enriched.rate, null);
  assert.equal(enriched.totals[1], undefined);
  assert.equal(enrichLeg({...expiring, next: now + 60000}, now, true).rate, null);
});

test('clears expired book prices even when the snapshot and the other leg are fresh', () => {
  const delayed = enrichLeg({...long, sourceAt: now - 60001, networks: []}, now);
  const fresh = enrichLeg({...short, networks: []}, now);
  assert.equal(delayed.bid, null);
  assert.equal(delayed.ask, null);
  assert.equal(fresh.ask, 102);
  assert.equal(makePair('BTC', delayed, fresh, now).entry, null);
});

test('distinguishes depth levels, true zero slippage and insufficient depth', () => {
  const levels = [[100, 1], [101, 1]];
  assert.equal(depthValue(levels, 2), 201);
  assert.equal(depthValue(levels, 3), null);
  assert.equal(slipValue(levels, 100, false), 0);
  assert.equal(slipValue(levels, 300, false), null);
});

test('uses valuations only for a fresh, verified asset and keeps missing fields unknown', () => {
  live.valuations = {btc: {cap: 200, fdv: null, updatedAt: now - 1000, expiresAt: now + 1000}};
  const pair = makePair('BTC', long, short, now);
  assert.equal(pair.cap, 200);
  assert.equal(pair.fdv, null);
  assert.equal(makePair('BTC', long, {...short, asset: null}, now).cap, null);
  assert.equal(makePair('BTC', long, short, now + 1000).cap, null);
});

test('futures stream changes only its two directions and drops them on expiry or clearing', () => {
  const at = Date.now();
  const legs = [long, short].map(leg => ({...leg, networks: [], sourceAt: at, receivedAt: at}));
  const third = {...legs[0], id: 'c', exchange: 'C'};
  const snapshot = {generatedAt: at, markets: {BTC: [legs[0], {...legs[1], sourceAt: at - 2000}, third]}};
  updateLive(snapshot);
  const quotes = legs.map(leg => ({...leg, quoteSource: 'ws'}));
  const pair = {coin: 'BTC', expiresAt: at + 3000, legs: quotes};
  assert.equal(setFuturesQuotes({baseAt: at, pairs: [pair]}), true);
  rebuildPairs();
  assert.equal(live.fromId('a~b').long.quoteSource, 'ws');
  assert.equal(live.fromId('b~a').short.quoteSource, 'ws');
  assert.equal(live.fromId('a~b').baseline.entry, null);
  assert.equal(live.fromId('c~b').entry, null);
  assert.equal(live.nextDataChange, at + 3000);

  setFuturesQuotes({baseAt: at, pairs: [{...pair, expiresAt: at}]});
  rebuildPairs();
  assert.equal(live.fromId('a~b').entry, null);
  setFuturesQuotes({baseAt: at, pairs: []});
  rebuildPairs();
  assert.equal(live.fromId('a~b').entry, null);

  assert.equal(setFuturesQuotes({baseAt: at + 1, pairs: [pair]}), true);
  rebuildPairs();
  assert.equal(live.fromId('a~b').entry, null);
  updateLive({...snapshot, generatedAt: at + 1});
  assert.equal(live.fromId('a~b').long.quoteSource, 'ws');
  assert.equal(setFuturesQuotes({baseAt: at, pairs: []}), false);
  updateLive({...snapshot, generatedAt: at + 2});
  assert.equal(live.fromId('a~b').entry, null);
});
