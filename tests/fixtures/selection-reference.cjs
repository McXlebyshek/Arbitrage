// Baseline selection is a regression oracle, never loaded by the app.
module.exports = String.raw`
function matches(pair) {
  const {coin, long, short, entry, apr, cap, fdv, delta, category} = pair;
  const spot = long.type === 'spot';
  if (spot !== ['spot', 'spotSpread', 'negative'].includes(state.mode)) return false;
  if (state.mode === 'negative' && !(entry < 0)) return false;
  if (state.mode === 'spread' && !(isDemo ? entry > 0 : Number.isFinite(entry))) return false;
  if (state.mode === 'spotSpread' && !(entry > 0)) return false;
  if (!isDemo && ['funding', 'spot'].includes(state.mode) && !Number.isFinite(pair.funding)) return false;
  const query = state.query.trim().toUpperCase();
  const matchesCoin = coin.includes(query) || (!isDemo &&
    live.markets[coin]?.legs.some(leg => leg.nativeBase?.toUpperCase().includes(query)));
  if (!matchesCoin || (state.expand && state.expand !== coin)) return false;
  if (!state.exchanges.includes(long.exchange) || !state.exchanges.includes(short.exchange)) return false;
  if (state.favorites && !favorites.has(coin)) return false;
  if (state.categories.length && !state.categories.includes(category)) return false;
  if (state.quotes.length && (!state.quotes.includes(long.quote) || !state.quotes.includes(short.quote))) return false;
  if (state.spread && (!Number.isFinite(entry) ||
      (state.operator === 'lte' ? entry > Number(state.spread) : entry < Number(state.spread)))) return false;
  const volume = Number.isFinite(long.volume) && Number.isFinite(short.volume) ? Math.min(long.volume, short.volume) : null;
  const minima = {volume, apr, funding: pair.funding, capMin: cap, fdvMin: fdv, delta, delta3: pair.delta3, delta7: pair.delta7};
  return Object.entries(minima).every(([key, value]) => !state[key] || (Number.isFinite(value) && value >= Number(state[key]))) &&
    (!state.capMax || (Number.isFinite(cap) && cap <= Number(state.capMax))) &&
    (!state.fdvMax || (Number.isFinite(fdv) && fdv <= Number(state.fdvMax)));
}
function visiblePairs() {
  const rows = dataSource.pairs.filter(matches);
  rows.sort((a, b) => {
    const key = state.sort;
    if (key !== 'coin' && Number.isFinite(a[key]) !== Number.isFinite(b[key])) return Number.isFinite(a[key]) ? -1 : 1;
    const order = key === 'coin' ? a.coin.localeCompare(b.coin) : a[key] - b[key];
    return order * (state.direction === 'asc' ? 1 : -1);
  });
  if (!state.group || state.expand) return rows.map(pair => [pair, 0]);
  const groups = new Map();
  for (const pair of rows) {
    if (!groups.has(pair.coin)) groups.set(pair.coin, [pair, 0]);
    else groups.get(pair.coin)[1]++;
  }
  return Array.from(groups.values());
}
`;
