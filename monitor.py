"""Local read-only arbitrage screener backed by public exchange data."""

import argparse
import asyncio
from collections import defaultdict
from decimal import Decimal, ROUND_FLOOR
import json
import logging
from pathlib import Path
import sqlite3
from time import monotonic
from uuid import uuid4
import webbrowser

import aiohttp
from aiohttp import web
import ccxt.async_support as ccxt
import orjson

from market_monitor.asset_metrics import AssetMetrics
from market_monitor.contract_details import INDEX_VENUES, contract_index, contract_limits
from market_monitor.current_funding import funding_fields
from market_monitor.economics import EconomicBlock, amount_step, check_limits, common_step, economic_message, fill
from market_monitor.exchange_clock import ExchangeClock
from market_monitor.funding_history import DAY, cadence_stats, fetch_history, window_stats
from market_monitor.futures_ws import FuturesBookFeed, PILOT_VENUES, QUOTE_AGE
from market_monitor.history import History
from market_monitor.market_admission import valid_market
from market_monitor.market import MAX_AGE, Registry, decimal, fresh, now_ms, numeric, positive
from market_monitor.rest_budget import RestBudget
from market_monitor.sources import CYCLE_FUNDING_VENUES, SPOT_CLIENTS, VENUES, Source


ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / 'frontend'
INTERVAL = 15
DETAIL_TTL_MS = 300_000
DETAIL_RETRY_MS = 15_000
BINANCE_DEPTH_LIMIT = 4
EARLY_BINANCE_SOURCES = (('binanceusdm', 'swap'), ('kucoinfutures', 'swap'))
EARLY_QUOTE_MAX_AGE = 3000
FUTURES_WS_LIMIT = 30
FUTURES_WS_REFRESH = 180_000
LOG = logging.getLogger(__name__)


def complete_history(result, events, now, error, *, tolerance_ms=1000, interval_ms=None):
    result['events'] = [[event['timestamp'], float(event['rate']) * 100] for event in events]
    if error:
        result['errors']['history'] = error[:250]
        return
    for days in (1, 3, 7):
        coverage = window_stats(events, now, days, tolerance_ms)
        if (coverage['coverage'] == 'regular_observed_coverage'
                and (interval_ms is None or coverage['regular_interval_ms'] == interval_ms)):
            result['totals'][str(days)] = sum(rate for stamp, rate in result['events']
                                              if now - days * DAY < stamp <= now)
    funding = result.get('funding', {})
    if len(events) >= 3 and not funding.get('hours') and funding.get('next'):
        observed = cadence_stats(events[-3:])['regular_interval_ms']
        if observed is not None and abs(funding['next'] - events[-1]['timestamp'] - observed) <= 1000:
            funding['hours'] = observed / 3_600_000


def detail_refresh_after(result, started, batch_next, *, retry_incomplete):
    ttl = DETAIL_RETRY_MS if retry_incomplete and '1' not in result['totals'] else DETAIL_TTL_MS
    detail_next = result.get('funding', {}).get('next')
    return min([started + ttl] + [stamp for stamp in (batch_next, detail_next) if stamp and stamp > started])


def normalized_book(book, size, received, clock=None):
    stamp = numeric(book.get('timestamp'))
    if clock is not None:
        stamp = clock.correct(stamp, received)
    if stamp is not None and not fresh(stamp, received, 3000):
        raise ValueError('стакан просрочен')
    sides = {}
    for side in ('bids', 'asks'):
        levels = []
        for row in book.get(side, [])[:50]:
            price, amount = map(numeric, row[:2])
            if price and amount and price > 0 and amount > 0:
                levels.append([price, amount * size])
        sides[side] = sorted(levels, reverse=side == 'bids')
    if not sides['bids'] or not sides['asks'] or sides['bids'][0][0] >= sides['asks'][0][0]:
        raise ValueError('пустой или пересечённый стакан')
    return dict(**sides, sourceAt=stamp, receivedAt=received)


def compatible_assets(left, right):
    units = left.get('priceUnits', 1)
    if units != right.get('priceUnits', 1):
        return False
    if units != 1 or left.get('nativeBase') or right.get('nativeBase'):
        return bool(left['asset'] and left['asset'] == right['asset'])
    return not (left['asset'] and right['asset'] and left['asset'] != right['asset'])


def pair_coverage(markets, now):
    total = usable = 0
    for legs in markets.values():
        for long in legs:
            for short in legs:
                if (long is short or short['type'] != 'swap' or long['quote'] != short['quote']
                        or (long['exchange'] == short['exchange'] and long['type'] == short['type'])
                        or not compatible_assets(long, short)):
                    continue
                total += 1
                if not all(fresh(leg['receivedAt'], now) and
                           (leg['sourceAt'] is None or fresh(leg['sourceAt'], now)) and
                           leg['bid'] and leg['ask'] for leg in (long, short)):
                    continue
                if max(long['bid'], short['bid']) / min(long['bid'], short['bid']) > 2:
                    continue
                a, b = long['sourceAt'], short['sourceAt']
                if a is not None and b is not None:
                    usable += abs(a - b) <= 1500
                else:
                    usable += abs(long['receivedAt'] - short['receivedAt']) <= 3000
    return dict(total=total, usable=usable, fraction=usable / total if total else None)


