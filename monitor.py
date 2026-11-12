"""Local read-only arbitrage screener backed by public exchange data."""

import argparse
import asyncio
import logging
from pathlib import Path
import sqlite3
from time import monotonic
from uuid import uuid4

import aiohttp
from aiohttp import web
import ccxt.async_support as ccxt
import orjson

from market_monitor.asset_metrics import AssetMetrics
from market_monitor.books import normalized_book
from market_monitor.contract_details import INDEX_VENUES, contract_index
from market_monitor.current_funding import funding_fields
from market_monitor.exchange_clock import ExchangeClock
from market_monitor.funding_history import DAY, cadence_stats, fetch_history, window_stats
from market_monitor.futures_ws import FuturesBookFeed, PILOT_VENUES, QUOTE_AGE
from market_monitor.history import History
from market_monitor.history_selection import WATCH_TTL_MS, history_legs
from market_monitor.http_api import create_app
from market_monitor.market import MAX_AGE, Registry, compatible_assets, fresh, now_ms, numeric
from market_monitor.rest_budget import RestBudget
from market_monitor.snapshot import SnapshotBuilder
from market_monitor.sources import CYCLE_FUNDING_VENUES, SPOT_CLIENTS, VENUES, Source


ROOT = Path(__file__).resolve().parent
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


class Monitor:
    def __init__(self, venues, path, *, futures_ws=False):
        self.history = History(path)
        self.watched_history_keys = {}
        self.watched_history_groups = {}
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
        self.snapshot_builder = SnapshotBuilder(self.sources, self.clocks, self.registry, self.cached_detail)
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

    def network_details(self, venue, base):
        return self.snapshot_builder.network_details(venue, base)

    def snapshot_markets(self, now, *, source_keys=None):
        markets, routes = self.snapshot_builder.build(now, source_keys=source_keys)
        if source_keys is None:
            self.instruments = routes.copy()
        return markets

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

    def watch_history(self, keys, groups, now):
        self.watched_history_keys = {key: deadline for key, deadline in self.watched_history_keys.items()
                                     if deadline > now and key in self.instruments}
        self.watched_history_groups = {group: deadline for group, deadline in self.watched_history_groups.items()
                                       if deadline > now and group in self.snapshot['markets']}
        self.watched_history_keys.update((key, now + WATCH_TTL_MS) for key in keys if key in self.instruments)
        self.watched_history_groups.update((group, now + WATCH_TTL_MS) for group in groups
                                           if group in self.snapshot['markets'])

    def record_history(self, markets, now, keys, groups):
        self.history.record(history_legs(markets, now, keys, groups), now)

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
                        self.watch_history((), (), now)
                        await self.history.run(self.record_history, markets, now,
                                               set(self.watched_history_keys), set(self.watched_history_groups))
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
