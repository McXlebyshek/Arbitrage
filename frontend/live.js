const isDemo = location.protocol === 'file:' || new URLSearchParams(location.search).get('demo') === '1';
const live = {
  exchanges: {
    ...demo.exchanges, Aster: ['A', '#7b621d'], Extended: ['E', '#584786'],
  },
  markets: {}, valuations: {}, pairs: [], pairIndex: new Map(), details: new Map(), books: new Map(),
  pairSlots: new Map(), quoteOverrides: new Map(), baselineLength: 0, baselineDeadline: 0, baselineAt: 0,
  generatedAt: null, config: null, futuresQuotes: null, earlyPair: null, earlyBaseAt: 0, nextDataChange: Infinity,
  state: 'starting', venues: {}, error: false,
  priceAnomalies: 0,
};
for (const venue of ['Arcus', 'Robinlighter']) delete live.exchanges[venue];

function threshold(name) {
  const value = live.config?.[name];
  return Number.isFinite(value) ? value : Infinity;
}

function currentQuote(leg, now) {
  const fresh = stamp => Number.isFinite(stamp) && now - stamp >= -threshold('futureClockToleranceMs') &&
    now - stamp <= threshold('quoteMaxAgeMs');
  if (leg.quoteSource === 'ws' || leg.quoteSource === 'rest') {
    return Number.isFinite(leg.confirmedAt) && Number.isFinite(leg.validUntil) &&
      now >= leg.confirmedAt - threshold('futureClockToleranceMs') && now <= leg.validUntil;
  }
  return fresh(leg.receivedAt) && (leg.sourceAt === null || fresh(leg.sourceAt));
}

function spreadQuality(long, short, now) {
  if (!currentQuote(long, now) || !currentQuote(short, now)) return 'stale';
  if (!(long.ask > 0 && short.bid > 0 && long.bid > 0 && short.ask > 0)) return 'missing';
  if (Number.isFinite(long.confirmedAt) && Number.isFinite(short.confirmedAt)) {
    return 'receipt';
  }
  if (long.sourceAt !== null && short.sourceAt !== null) {
    return Math.abs(long.sourceAt - short.sourceAt) <= threshold('sourceTimeSkewMs') ? 'timed' : 'skew';
  }
  return Math.abs(long.receivedAt - short.receivedAt) <= threshold('receiptTimeSkewMs') ? 'receipt' : 'skew';
}

function openSpread(longAsk, shortBid) {
  if (!(Number.isFinite(longAsk) && longAsk > 0 && Number.isFinite(shortBid) && shortBid > 0)) return null;
  return 200 * (shortBid - longAsk) / (longAsk + shortBid);
}

function closeSpread(longBid, shortAsk) {
  return openSpread(shortAsk, longBid);
}

function aprAfterFees(pair, settings) {
  const inputs = [settings.longFee, settings.shortFee, settings.holdDays];
  if (!Number.isFinite(pair.apr) || inputs.some(value => value === '' || value == null)) return null;
  const [longFee, shortFee, holdDays] = inputs.map(Number);
  if (![longFee, shortFee, holdDays].every(Number.isFinite) || longFee < 0 || shortFee < 0 ||
      longFee > 1 || shortFee > 1 || holdDays <= 0) return null;
  // APR assumes equal opening notionals; each taker fee is paid on entry and exit.
  return pair.apr / 2 - (longFee + shortFee) * 365 / holdDays;
}