def binance_depth_candidates(markets):
    """Select liquid Binance legs with no time-comparable futures counterpart."""
    for legs in markets.values():
        swaps = [leg for leg in legs if leg['type'] == 'swap' and (leg['volume'] or 0) >= 1]
        for leg in swaps:
            if leg['exchange'] != 'Binance':
                continue
            peers = [other for other in swaps if other['exchange'] != 'Binance' and other['quote'] == leg['quote']
                     and other['bid'] and other['ask'] and compatible_assets(leg, other)]
            from_depth = leg.get('quoteSource') == 'depth'
            if not from_depth and leg['bid'] and leg['ask'] and any(
                abs(leg['sourceAt'] - peer['sourceAt']) <= 1500 if leg['sourceAt'] is not None and peer['sourceAt'] is not None
                else abs(leg['receivedAt'] - peer['receivedAt']) <= 3000 for peer in peers
            ):
                continue
            newer = [peer for peer in peers if peer['sourceAt'] is not None and
                     (from_depth or not leg['bid'] or (leg['sourceAt'] is not None and peer['sourceAt'] - leg['sourceAt'] > 1500))]
            if newer:
                yield leg, newer


def estimate_pair(instruments, books, budget, fees, now):
    stamps = [book['sourceAt'] for book in books]
    if not all(fresh(stamp, now, 3000) for stamp in stamps) or abs(stamps[0] - stamps[1]) > 1500:
        raise EconomicBlock('Нужны синхронные стаканы с биржевым временем')
    sides = [{side: [(decimal(price), decimal(size)) for price, size in book[side]]
              for side in ('asks', 'bids')} for book in books]
    left, right = sides
    sizes = [positive(market['contractSize']) for _, market in instruments]
    steps = [amount_step(market, source.client.precisionMode) * size
             for (source, market), size in zip(instruments, sizes)]
    step = common_step(*steps)
    # Size by the marginal cost of opening one additional base unit on both legs.
    quantity = Decimal(0)
    a = iter(left['asks'])
    b = iter(right['bids'])
    ask, aq = next(a)
    bid, bq = next(b)
    while budget > 0:
        take = min(aq, bq)
        level_cost = take * (ask + bid)
        if budget <= level_cost:
            quantity += budget / (ask + bid)
            break
        quantity += take
        budget -= level_cost
        aq -= take
        bq -= take
        if aq == 0:
            level = next(a, None)
            if level is None:
                break
            ask, aq = level
        if bq == 0:
            level = next(b, None)
            if level is None:
                break
            bid, bq = level
    quantity = (quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if quantity <= 0:
        raise EconomicBlock('Бюджета недостаточно для общего шага объёма')
    entry = [fill(left['asks'], quantity), fill(right['bids'], quantity)]
    exit_ = [fill(left['bids'], quantity), fill(right['asks'], quantity)]
    for i, (source, market) in enumerate(instruments):
        contracts = quantity / sizes[i]
        maximum = (contract_limits(source.venue, market) or {}).get('marketMax')
        if maximum is not None and contracts > decimal(maximum):
            raise EconomicBlock(f'{VENUES[source.venue]}: превышен максимум одного рыночного ордера')
        for observations in (entry, exit_):
            check_limits(market, contracts, observations[i][1])
    fee_total = sum((entry[i][1] + exit_[i][1]) * fees[i] for i in range(2))
    gross = (exit_[0][1] - entry[0][1]) + (entry[1][1] - exit_[1][1])
    return dict(quantity=float(quantity), entry=[float(price) for price, _ in entry],
                exit=[float(price) for price, _ in exit_], used=float(entry[0][1] + entry[1][1]),
                fees=float(fee_total), gross=float(gross), net=float(gross - fee_total))


class Monitor:
    def __init__(self, venues, path, *, futures_ws=False):
        self.history = History(path)
        self.registry = Registry.load()
        self.asset_metrics = AssetMetrics(self.registry.coingecko_ids)
        self.sources = {}
        self.clocks = {}
        for venue in venues:
            budget = RestBudget()
            clock = self.clocks[venue] = ExchangeClock()
            self.sources[venue, 'swap'] = Source(venue, 'swap', budget, clock)
            if venue in SPOT_CLIENTS:
                self.sources[venue, 'spot'] = Source(venue, 'spot', budget, clock)
        self.snapshot = dict(state='starting', markets={}, venues={}, valuations={}, generatedAt=None)
        self.snapshot_run_id = uuid4().hex
        self.snapshot_response = (orjson.dumps(self.snapshot), self.snapshot_etag(self.snapshot))
        self.cycle_ms = None
        self.instruments = {}
        self.catalog_descriptors = {}
        self.network_descriptors = {}
        self.snapshot_markets_ms = None
        self.details, self.pending = {}, {}
        self.binance_depth_cursor = self.binance_early_cursor = ''
        self.early_pair = None
        self.early_pair_changed = asyncio.Event()
        self.futures_pairs = []
        self.futures_contracts = {}
        self.futures_refresh_at = 0
        self.futures_feeds = {
            venue: FuturesBookFeed(venue, self.clear_futures_pairs, self.clocks[venue]) for venue in PILOT_VENUES
        } if futures_ws and all((venue, 'swap') in self.sources for venue in PILOT_VENUES) else {}

    def snapshot_etag(self, snapshot):
        return f'W/"{self.snapshot_run_id}-{snapshot["generatedAt"]}"'

    async def publish_snapshot(self, snapshot):
        body = await asyncio.to_thread(orjson.dumps, snapshot)
        self.snapshot = snapshot
        self.snapshot_response = (body, self.snapshot_etag(snapshot))
        self.notify_quotes_changed()

    def clear_futures_pairs(self):
        if self.futures_pairs:
            self.futures_pairs = []
            self.notify_quotes_changed()

    def select_futures_contracts(self, markets):
        ranked = []
        for coin, group in markets.items():
            by_id = {leg['id']: leg for leg in group}
            ids = [f'{venue}.swap.{coin}.USDT' for venue in PILOT_VENUES]
            if any(key not in by_id or key not in self.instruments for key in ids):
                continue
            a, b = (by_id[key] for key in ids)
            if not compatible_assets(a, b):
                continue
            symbols = tuple(self.instruments[key][1]['id'] for key in ids)
            fixed = self.registry.ws_priority.get(coin) == symbols
            volume = min(a['volume'] or 0, b['volume'] or 0)
            if fixed or volume >= 1:
                verified = bool(a['asset'] and a['asset'] == b['asset'])
                ranked.append((not fixed, not verified, -volume, coin, symbols))
        ranked.sort()
        return {coin: symbols for _, _, _, coin, symbols in ranked[:FUTURES_WS_LIMIT]}

    async def refresh_futures_feeds(self, session, markets, now, *, refresh=True):
        if refresh and now >= self.futures_refresh_at:
            self.futures_contracts = self.select_futures_contracts(markets)
            period = FUTURES_WS_REFRESH if len(self.futures_contracts) == FUTURES_WS_LIMIT else INTERVAL * 1000
            self.futures_refresh_at = now + period
        for i, feed in enumerate(self.futures_feeds.values()):
            symbols = frozenset(pair[i] for pair in self.futures_contracts.values())
            if feed.symbols != symbols:
                await feed.close()
                feed.watermarks = {key: value for key, value in feed.watermarks.items() if key in symbols}
                feed.symbols = symbols
            feed.start(session, symbols)

    def update_futures_pairs(self, markets, now):
        pairs = []
        for coin, symbols in self.futures_contracts.items():
            ids = [f'{venue}.swap.{coin}.USDT' for venue in PILOT_VENUES]
            instruments = [self.instruments.get(key) for key in ids]
            if any(item is None or item[1]['id'] != symbol for item, symbol in zip(instruments, symbols)):
                continue
            by_id = {leg['id']: leg for leg in markets.get(coin, ())}
            legs = [by_id[key] for key in ids]
            quotes = [self.futures_feeds[venue].quote(symbol, now)
                      for venue, symbol in zip(PILOT_VENUES, symbols)]
            if not all(quotes) or not compatible_assets(*legs):
                continue
            if abs(quotes[0]['sourceAt'] - quotes[1]['sourceAt']) > 1500:
                continue
            expires_at = min(quote[field] for quote in quotes for field in ('sourceAt', 'receivedAt')) + QUOTE_AGE
            if expires_at > now:
                pairs.append(dict(coin=coin, expiresAt=expires_at,
                                  legs=[dict(leg, **quote) for leg, quote in zip(legs, quotes)]))
        self.futures_pairs = pairs
        self.notify_quotes_changed()

    def set_early_pair(self, pair):
        self.early_pair = pair
        self.notify_quotes_changed()

    def notify_quotes_changed(self):
        self.early_pair_changed.set()
        self.early_pair_changed = asyncio.Event()

    def market_descriptors(self, venue, kind, source):
        catalog = source.catalogs.get(venue)
        if catalog is None:
            self.catalog_descriptors.pop((venue, kind), None)
            return ()
        cached = self.catalog_descriptors.get((venue, kind))
        if source.catalog_at is not None and cached is not None and cached[0] is catalog:
            return cached[1]
        entries = []
        for market in catalog.values():
            binding = self.registry.match(venue, kind, market)
            group = binding.get('group') if binding else None
            if valid_market(market, kind, venue=venue, group=group):
                base, quote = market['base'], market['quote']
                group = group or base
                entries.append((market, binding, group, f'{venue}.{kind}.{group}.{quote}'))
        if source.catalog_at is not None:
            self.catalog_descriptors[venue, kind] = catalog, entries
        return entries

    def network_details(self, venue, base):
        source = self.sources.get((venue, 'spot'))
        if source is None:
            return [], [None, None]
        currency = source.currencies.get(base)
        if currency is None:
            return [], [None, None]
        key = venue, base
        cached = self.network_descriptors.get(key)
        if cached and cached[0] is currency:
            return cached[1:]
        networks = [dict(name=code, deposit=item.get('deposit'), withdraw=item.get('withdraw'))
                    for code, item in (currency.get('networks') or {}).items()
                    if item.get('deposit') is not None or item.get('withdraw') is not None]
        status = []
        for field in ('deposit', 'withdraw'):
            values = [network[field] for network in networks]
            status.append(True if True in values else False if values and all(v is False for v in values) else None)
        self.network_descriptors[key] = currency, networks, status
        return networks, status

    def snapshot_markets(self, now, *, source_keys=None):
        groups, instruments = defaultdict(list), {}
        for venue, kind in self.sources if source_keys is None else source_keys:
            source = self.sources[venue, kind]
            batch = source.batches.get(venue, {})
            batch_received = batch.get('received_at_ms')
            tickers = batch.get('tickers', {})
            for market, binding, group, key in self.market_descriptors(venue, kind, source):
                symbol = market['symbol']
                base, quote = market['base'], market['quote']
                ticker = tickers.get(symbol, {})
                received = ticker.get('quote_received_at_ms', batch_received)
                stamp = numeric(ticker.get('timestamp'))
                clock = self.clocks.get(venue)
                if clock is not None:
                    stamp = clock.correct(stamp, now)
                current = ticker.get('symbol') == symbol and ticker.get('native_id', market['id']) == market['id'] and fresh(received, now) and (
                    stamp is None or fresh(stamp, now))
                if not current:
                    ticker, stamp = {}, None
                bid, ask = numeric(ticker.get('bid')), numeric(ticker.get('ask'))
                if not (bid and ask and 0 < bid <= ask):
                    bid = ask = None
                if key in instruments:
                    # A route must identify exactly one native instrument.
                    instruments[key] = None
                    continue
                instruments[key] = (source, market)
                asset = binding['asset'] if binding else None
                raw = source.funding.get(symbol) or ticker
                raw_info = raw.get('info') or {}
                ticker_info = ticker.get('info') or {}
                turnover_ticker = source.volume.get(symbol, {})
                if (turnover_ticker.get('expiresAt', now + 1) <= now
                        or turnover_ticker.get('native_id', market['id']) != market['id']):
                    turnover_ticker = {}
                volume = numeric(ticker.get('quoteVolume'))
                if volume is None:
                    volume = numeric(turnover_ticker.get('quoteVolume'))
                _, network_status = self.network_details(venue, base)
                detail = self.cached_detail(key, now) or {}
                fields = funding_fields(raw, market, venue, now, source.schedule) if kind == 'swap' else dict(rate=0, hours=None, next=None)
                if kind == 'swap':
                    details_funding = detail.get('funding', {})
                    if venue in CYCLE_FUNDING_VENUES and (details_funding.get('next') or 0) <= now:
                        details_funding = {}
                    for field, value in details_funding.items():
                        if fields[field] is None and value is not None and (field != 'next' or value > now):
                            fields[field] = value
                leg = dict(
                    id=key, exchange=VENUES[venue], type=kind, quote=quote, symbol=symbol,
                    bid=bid, ask=ask, last=numeric(ticker.get('last') or turnover_ticker.get('last')), asset=asset,
                    sourceAt=stamp, receivedAt=received if current else None,
                    index=numeric(raw.get('indexPrice') or raw_info.get('indexPrice')
                                  or raw_info.get('index_price') or ticker_info.get('indexPrice')),
                    oi=numeric(ticker.get('openInterestValue') or ticker_info.get('openInterestValue')),
                    volume=volume / 1_000_000 if volume is not None and volume >= 0 else None,
                    **fields,
                )
                if network_status != [None, None]:
                    leg['status'] = network_status
                if kind == 'swap' and detail.get('totals'):
                    leg['totals'] = detail['totals']
                if group != base:
                    leg['nativeBase'] = base
                if kind == 'swap' and binding and binding['price_units'] != 1:
                    leg['priceUnits'] = binding['price_units']
                groups[group].append(leg)
        result = {}
        for base, legs in groups.items():
            unique = [leg for leg in legs if instruments[leg['id']] is not None]
            usable = [leg for leg in unique if any(
                other is not leg and other['quote'] == leg['quote'] and
                (other['type'] == 'swap' or leg['type'] == 'swap') and
                (other['exchange'] != leg['exchange'] or other['type'] != leg['type']) and
                compatible_assets(leg, other)
                for other in unique)]
            if usable:
                result[base] = usable
        if source_keys is None:
            self.instruments = {leg['id']: instruments[leg['id']] for legs in result.values() for leg in legs}
        return result

    async def binance_depth_quote(self, key):
        source, market = self.instruments[key]
        try:
            async with asyncio.timeout(3):
                raw = await source.client.fetch_order_book(market['symbol'], limit=5)
            if not isinstance(raw, dict) or raw.get('symbol') != market['symbol']:
                raise ValueError('order book symbol does not match the market')
            book = normalized_book(raw, market['contractSize'], now_ms(), self.clocks.get(getattr(source, 'venue', None)))
            if book['sourceAt'] is None:
                raise ValueError('order book has no exchange timestamp')
            return dict(bid=book['bids'][0][0], ask=book['asks'][0][0], sourceAt=book['sourceAt'],
                        receivedAt=book['receivedAt'], quoteSource='depth')
        except (aiohttp.ClientError, ccxt.BaseError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            LOG.debug('Binance depth refresh %s: %s', key, str(exc)[:160] or type(exc).__name__)
            return None

    async def binance_depth_quotes(self, reserved=None):
        candidates = [leg['id'] for leg, _ in binance_depth_candidates(self.snapshot['markets'])
                      if leg['id'] in self.instruments and leg['id'] != reserved]
        candidates.sort(key=lambda key: (key <= self.binance_depth_cursor, key))
        keys = candidates[:BINANCE_DEPTH_LIMIT - int(reserved is not None)]
        if not keys:
            return {}
        self.binance_depth_cursor = keys[-1]
        quotes = await asyncio.gather(*(self.binance_depth_quote(key) for key in keys))
        return {key: quote for key, quote in zip(keys, quotes) if quote is not None}

    async def binance_cycle_depth(self, tickers_ready):
        if not tickers_ready:
            return await self.binance_depth_quotes()
        try:
            async with asyncio.timeout(2):
                await asyncio.gather(*(event.wait() for event in tickers_ready.values()))
        except TimeoutError:
            return await self.binance_depth_quotes()

        ready = now_ms()
        markets = self.snapshot_markets(ready, source_keys=EARLY_BINANCE_SOURCES)
        candidates = {}
        for leg, peers in binance_depth_candidates(markets):
            key = leg['id']
            peer = peers[0]
            if (leg['quote'] != 'USDT' or not leg['asset'] or leg['asset'] != peer['asset']
                    or any(l['id'] not in self.instruments for l in (leg, peer))
                    or not fresh(peer['sourceAt'], ready, EARLY_QUOTE_MAX_AGE)):
                continue
            if any(source.catalogs.get(source.venue, {}).get(market['symbol'], {}).get('id') != market['id']
                   for source, market in (self.instruments[l['id']] for l in (leg, peer))):
                continue
            candidates[key] = (leg, peer)
        if not candidates:
            return await self.binance_depth_quotes()

        key = min(candidates, key=lambda k: (k <= self.binance_early_cursor, k))
        self.binance_early_cursor = key
        leg, peer = candidates[key]
        quote = await self.binance_depth_quote(key)
        quotes = {key: quote} if quote is not None else {}
        now = now_ms()
        if quote is not None:
            stamps = [q[field] for q in (quote, peer) for field in ('sourceAt', 'receivedAt')]
            if all(fresh(stamp, now, EARLY_QUOTE_MAX_AGE) for stamp in stamps):
                early_legs = [dict(leg), peer]
                if self.apply_binance_depth({'early': early_legs}, quotes, now):
                    self.set_early_pair(dict(
                        baseAt=self.snapshot['generatedAt'], coin=key.split('.')[2],
                        expiresAt=min(stamps) + EARLY_QUOTE_MAX_AGE,
                        legs=early_legs,
                    ))
        quotes.update(await self.binance_depth_quotes(reserved=key))
        return quotes

    @staticmethod
    def apply_binance_depth(markets, quotes, now):
        applied = 0
        for leg, peers in binance_depth_candidates(markets):
            quote = quotes.get(leg['id'])
            if (quote and fresh(quote['receivedAt'], now, 3000) and fresh(quote['sourceAt'], now, 3000)
                    and (leg['sourceAt'] is None or quote['sourceAt'] > leg['sourceAt'])
                    and any(abs(quote['sourceAt'] - peer['sourceAt']) <= 1500 for peer in peers)):
                leg.update(quote)
                applied += 1
        return applied

    async def recheck_pybobo(self, markets):
        """Repair skew for the configured verified pair as one update."""
        rule = self.registry.depth_recheck
        if rule is None:
            return None
        keys = rule['markets']
        by_id = {leg['id']: leg for leg in markets.get(rule['group'], ())}
        if any(key not in by_id or key not in self.instruments for key in keys):
            return None
        legs = [by_id[key] for key in keys]
        if any(leg['asset'] != rule['asset'] or not (leg['bid'] and leg['ask'])
               or leg['sourceAt'] is None for leg in legs) or not compatible_assets(*legs):
            return None
        bulk_skew = abs(legs[0]['sourceAt'] - legs[1]['sourceAt'])
        if bulk_skew <= 1500:
            return dict(state='bulk_aligned', bulkSkewMs=bulk_skew)

        async def read(key):
            source, market = self.instruments[key]
            try:
                async with asyncio.timeout(3):
                    raw = await source.public_book(market)
                if not isinstance(raw, dict) or raw.get('symbol') != market['symbol']:
                    raise ValueError('order book symbol does not match the market')
                return normalized_book(raw, market['contractSize'], now_ms(), self.clocks.get(getattr(source, 'venue', None)))
            except (aiohttp.ClientError, ccxt.BaseError, TimeoutError, ValueError, KeyError, TypeError) as exc:
                LOG.debug('Paired depth refresh %s: %s', key, str(exc)[:160] or type(exc).__name__)
                return None

        books = await asyncio.gather(*(read(key) for key in keys))
        now = now_ms()
        if any(book is None or not fresh(book['sourceAt'], now, 3000)
               or not fresh(book['receivedAt'], now, 3000) for book in books):
            return dict(state='unavailable', bulkSkewMs=bulk_skew)
        depth_skew = abs(books[0]['sourceAt'] - books[1]['sourceAt'])
        if depth_skew > 1500:
            return dict(state='depth_skew', bulkSkewMs=bulk_skew, depthSkewMs=depth_skew)
        for leg, book in zip(legs, books):
            leg.update(bid=book['bids'][0][0], ask=book['asks'][0][0], sourceAt=book['sourceAt'],
                       receivedAt=book['receivedAt'], quoteSource='depth')
        return dict(state='applied', bulkSkewMs=bulk_skew, depthSkewMs=depth_skew)

    async def run_asset_metrics(self, session):
        while True:
            try:
                await self.asset_metrics.run(session)
                return
            except Exception:
                LOG.exception('Asset valuation worker failed')
                await asyncio.sleep(30)

    def health(self, now):
        venues = {}
        for (venue, kind), source in self.sources.items():
            batch = source.batches.get(venue)
            age = max(0, now - batch['received_at_ms']) if batch else None
            venues[f'{venue}.{kind}'] = dict(
                ticker=source.ticker_status.get(venue, {}).get('state', 'starting'),
                catalog=source.catalog_status.get(venue, {'state': 'starting'}),
                collectMs=source.collect_ms, batchAgeMs=age, batchStale=age is None or age > MAX_AGE,
                turnoverError=source.turnover_error,
            )
        budgets = {venue: source.budget for (venue, kind), source in self.sources.items()}
        return dict(
            state=self.snapshot['state'], generatedAt=self.snapshot['generatedAt'], cycleMs=self.cycle_ms,
            snapshotMarketsMs=self.snapshot_markets_ms, sources=venues, rest={venue: dict(requests=budget.requests, rateLimits=budget.rate_limits)
                                  for venue, budget in budgets.items()},
            clocks={venue: dict(offsetMs=offset, warning=offset is not None and abs(offset) > 500)
                    for venue, clock in self.clocks.items() if (offset := clock.estimate(now)) is not None},
            reconnects={venue: feed.reconnects for venue, feed in self.futures_feeds.items()},
        )

    async def run(self):
        last_slot = None
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session, asyncio.TaskGroup() as tasks:
            tasks.create_task(self.run_asset_metrics(session))
            for source in self.sources.values():
                source.open(session)
            while True:
                started = monotonic()
                tickers_ready = ({key: asyncio.Event() for key in EARLY_BINANCE_SOURCES}
                                 if self.snapshot['generatedAt'] and all(key in self.sources for key in EARLY_BINANCE_SOURCES)
                                 else {})
                *outcomes, quotes = await asyncio.gather(
                    *(source.collect(tickers_ready[key]) if key in tickers_ready else source.collect()
                      for key, source in self.sources.items()),
                    self.binance_cycle_depth(tickers_ready), return_exceptions=True)
                statuses = {}
                for ((venue, kind), source), outcome in zip(self.sources.items(), outcomes):
                    status = dict(source.catalog_status.get(venue, {}))
                    if status.get('state') in ('ok', 'stale'):
                        ticker = source.ticker_status.get(venue, {})
                        status = dict(ticker, catalog=status)
                    elif not status:
                        status = dict(state='starting')
                    batch = source.batches.get(venue)
                    status['batchAt'] = batch['received_at_ms'] if batch else None
                    if isinstance(outcome, Exception):
                        LOG.warning('%s %s: %s', venue, kind, str(outcome)[:250])
                        status = dict(state='error', error=str(outcome)[:250])
                    statuses[f'{VENUES[venue]} · {kind}'] = status
                now = now_ms()
                started_snapshot = monotonic()
                markets = self.snapshot_markets(now)
                self.snapshot_markets_ms = round((monotonic() - started_snapshot) * 1000, 1)
                pybobo_depth = await self.recheck_pybobo(markets)
                now = now_ms()
                if isinstance(quotes, Exception):
                    LOG.warning('Binance depth refresh: %s', str(quotes)[:200])
                elif quotes:
                    applied = self.apply_binance_depth(markets, quotes, now)
                    statuses['Binance · swap'] = {**statuses['Binance · swap'], 'depth_refreshed': applied}
                if self.futures_feeds:
                    inputs_ready = all(statuses[f'{VENUES[venue]} · swap'].get('state') == 'ok'
                                       for venue in PILOT_VENUES)
                    await self.refresh_futures_feeds(session, markets, now, refresh=inputs_ready)
                    now = now_ms()
                snapshot = dict(
                    state='ready' if all(s.get('state') == 'ok' for s in statuses.values()) else 'partial',
                    venues=statuses, markets=markets, valuations=self.asset_metrics.snapshot(now), generatedAt=now,
                    expectedIntervalMs=INTERVAL * 1000,
                )
                if pybobo_depth is not None:
                    snapshot['pyboboDepth'] = pybobo_depth
                await self.publish_snapshot(snapshot)
                self.cycle_ms = round((monotonic() - started) * 1000, 1)
                if self.futures_feeds:
                    self.update_futures_pairs(markets, now_ms())
                if self.early_pair is not None:
                    self.set_early_pair(None)
                self.details = {key: item for key, item in self.details.items() if item['refreshAfter'] > now}
                if now // 60_000 != last_slot and markets:
                    try:
                        await self.history.run(self.history.record, [leg for legs in markets.values() for leg in legs], now)
                        last_slot = now // 60_000
                    except (sqlite3.Error, OSError):
                        LOG.exception('Unable to store market history')
                await asyncio.sleep(max(1, INTERVAL - (monotonic() - started)))

    async def load_detail(self, source, market):
        client, symbol = source.client, market['symbol']
        now = now_ms()
        result = dict(errors={}, totals={}, events=[])

        async def load(name, method, *args, **kwargs):
            try:
                async with asyncio.timeout(10):
                    return await method(*args, **kwargs)
            except (aiohttp.ClientError, ccxt.BaseError, TimeoutError, ValueError, AttributeError, KeyError, TypeError) as exc:
                result['errors'][name] = str(exc)[:250] or type(exc).__name__
                return None

        if source.venue in ('edgex', 'extended'):
            events = await load('history', source.public_funding_history, market, now)
            if events is not None:
                # Extended publishes application times, which vary by a few
                # seconds around the hourly settlement. Keep original times.
                if source.venue == 'extended':
                    complete_history(result, events, now, None, tolerance_ms=10_000, interval_ms=3_600_000)
                else:
                    complete_history(result, events, now, None)
            return result
        if source.kind == 'swap':
            if client.has.get('fetchFundingRate') is True:
                raw = await load('funding', client.fetch_funding_rate, symbol)
                if raw and raw.get('symbol') in (None, symbol):
                    result['funding'] = funding_fields(raw, market, source.venue, now, source.schedule)
            if client.has.get('fetchFundingRateHistory') is True:
                report = await load('history', fetch_history, client, source.venue,
                                    symbol, now - 8 * DAY, now, max_pages=4)
                if report:
                    complete_history(result, report['events'], now, report['error'])
        return result

    def cached_detail(self, key, now):
        cached = self.details.get(key)
        return cached if cached and now < cached['refreshAfter'] else None

    async def detail(self, key):
        cached = self.cached_detail(key, now_ms())
        if cached is not None:
            return cached
        task = self.pending.get(key)
        if task is None:
            if len(self.pending) >= 8:
                raise web.HTTPTooManyRequests(text='Лимит запросов карточек')
            task = self.pending[key] = asyncio.create_task(self.cache_detail(key))
        return await asyncio.shield(task)

    async def cache_detail(self, key):
        try:
            source, market = self.instruments[key]
            started = now_ms()
            batch = source.funding.get(market['symbol'], {})
            next_at = funding_fields(batch, market, source.venue, started, source.schedule)['next']
            if source.kind == 'swap' and source.venue in INDEX_VENUES:
                result, index = await asyncio.gather(self.load_detail(source, market), self.load_index(source, market))
                result.update(index)
            else:
                result = await self.load_detail(source, market)
            result['refreshAfter'] = detail_refresh_after(
                result, started, next_at, retry_incomplete=source.kind == 'swap' and source.venue in CYCLE_FUNDING_VENUES,
            )
            self.details[key] = result
            return result
        finally:
            self.pending.pop(key, None)

    async def load_index(self, source, market):
        try:
            async with asyncio.timeout(6):
                catalog_at = source.catalog_status.get(source.venue, {}).get('checked_at_ms')
                return {'indexComposition': await contract_index(source.venue, source.client, market, catalog_at)}
        except (aiohttp.ClientError, ccxt.BaseError, TimeoutError, ValueError, AttributeError, KeyError, TypeError) as exc:
            LOG.warning('Index %s %s: %s', source.venue, market['id'], str(exc)[:180] or type(exc).__name__)
            return {'indexComposition': None, 'indexUnavailable': True}

    async def close(self):
        await asyncio.gather(*(feed.close() for feed in self.futures_feeds.values()))
        for task in self.pending.values():
            task.cancel()
        await asyncio.gather(*self.pending.values(), return_exceptions=True)
        await asyncio.gather(*(source.close() for source in self.sources.values()))
        await self.history.shutdown()


def create_app(monitor, *, collect=True, open_url=None):
    @web.middleware
    async def local_requests(request, handler):
        endpoint = request.transport.get_extra_info('sockname') if request.transport else None
        if not endpoint or request.host.lower() not in (
            f'127.0.0.1:{endpoint[1]}', f'localhost:{endpoint[1]}',
        ):
            raise web.HTTPBadRequest(text='Недопустимый Host')
        return await handler(request)

    async def protect_response(_request, response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )

    app = web.Application(middlewares=[local_requests])
    app.on_response_prepare.append(protect_response)
    depth = {}
    depth_pending = {}

    async def lifecycle(_app):
        task = asyncio.create_task(monitor.run()) if collect else None
        browser_task = None
        if open_url:
            async def open_browser():
                await asyncio.sleep(0.8)
                await asyncio.to_thread(webbrowser.open, open_url)

            browser_task = asyncio.create_task(open_browser())
        try:
            yield
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if browser_task:
                browser_task.cancel()
                await asyncio.gather(browser_task, return_exceptions=True)
            pending = tuple(depth_pending.values())
            for pending_task in pending:
                pending_task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await monitor.close()

    def payload(data):
        response = web.json_response(data, headers={'Cache-Control': 'no-store'})
        response.enable_compression()
        return response

    async def snapshot(_request):
        snapshot_body, snapshot_etag = monitor.snapshot_response
        headers = {'Cache-Control': 'no-store', 'ETag': snapshot_etag}
        candidates = _request.headers.get('If-None-Match', '').split(',')
        if any(tag.strip() in ('*', snapshot_etag, snapshot_etag[2:]) for tag in candidates):
            return web.Response(status=304, headers=headers)
        response = web.Response(body=snapshot_body, content_type='application/json',
                                headers=headers)
        response.enable_compression()
        return response

    async def health(request):
        now = now_ms()
        result = monitor.health(now)
        if request.query.get('coverage') == '1':
            result['pairCoverage'] = await asyncio.to_thread(pair_coverage, monitor.snapshot['markets'], now)
        return payload(result)

    stream_stopping = asyncio.Event()

    async def stop_streams(_app):
        stream_stopping.set()
        monitor.early_pair_changed.set()

    async def early_quotes(request):
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store'})
        await response.prepare(request)
        sent_snapshot = None
        try:
            while not stream_stopping.is_set():
                changed = monitor.early_pair_changed
                pair = monitor.early_pair
                message = (pair if pair is not None and pair['expiresAt'] > now_ms()
                           else dict(baseAt=monitor.snapshot['generatedAt'], legs=None))
                frames = f'data: {json.dumps(message)}\n\n'
                generated = monitor.snapshot['generatedAt']
                if generated != sent_snapshot:
                    frames += f'event: snapshot-ready\ndata: {json.dumps(dict(generatedAt=generated))}\n\n'
                    sent_snapshot = generated
                if monitor.futures_feeds:
                    now = now_ms()
                    event = dict(baseAt=monitor.snapshot['generatedAt'],
                                 pairs=[pair for pair in monitor.futures_pairs if pair['expiresAt'] > now])
                    frames += f'event: futures-quotes\ndata: {json.dumps(event)}\n\n'
                async with asyncio.timeout(3):
                    await response.write(frames.encode())
                try:
                    async with asyncio.timeout(10):
                        await changed.wait()
                except TimeoutError:
                    if request.transport is None or request.transport.is_closing():
                        break
        except (ConnectionError, TimeoutError):
            pass
        return response

    def instrument(request):
        key = request.match_info['key']
        if key not in monitor.instruments:
            raise web.HTTPNotFound(text='Рынок недоступен в текущем каталоге')
        return key

    async def details(request):
        key = instrument(request)
        details, samples = await asyncio.gather(
            monitor.detail(key), monitor.history.run(monitor.history.read, key, now_ms()))
        source, market = monitor.instruments.get(key, (None, None))
        limits = contract_limits(source.venue, market) if source and source.kind == 'swap' else None
        networks, _ = monitor.network_details(source.venue, market.get('base'))
        return payload(dict(details, samples=samples, contractLimits=limits, networks=networks))

    async def get_book(key):
        cached = depth.get(key)
        now = now_ms()
        if (cached and fresh(cached['receivedAt'], now, 3000)
                and (cached['sourceAt'] is None or fresh(cached['sourceAt'], now, 3000))):
            return cached
        task = depth_pending.get(key)
        if task is None:
            if len(depth_pending) >= 4:
                raise web.HTTPTooManyRequests(text='Лимит одновременных запросов стакана')
            task = depth_pending[key] = asyncio.create_task(load_book(key))
            # Retrieve failures even if all HTTP callers have disconnected.
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(task)

    async def load_book(key):
        try:
            return await fetch_book(key)
        finally:
            depth_pending.pop(key, None)

    async def fetch_book(key):
        source, market = monitor.instruments[key]
        try:
            async with asyncio.timeout(8):
                raw = await source.public_book(market)
                if raw.get('symbol') not in (None, market['symbol']):
                    raise ValueError('стакан другого инструмента')
                size = market['contractSize'] if source.kind == 'swap' else 1
                result = normalized_book(raw, size, now_ms(), monitor.clocks.get(getattr(source, 'venue', None)))
        except (aiohttp.ClientError, ccxt.BaseError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            LOG.warning('Depth %s: %s', key, str(exc)[:250])
            raise web.HTTPServiceUnavailable(text='Публичный стакан временно недоступен') from exc
        depth_copy = {item: value for item, value in depth.items() if fresh(value['receivedAt'], now_ms(), 3000)}
        depth.clear()
        depth.update(depth_copy)
        depth[key] = result
        return result

    async def book(request):
        return payload(await get_book(instrument(request)))

    async def executable_spread(request):
        try:
            data = await request.json()
            nominal = positive(data['nominal'])
            pairs = data['pairs']
            if not Decimal(1) <= nominal <= Decimal(1_000_000) or not isinstance(pairs, list) or not 1 <= len(pairs) <= 2:
                raise ValueError
            if not all(isinstance(pair, list) and len(pair) == 2 and
                       all(isinstance(key, str) and key in monitor.instruments for key in pair)
                       and pair[0] != pair[1] for pair in pairs):
                raise ValueError
        except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
            raise web.HTTPBadRequest(text='Укажите номинал и до двух доступных связок') from exc

        async def measure(keys):
            legs = [monitor.instruments[key] for key in keys]
            (left_source, left), (right_source, right) = legs
            bindings = [monitor.registry.match(source.venue, source.kind, market) for source, market in legs]
            if (right_source.kind != 'swap' or left['quote'] != right['quote']
                    or not all(bindings) or bindings[0]['asset'] != bindings[1]['asset']
                    or bindings[0].get('price_units', 1) != bindings[1].get('price_units', 1)):
                return dict(executableSpread=None, reason='Нужны две совместимые ноги из реестра')
            try:
                books = await asyncio.gather(*(get_book(key) for key in keys))
                a, b = (book['sourceAt'] for book in books)
                now = now_ms()
                if not (fresh(a, now, 3000) and fresh(b, now, 3000) and abs(a - b) <= 1500):
                    raise EconomicBlock('Нужны синхронные стаканы с биржевым временем')
                asks = [(decimal(price), decimal(size)) for price, size in books[0]['asks']]
                bids = [(decimal(price), decimal(size)) for price, size in books[1]['bids']]
                quantity = nominal * 2 / (asks[0][0] + bids[0][0])
                ask, _ = fill(asks, quantity)
                bid, _ = fill(bids, quantity)
                return dict(executableSpread=float(200 * (bid - ask) / (ask + bid)),
                            expiresAt=min(a, b, *(book['receivedAt'] for book in books)) + 3000)
            except EconomicBlock as exc:
                return dict(executableSpread=None, reason=economic_message(str(exc)))
            except web.HTTPException as exc:
                return dict(executableSpread=None, reason=exc.text)

        return payload(dict(nominal=float(nominal), results=await asyncio.gather(*(measure(pair) for pair in pairs))))

    async def estimate(request):
        keys = [request.query.get(side) for side in ('long', 'short')]
        if keys[0] == keys[1] or any(key not in monitor.instruments for key in keys):
            raise web.HTTPBadRequest(text='Выберите два разных рынка')
        instruments = [monitor.instruments[key] for key in keys]
        sources, markets = zip(*instruments)
        bindings = [monitor.registry.match(source.venue, source.kind, market)
                    for source, market in instruments]
        if (any(source.kind != 'swap' for source in sources)
                or not all(bindings) or bindings[0]['asset'] != bindings[1]['asset']
                or bindings[0]['price_units'] != bindings[1]['price_units']
                or markets[0]['quote'] != markets[1]['quote']):
            raise web.HTTPBadRequest(text='Для расчёта нужна пара совместимых контрактов из реестра')
        try:
            budget = positive(request.query.get('budget'))
            fees = [decimal(request.query.get(key)) / 100 for key in ('longFee', 'shortFee')]
            if budget > 1_000_000 or any(not 0 <= fee <= Decimal('.01') for fee in fees):
                raise ValueError('Бюджет до 1 000 000, комиссии от 0 до 1%')
            books = await asyncio.gather(*(get_book(key) for key in keys))
            return payload(estimate_pair(instruments, books, budget, fees, now_ms()))
        except EconomicBlock as exc:
            raise web.HTTPUnprocessableEntity(text=economic_message(str(exc))) from exc
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise web.HTTPUnprocessableEntity(text='Проверьте бюджет и ставки комиссий') from exc

    async def index(_request):
        return web.FileResponse(FRONTEND / 'index.html')

    app.cleanup_ctx.append(lifecycle)
    app.on_shutdown.append(stop_streams)
    app.router.add_get('/api/markets', snapshot)
    app.router.add_get('/api/health', health)
    app.router.add_get('/api/markets/early', early_quotes)
    app.router.add_get('/api/market/{key}/detail', details)
    app.router.add_get('/api/market/{key}/book', book)
    app.router.add_get('/api/estimate', estimate)
    app.router.add_post('/api/executable-spread', executable_spread)
    app.router.add_get('/', index)
    app.router.add_static('/', FRONTEND)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8780)
    parser.add_argument('--venues', nargs='+', choices=VENUES, default=tuple(VENUES))
    parser.add_argument('--history', type=Path, default=ROOT / 'data' / 'monitor.sqlite3')
    parser.add_argument('--futures-ws', action='store_true', help='Pilot MEXC and KuCoin futures book streams')
    parser.add_argument('--open', action='store_true', help='Open the screener in the default browser')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    url = f'http://127.0.0.1:{args.port}/' if args.open else None
    web.run_app(create_app(Monitor(dict.fromkeys(args.venues), args.history, futures_ws=args.futures_ws), open_url=url),
                host='127.0.0.1', port=args.port, handler_cancellation=True)


if __name__ == '__main__':
    main()
