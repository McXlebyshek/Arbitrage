// Deterministic artificial markets, never used as live data.
const NOW = 1800000000000;
function snapshot(count = 100, at = NOW) {
  const markets = {}, valuations = {};
  for (let i = 0; i < count; i++) {
    const coin = 'TEST' + String(i).padStart(4, '0');
    markets[coin] = Array.from({length: 12}, (_, j) => ({
      id: `${coin}.${j}`, exchange: 'Venue' + j % 6, type: j < 6 ? 'swap' : 'spot',
      quote: i % 5 ? 'USDT' : 'USDC', asset: coin, bid: 100 + (j + i) % 7 / 10,
      ask: 101 + (j + i) % 7 / 10, last: 101, sourceAt: at - (j === 1 ? 2000 : 0),
      receivedAt: at, rate: j < 6 ? j * 0.001 : 0, hours: j < 6 ? 8 : null,
      next: j < 6 ? at + 30000 : null, totals: {'1': j * .01, '3': j * .03, '7': j * .07},
      networks: [], volume: i % 11 ? 5 : null,
    }));
    valuations[coin] = {cap: i % 13 ? i + 1 : null, fdv: i + 2, updatedAt: at, expiresAt: at + 45000};
  }
  return {generatedAt: at, state: 'ready', venues: {}, markets, valuations};
}
function quotes(snapshot, count = 30, at = NOW) {
  return {baseAt: snapshot.generatedAt, pairs: Object.entries(snapshot.markets).slice(0, count)
    .map(([coin, legs]) => ({coin, expiresAt: at + 3000,
      legs: legs.slice(0, 2).map((leg, i) => ({...leg, bid: 101 + i, ask: 102 + i,
        sourceAt: at, changedAt: at, receivedAt: at, confirmedAt: at, validUntil: at + 3000,
        quoteSource: 'ws'}))}))};
}
module.exports = {NOW, snapshot, quotes};