function makePair(coin, long, short, now, onPriceAnomaly) {
  if (!long || !short || long === short || short.type !== 'swap' || long.quote !== short.quote ||
      (long.exchange === short.exchange && long.type === short.type) ||
      (long.asset && short.asset && long.asset !== short.asset)) return null;
  const verified = Boolean(long.asset && long.asset === short.asset);
  const priceUnits = long.priceUnits ?? 1;
  if (priceUnits !== (short.priceUnits ?? 1) ||
      ((priceUnits !== 1 || long.nativeBase || short.nativeBase) && !verified)) return null;
  const knownPrices = [long.bid ?? long.last, short.bid ?? short.last];
  if (knownPrices.every(price => price > 0) && Math.max(...knownPrices) / Math.min(...knownPrices) > threshold('maxPriceRatio')) {
    onPriceAnomaly?.();
    return null;
  }
  const quality = spreadQuality(long, short, now);
  const comparable = quality === 'timed' || quality === 'receipt';
  const normalizedRate = leg => leg.type === 'spot' ? 0 :
    !fundingExpired(leg, now) && Number.isFinite(leg.rate) && leg.hours > 0 ? leg.rate * 8 / leg.hours : null;
  const left = normalizedRate(long), right = normalizedRate(short);
  const funding = left === null || right === null ? null : right - left;
  const delta = days => Number.isFinite(long.totals[days]) && Number.isFinite(short.totals[days])
    ? short.totals[days] - long.totals[days] : null;
  const cached = verified ? live.valuations[long.asset] : null;
  const valuation = cached?.expiresAt > now ? cached : null;
  return {
    id: [long.id, short.id].join('~'), coin, long, short, category: 'crypto',
    entry: comparable ? openSpread(long.ask, short.bid) : null,
    exit: comparable ? closeSpread(long.bid, short.ask) : null,
    funding, apr: funding === null ? null : funding * 1095, delta: delta(1), delta3: delta(3), delta7: delta(7),
    quality, timestampSuspect: Boolean(long.suspect || short.suspect ||
      Number.isFinite(long.changedAt) && Number.isFinite(short.changedAt) &&
      Math.abs(long.changedAt - short.changedAt) > threshold('sourceTimeSkewMs')),
    verified, cap: valuation?.cap ?? null, fdv: valuation?.fdv ?? null,
    valuationAt: valuation?.updatedAt ?? null,
  };
}

function fundingExpired(leg, now) {
  return Number.isFinite(leg.next) && leg.next <= now;
}

function enrichLeg(leg, now, snapshotStale = false) {
  const cached = live.details.get(leg.id);
  const detail = cached?.refreshAfter > now ? cached : null;
  const fundingPending = fundingExpired(leg, now);
  const totals = {...leg.totals, ...detail?.totals};
  const funding = Object.fromEntries(Object.entries(detail?.funding ?? {}).filter(([field, value]) =>
    leg[field] == null && value !== null && (field !== 'next' || value > now)));
  if (fundingPending) Object.assign(funding, {rate: null, hours: null, next: null});
  else if (snapshotStale && leg.type === 'swap') funding.rate = null;
  const status = ['deposit', 'withdraw'].map(field => leg.networks.some(network => network[field] === true)
    ? true : leg.networks.length && leg.networks.every(network => network[field] === false) ? false : undefined);
  const quote = currentQuote(leg, now) ? {} : {bid: null, ask: null};
  return {...leg, ...quote, ...funding, status, totals: fundingPending ? {} : totals, fundingPending};
}

function updateLive(snapshot) {
  const {markets, valuations = {}, generatedAt, state, venues} = snapshot;
  Object.assign(live, {generatedAt, state, venues, valuations, config: snapshot.config ?? live.config,
    error: false, markets: {}});
  live.rawMarkets = markets;
  if (live.earlyPair && generatedAt > live.earlyPair.baseAt) live.earlyPair = null;
  rebuildPairs();
}

function setEarlyPair(event, now = Date.now()) {
  if (!Number.isFinite(event.baseAt) || event.baseAt < live.earlyBaseAt) return false;
  live.earlyBaseAt = event.baseAt;
  const previous = live.earlyPair;
  if (event.legs === null) {
    if (!previous || previous.baseAt > event.baseAt) return false;
    live.earlyPair = null;
    return true;
  }
  if (event.baseAt < live.generatedAt || previous?.baseAt > event.baseAt ||
      !Number.isFinite(event.expiresAt) || event.expiresAt <= now || !Array.isArray(event.legs) ||
      event.legs.length !== 2 || event.legs[0].id === event.legs[1].id) return false;
  live.earlyPair = event;
  return true;
}

