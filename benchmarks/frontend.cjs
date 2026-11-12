// Run from project root. Optional first argument: directory with the baseline frontend/.
const {readFileSync} = require('node:fs');
const {resolve, join} = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {performance} = require('node:perf_hooks');
const {NOW, snapshot, quotes} = require('../tests/fixtures/performance.cjs');
const baseline = process.argv[2];
function load(root, legacy) {
  const model = readFileSync(join(root, 'frontend/live.js'), 'utf8');
  const app = readFileSync(join(root, 'frontend/app.js'), 'utf8');
  const selection = legacy && !baseline ? require('../tests/fixtures/selection-reference.cjs') :
    app.slice(app.indexOf(legacy ? 'function matches(' : 'function createMatcher('), app.indexOf('function status('));
  const context = vm.createContext({location: {protocol: 'http:', search: ''}, URLSearchParams,
    demo: {exchanges: {}}, Date: class extends Date {static now() {return NOW;}}});
  const setup = `const dataSource=live, favorites=new Set(); const state={mode:'spread',sort:'entry',
    direction:'desc',group:true,categories:['crypto'],quotes:[],query:'',
    exchanges:Array.from({length:6},(_,i)=>'Venue'+i)};`;
  return vm.runInContext(model + '\n' + setup + selection + '\n({live,updateLive,setFuturesQuotes,' +
    'rebuildPairs,visiblePairs,refresh:' + (legacy ? 'rebuildPairs' : 'refreshQuotePairs') + '})', context);
}
function median(fn, iterations = 15) {
  for (let i = 0; i < 3; i++) fn();
  const timings = [];
  for (let i = 0; i < iterations; i++) {
    const start = performance.now(); fn(); timings.push(performance.now() - start);
  }
  timings.sort((a, b) => a - b);
  return Number(timings[Math.floor(timings.length / 2)].toFixed(3));
}
const data = snapshot(1000), event = quotes(data, 30);
const old = load(baseline ? resolve(baseline) : '.', true), fast = load('.', false);
for (const api of [old, fast]) {api.updateLive(data); api.setFuturesQuotes(event); api.refresh();}
assert.equal(JSON.stringify(fast.live.pairs), JSON.stringify(old.live.pairs));
assert.equal(JSON.stringify(fast.visiblePairs()), JSON.stringify(old.visiblePairs()));
const result = {synthetic: true, coins: 1000, legs: 12000, pairs: fast.live.pairs.length,
  wsCoins: 30, iterations: 15, baseline: baseline ?? 'full rebuild and legacy selection oracle',
  quoteUpdateMs: {before: median(() => old.refresh()), after: median(() => fast.refresh())},
  groupedSelectionMs: {before: median(() => old.visiblePairs()), after: median(() => fast.visiblePairs())},
  equivalent: true, includesDOM: false};
console.log(JSON.stringify(result, null, 2));
