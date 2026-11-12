/* Fixtures for the prototype. All prices, events and series are illustrative. */
const demo = (() => {
  const exchanges = {
    OKX: ['▦', '#171b21'], Robinlighter: ['◈', '#49682c'], Arcus: ['◔', '#5c482f'],
    HTX: ['♨', '#285fa9'], Gate: ['G', '#2863de'], Bybit: ['B', '#484139'],
    Bitget: ['↯', '#007888'], KuCoin: ['K', '#00846b'], MEXC: ['M', '#1c5dcb'],
    EdgeX: ['▲', '#484956'], WhiteBIT: ['W', '#706021'], Binance: ['◆', '#7b601e'],
  };

  const assets = [
    // Token, exchanges, illustrative prices, funding rates and intervals, 24h delta, entry spread.
    ['AI', 'OKX', 'Robinlighter', 0.01428, 0.02414, 0.005, 0.0012, 4, 1, 0.01, 49.18],
    ['CASHCAT', 'Arcus', 'HTX', 0.07281, 0.093295, 0.0013, 0.0013, 1, 1, 0, 22.74],
    ['MYX', 'Gate', 'HTX', 0.0492, 0.05618, 0.01, 0.005, 8, 4, -0.04, 13.08],
    ['10000NEX', 'Bybit', 'Bitget', 0.00593, 0.00659, 0.0054, 0.005, 4, 4, 0.11, 10.66],
    ['H100', 'Bitget', 'OKX', 2.942, 3.2204, 0.005, -0.4696, 8, 1, -0.73, 9.04],
    ['RAVE', 'KuCoin', 'HTX', 0.13014, 0.14206, 0.0029, 0.005, 4, 4, 0, 8.13],
    ['CLAUDIA', 'Gate', 'MEXC', 0.007673, 0.00833, -0.0083, 0.0123, 1, 4, 11.97, 7.80],
    ['BOME', 'Bitget', 'HTX', 0.0006031, 0.0006442, 0.005, 0.005, 4, 4, 0, 6.16],
    ['BP', 'EdgeX', 'Gate', 0.87705, 0.9444, 0.0505, 0.005, 4, 4, -0.27, 5.96],
    ['JCT', 'Gate', 'KuCoin', 0.01237, 0.0129286, 0.0445, 0.8932, 4, 4, 1.34, 4.78],
    ['EPIC', 'Gate', 'MEXC', 0.088, 0.0904, -0.6088, -0.0143, 4, 4, 5.06, 2.24],
    ['1000CAT', 'Bitget', 'Bybit', 0.001705, 0.001738, -0.019, 0.0013, 4, 1, 0.1, 1.63],
    ['AXL', 'WhiteBIT', 'Bybit', 0.03516, 0.03574, 0.005, 0.005, 4, 4, -0.1, 1.41],
    ['BATON', 'Gate', 'MEXC', 0.005296, 0.005324, 0.005, 0.0119, 4, 4, 0.14, 1.27],
    ['ETH', 'Binance', 'OKX', 3844.12, 3848.40, 0.008, 0.01, 8, 8, 0.015, 0.11],
    ['BTC', 'Binance', 'Bybit', 104180.2, 104269.5, 0.006, 0.009, 8, 8, 0.008, 0.08],
    ['AAPL', 'Gate', 'MEXC', 230.41, 230.71, 0.004, 0.007, 4, 4, 0.02, 0.13, 'stock'],
    ['XAU', 'Bybit', 'Bitget', 4106.1, 4113.5, 0.005, 0.008, 4, 4, 0.08, 0.18, 'commodity'],
  ];

  const markets = {};
  function market(exchange, price, rate, hours, volume, status, extra = {}) {
    return {exchange, price, rate, hours, volume, status, quote: 'USDT', type: 'swap', ...extra};
  }

  function pair(coin, long, short, delta, entry, category = 'crypto') {
    const funding = (short.rate * 8 / short.hours) - (long.rate * 8 / long.hours);
    return {
      id: [coin, short.exchange, short.type, long.exchange, long.type].join('-').toLowerCase(),
      coin, long, short, delta, entry, funding, apr: funding * 1095, category,
      get exit() { return closeSpread(long.price * .9987, short.price * 1.0013); },
      cap: markets[coin].cap, fdv: markets[coin].fdv,
    };
  }

  const pairs = assets.flatMap(([coin, a, b, pa, pb, ra, rb, ha, hb, delta, entry, category], index) => {
    const long = market(a, pa, ra, ha, index === 6 ? 19.74 : 0.47 + index * 1.35, index % 3 === 1 ? [] : [true, true]);
    const short = market(b, pb, rb, hb, index === 6 ? 15.57 : 0.34 + index * 1.12, index === 6 ? [true, false] : index % 4 === 3 ? [true, true] : []);
    if (coin === 'AI') short.quote = 'USDG';
    if (coin === '10000NEX') short.delist = true;
    if (coin === 'CLAUDIA') short.limit = 514.14;
    if (coin === 'MYX') long.noBook = short.noBook = true;

    const alternative = ['Gate', 'Bybit', 'Binance'].find(exchange => exchange !== a && exchange !== b);
    const alt = market(alternative, Number((pa * (1 + entry / 450)).toPrecision(7)), 0.0081, 4, long.volume * 0.78, [true, true]);
    const spotPrice = index % 3 === 0 ? pb * 1.004 : pa * 1.003;
    const spot = market(a, Number(spotPrice.toPrecision(7)), 0, 8, long.volume, long.status, {type: 'spot'});
    markets[coin] = {legs: [long, short, alt, spot], category, cap: 9 + index ** 2 * 14, fdv: 14 + index ** 2 * 21};
    return [
      pair(coin, long, short, delta, entry, category),
      pair(coin, alt, short, delta - 0.12, entry * 0.67, category),
      pair(coin, long, alt, delta * 0.17, entry * 0.23, category),
      pair(coin, spot, short, delta * 0.4, 200 * (pb - spot.price) / (pb + spot.price), category),
    ];
  });

  function resolve(coin, long, short) {
    const id = [coin, short.exchange, short.type, long.exchange, long.type].join('-').toLowerCase();
    const existing = pairs.find(item => item.id === id);
    const reverse = pairs.find(item => item.coin === coin && item.long === short && item.short === long);
    return existing ?? pair(coin, long, short, reverse ? -reverse.delta : (short.rate - long.rate) * 3,
      reverse ? -reverse.entry : 200 * (short.price - long.price) / (short.price + long.price), markets[coin].category);
  }

  function fromId(id) {
    for (const [coin, {legs}] of Object.entries(markets)) {
      for (const long of legs) {
        for (const short of legs) {
          if (long === short || short.type === 'spot') continue;
          if ([coin, short.exchange, short.type, long.exchange, long.type].join('-').toLowerCase() === id) {
            return resolve(coin, long, short);
          }
        }
      }
    }
    return null;
  }

  return {exchanges, markets, pairs, resolve, fromId};
})();