function setFuturesQuotes(event) {
  if (!Number.isFinite(event.baseAt) || event.baseAt < live.generatedAt ||
      event.baseAt < live.futuresQuotes?.baseAt || !Array.isArray(event.pairs)) return false;
  live.futuresQuotes = event;
  return true;
}

function rebuildPairs() {
  const now = Date.now();
  const snapshotExpires = live.generatedAt ? live.generatedAt + threshold('quoteMaxAgeMs') : Infinity;
  const snapshotStale = now >= snapshotExpires;
  live.nextDataChange = snapshotStale ? Infinity : snapshotExpires;
  for (const {expiresAt} of Object.values(live.valuations)) {
    if (expiresAt > now) live.nextDataChange = Math.min(live.nextDataChange, expiresAt);
  }
  live.pairs = [];
  live.pairIndex = new Map();
  live.pairSlots = new Map();
  live.quoteOverrides = new Map();
  live.markets = {};
  let priceAnomalies = 0;
  const countPriceAnomaly = () => { priceAnomalies++; };
  for (const [coin, values] of Object.entries(live.rawMarkets ?? {})) {
    const legs = values.map(leg => {
      const enriched = enrichLeg(leg, now, snapshotStale);
      const quoteExpires = enriched.bid > 0 && enriched.ask > 0
        ? Math.min(enriched.sourceAt ?? Infinity, enriched.receivedAt) + threshold('quoteMaxAgeMs') + 1 : null;
      for (const deadline of [quoteExpires, enriched.next, live.details.get(leg.id)?.refreshAfter]) {
        if (deadline > now) live.nextDataChange = Math.min(live.nextDataChange, deadline);
      }
      // A browser clock behind the source can make a future quote usable later.
      const validAfter = Math.max(leg.sourceAt ?? -Infinity, leg.receivedAt ?? -Infinity) -
        threshold('futureClockToleranceMs');
      if (validAfter > now) live.nextDataChange = Math.min(live.nextDataChange, validAfter);
      return enriched;
    });
    let available = false;
    for (const long of legs) for (const short of legs) {
      const pair = makePair(coin, long, short, now, countPriceAnomaly);
      if (!pair) continue;
      live.pairSlots.set(pair.id, live.pairs.length);
      live.pairs.push(pair);
      live.pairIndex.set(pair.id, pair);
      available = true;
    }
    if (available) live.markets[coin] = {legs};
  }
  live.baselineLength = live.pairs.length;
  live.priceAnomalies = priceAnomalies;
  live.baselineDeadline = live.nextDataChange;
  live.baselineAt = now;
  refreshQuotePairs(now);
}

function refreshQuotePairs(now = Date.now()) {
  // Rebuild the full model only when base quotes, funding or details expire.
  if (now >= live.baselineDeadline || now < live.baselineAt) {
    rebuildPairs();
    return;
  }
  for (const [id, baseline] of live.quoteOverrides) {
    if (baseline) {
      live.pairIndex.set(id, baseline);
      live.pairs[live.pairSlots.get(id)] = baseline;
    } else {
      live.pairIndex.delete(id);
      live.pairSlots.delete(id);
    }
  }
  live.pairs.length = live.baselineLength;
  live.quoteOverrides.clear();
  live.nextDataChange = live.baselineDeadline;
  function replace(pair) {
    if (!live.quoteOverrides.has(pair.id)) {
      live.quoteOverrides.set(pair.id, live.pairIndex.get(pair.id));
    }
    if (!live.pairSlots.has(pair.id)) live.pairSlots.set(pair.id, live.pairs.length);
    live.pairs[live.pairSlots.get(pair.id)] = pair;
    live.pairIndex.set(pair.id, pair);
  }
  const updates = [];
  if (live.futuresQuotes?.baseAt === live.generatedAt) {
    for (const event of live.futuresQuotes.pairs) {
      if (!Number.isFinite(event.expiresAt) || event.expiresAt <= now || event.legs?.length !== 2) continue;
      const ids = new Set(live.markets[event.coin]?.legs.map(leg => leg.id));
      if (!event.legs.every(leg => ids.has(leg.id))) continue;
      const [a, b] = event.legs.map(leg => enrichLeg(leg, now));
      updates.push(...[makePair(event.coin, a, b, now), makePair(event.coin, b, a, now)]
        .filter(pair => pair?.quality === 'timed' || pair?.quality === 'receipt'));
      live.nextDataChange = Math.min(live.nextDataChange, event.expiresAt);
    }
  }
  for (const pair of updates) replace({...pair, baseline: live.pairIndex.get(pair.id)});
  const early = live.earlyPair;
  if (!early) return;
  if (now >= early.expiresAt || live.generatedAt > early.baseAt) {
    live.earlyPair = null;
    return;
  }
  live.nextDataChange = Math.min(live.nextDataChange, early.expiresAt);
  const ids = new Set(live.markets[early.coin]?.legs.map(leg => leg.id));
  if (!early.legs.every(leg => ids.has(leg.id))) return;
  const [left, right] = early.legs.map(leg => enrichLeg(leg, now));
  const updated = [makePair(early.coin, left, right, now), makePair(early.coin, right, left, now)]
    .filter(pair => pair?.verified && pair.quality === 'timed');
  for (const pair of updated) replace({...pair, early: true});
}

function selectPairs(pairs, matches, sort, direction, grouped, valueFor = pair => pair[sort]) {
  const sign = direction === 'asc' ? 1 : -1;
  const compare = (a, b) => {
    const left = valueFor(a), right = valueFor(b);
    if (sort !== 'coin' && Number.isFinite(left) !== Number.isFinite(right)) {
      return Number.isFinite(left) ? -1 : 1;
    }
    return (sort === 'coin' ? a.coin.localeCompare(b.coin) : left - right) * sign || 0;
  };
  if (!grouped) return pairs.filter(matches).sort(compare).map(pair => [pair, 0]);
  const groups = new Map();
  for (let index = 0; index < pairs.length; index++) {
    const pair = pairs[index];
    if (!matches(pair)) continue;
    const group = groups.get(pair.coin);
    if (!group) groups.set(pair.coin, {pair, more: 0, index});
    else {
      group.more++;
      if (compare(pair, group.pair) < 0) Object.assign(group, {pair, index});
    }
  }
  // Original position preserves the stable sort order when values tie.
  return [...groups.values()].sort((a, b) => compare(a.pair, b.pair) || a.index - b.index)
    .map(({pair, more}) => [pair, more]);
}

live.fromId = id => live.pairIndex.get(id);
live.resolve = (coin, long, short) => live.pairIndex.get([long?.id, short?.id].join('~'));

async function marketRequest(leg, resource) {
  const response = await fetch('/api/market/' + encodeURIComponent(leg.id) + '/' + resource,
    {signal: AbortSignal.timeout(28000)});
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function levelsFor(leg, short) {
  const book = live.books.get(leg.id);
  if (!book || Date.now() - book.receivedAt > threshold('bookMaxAgeMs') ||
      (book.sourceAt !== null && Date.now() - book.sourceAt > threshold('bookMaxAgeMs'))) return [];
  return short ? book.bids : book.asks;
}

function depthValue(levels, count) {
  return levels.length >= count ? levels.slice(0, count).reduce((sum, [price, size]) => sum + price * size, 0) : null;
}

function slipValue(levels, nominal, short) {
  if (!levels.length) return null;
  const first = levels[0][0];
  const quantity = nominal / first;
  let left = quantity, cost = 0;
  for (const [price, amount] of levels) {
    const size = Math.min(left, amount);
    cost += size * price;
    left -= size;
    if (left <= quantity * 1e-12) return Math.max(0, 100 * (short ? first - cost / quantity : cost / quantity - first) / first);
  }
  return null;
}

const liveQuality = {
  timed: 'Биржевое время сопоставимо', receipt: 'Предварительно · время получения',
  skew: 'Котировки разнесены по времени', missing: 'Нет Bid/Ask', stale: 'Котировки недоступны или устарели',
};
