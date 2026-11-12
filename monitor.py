"""Local read-only arbitrage screener backed by public exchange data."""

import argparse
import asyncio
from collections import defaultdict
from itertools import combinations
import logging
from pathlib import Path
import sqlite3
from time import monotonic
from uuid import uuid4

import aiohttp
from aiohttp import web
import ccxt.async_support as ccxt

from market_monitor.asset_metrics import AssetMetrics
from market_monitor.clock_calibration import ClockCalibration
from market_monitor.classification import valid_market
from market_monitor.contract_details import INDEX_VENUES, contract_index
from market_monitor.funding_fields import funding_fields
from market_monitor.funding_history import DAY, cadence_stats, fetch_history, window_stats
from market_monitor.futures_ws import FUTURES_WS_VENUES, FuturesBookFeed, PILOT_CONTRACTS, PILOT_VENUES
from market_monitor.history import History, eligible_legs
from market_monitor.http_server import create_app as create_http_app
from market_monitor.market import Registry, fresh, now_ms, numeric
from market_monitor.pair_economics import estimate_pair as estimate_pair, normalized_book
from market_monitor.rest_budget import RestBudget
from market_monitor.snapshot_wire import encode_compact
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
FUTURES_WS_HYSTERESIS = 10
FUTURES_WS_REFRESH = 180_000
FUTURES_REST_CHECK_INTERVAL = 30_000
FUTURES_REST_CHECKS_PER_VENUE = 2
PYBOBO_DEPTH_KEYS = ('kucoinfutures.swap.PYBOBO.USDT', 'mexc.swap.PYBOBO.USDT')
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


def compatible_assets(left, right):
    units = left.get('priceUnits', 1)
    if units != right.get('priceUnits', 1):
        return False
    if units != 1 or left.get('nativeBase') or right.get('nativeBase'):
        return bool(left['asset'] and left['asset'] == right['asset'])
    return not (left['asset'] and right['asset'] and left['asset'] != right['asset'])


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
        self.registry = Registry.load()
        self.asset_metrics = AssetMetrics(self.registry.coingecko_ids)
        self.sources = {}
        self.clocks = {}
        for venue in venues:
            budget = RestBudget()
            clock = self.clocks[venue] = ClockCalibration(venue)
            self.sources[venue, 'swap'] = Source(venue, 'swap', budget, clock=clock)
            if venue in SPOT_CLIENTS:
                self.sources[venue, 'spot'] = Source(venue, 'spot', budget, clock=clock)
        self.snapshot = dict(state='starting', markets={}, venues={}, valuations={}, generatedAt=None)
        self.snapshot_run = uuid4().hex
        self.snapshot_revision = 0
        self.encoded_snapshot = None
        self.compact_snapshot = None
        self.snapshot_metadata = None
        self.metadata_version = None
        self.metadata_packets = {}
        self.cycle_started = None
        self.cycle_duration_ms = None
        self.collection_failed = False
        self.snapshot_build_ms = None
        self.event_loop_lag_ms = None
        self.event_loop_lag_max_ms = None
        self._event_loop_lag_handle = None
        self.source_duration_ms = {}
        self.instruments = {}
        self.catalog_descriptors = {}
        self.details, self.pending = {}, {}
        self.binance_depth_cursor = self.binance_early_cursor = ''
        self.early_pair = None
        self.early_pair_changed = asyncio.Event()
        self.futures_pairs = []
        self.futures_contracts = {}
        self.futures_symbols = {}
        self.futures_refresh_at = 0
        self.futures_update = None
        self.futures_rest_checked = {}
        self.closing = False
        self.futures_feeds = {
            venue: FuturesBookFeed(venue, lambda venue=venue: self.clear_futures_pairs(venue),
                                   clock=self.clocks[venue],
                                   on_update=self.schedule_futures_update)
            for venue in FUTURES_WS_VENUES if (venue, 'swap') in self.sources
        } if futures_ws else {}

    def clear_futures_pairs(self, venue=None, symbols=None):
        if venue is None:
            remaining = []
        else:
            symbols = None if symbols is None else set(symbols)
            def affected(leg):
                instrument = self.instruments.get(leg['id'])
                if instrument is None:
                    return leg['id'].startswith(f'{venue}.swap.') and symbols is None
                source, market = instrument
                return source.venue == venue and (symbols is None or market.get('id') in symbols)

            remaining = [pair for pair in self.futures_pairs if not any(affected(leg) for leg in pair['legs'])]
        if remaining != self.futures_pairs:
            self.futures_pairs = remaining
            self.notify_quotes_changed()

    def schedule_futures_update(self):
        if not self.closing and self.futures_update is None:
            self.futures_update = asyncio.get_running_loop().call_later(.5, self.refresh_futures_quotes)

    def refresh_futures_quotes(self):
        self.futures_update = None
        if self.snapshot['generatedAt'] is not None:
            self.update_futures_pairs(self.snapshot['markets'], now_ms())

    def schedule_event_loop_lag_probe(self):
        if self.closing or self._event_loop_lag_handle is not None:
            return
        loop = asyncio.get_running_loop()
        expected = loop.time() + 1
        self._event_loop_lag_handle = loop.call_later(1, self.record_event_loop_lag, expected)

    def record_event_loop_lag(self, expected):
        self._event_loop_lag_handle = None
        loop = asyncio.get_running_loop()
        self.event_loop_lag_ms = max(0, round((loop.time() - expected) * 1000))
        if self.event_loop_lag_max_ms is None or self.event_loop_lag_ms > self.event_loop_lag_max_ms:
            self.event_loop_lag_max_ms = self.event_loop_lag_ms
        self.schedule_event_loop_lag_probe()

    def cancel_event_loop_lag_probe(self):
        if self._event_loop_lag_handle is not None:
            self._event_loop_lag_handle.cancel()
            self._event_loop_lag_handle = None

    @staticmethod
    def stabilize_subscription_rank(ranked, existing, limit, *, key, volume_index):
        """Keep near-cutoff incumbents unless a new candidate materially outranks them."""
        if len(ranked) <= limit:
            return ranked
        by_key = {key(item): item for item in ranked}
        selected = list(ranked[:limit])
        incumbents = set(existing)
        incoming = [item for item in selected if key(item) not in incumbents]
        near_cutoff = {key(item) for item in ranked[limit:limit + FUTURES_WS_HYSTERESIS]}
        outgoing = [by_key[symbol] for symbol in incumbents & near_cutoff]
        for old in outgoing:
            if not incoming:
                break
            newcomer = max(incoming)
            old_category = old[:volume_index]
            new_category = newcomer[:volume_index]
            old_volume = -old[volume_index]
            new_volume = -newcomer[volume_index]
            materially_better = new_category < old_category or (
                new_category == old_category and new_volume >= old_volume * 1.2
            )
            if materially_better:
                incoming.remove(newcomer)
                continue
            selected.remove(newcomer)
            selected.append(old)
            incoming.remove(newcomer)
        return sorted(selected)

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
            fixed = PILOT_CONTRACTS.get(coin) == symbols
            volume = min(a['volume'] or 0, b['volume'] or 0)
            if fixed or volume >= 1:
                verified = bool(a['asset'] and a['asset'] == b['asset'])
                ranked.append((not fixed, not verified, -volume, coin, symbols))
        ranked.sort()
        ranked = self.stabilize_subscription_rank(
            ranked, self.futures_contracts, FUTURES_WS_LIMIT,
            key=lambda item: item[3], volume_index=2,
        )
        return {coin: symbols for _, _, _, coin, symbols in ranked}

    async def refresh_futures_feeds(self, session, markets, now, *, refresh=True, ready_venues=None):
        if refresh and now >= self.futures_refresh_at:
            ready = set(self.futures_feeds) if ready_venues is None else ready_venues
            if all(venue in ready for venue in PILOT_VENUES):
                self.futures_contracts = self.select_futures_contracts(markets)
            if set(self.futures_feeds) == set(PILOT_VENUES):
                self.futures_symbols = {venue: frozenset(pair[i] for pair in self.futures_contracts.values())
                                        for i, venue in enumerate(PILOT_VENUES)}
            else:
                selected = self.select_futures_symbols(markets)
                self.futures_symbols = {venue: selected.get(venue, frozenset()) if venue in ready else
                                        self.futures_symbols.get(venue, frozenset()) for venue in self.futures_feeds}
            populated = any(len(symbols) >= FUTURES_WS_LIMIT for symbols in self.futures_symbols.values())
            period = FUTURES_WS_REFRESH if populated else INTERVAL * 1000
            self.futures_refresh_at = now + period
        for venue, feed in self.futures_feeds.items():
            symbols = self.futures_symbols.get(venue, frozenset())
            if feed.symbols != symbols:
                removed = feed.symbols - symbols
                self.clear_futures_pairs(venue, removed)
            feed.start(session, symbols)

    def futures_candidates(self, group):
        for leg in group:
            instrument = self.instruments.get(leg['id'])
            if instrument:
                source, market = instrument
                if source.kind == 'swap' and source.venue in self.futures_feeds and leg['quote'] == 'USDT':
                    yield leg, source.venue, market['id']

    async def check_quiet_futures_symbols(self, now):
        """Recheck a small rotating set of quiet Binance/Gate WS symbols over REST."""
        checks = []
        for venue, feed in self.futures_feeds.items():
            source = self.sources.get((venue, 'swap'))
            if source is None:
                continue
            checked = self.futures_rest_checked.setdefault(venue, {})
            eligible = [symbol for symbol in feed.quiet_symbols(now)
                        if now - checked.get(symbol, 0) >= FUTURES_REST_CHECK_INTERVAL]
            for symbol in eligible[:FUTURES_REST_CHECKS_PER_VENUE]:
                market = next((item for item in source.client.markets.values() if item.get('id') == symbol), None)
                if market is None:
                    continue
                checked[symbol] = now
                checks.append((venue, symbol, feed, source, market, feed.rest_token(symbol)))

        async def check(item):
            venue, symbol, feed, source, market, token = item
            try:
                raw = await source.public_book(market)
                received = now_ms()
                book = normalized_book(raw, market['contractSize'], received, clock=self.clocks.get(venue))
                if not book['bids'] or not book['asks']:
                    return
                feed.confirm_rest(symbol, token, book['bids'][0][0], book['asks'][0][0], received,
                                  changed_at=book['sourceAt'])
            except Exception as exc:
                LOG.debug('%s quiet-symbol REST check failed for %s: %s', venue, symbol, exc)

        if checks:
            await asyncio.gather(*(check(item) for item in checks))

    def select_futures_symbols(self, markets):
        candidates = defaultdict(set)
        for group in markets.values():
            for (a, venue, symbol), (b, peer, other) in combinations(self.futures_candidates(group), 2):
                if venue == peer or not compatible_assets(a, b) or min(a['volume'] or 0, b['volume'] or 0) < 1:
                    continue
                tentative = not (a['asset'] and a['asset'] == b['asset'])
                candidates[venue].add((tentative, -(a['volume'] or 0), symbol))
                candidates[peer].add((tentative, -(b['volume'] or 0), other))
        for i, venue in enumerate(PILOT_VENUES):
            for symbols in self.futures_contracts.values():
                if not any(symbol == symbols[i] for _, _, symbol in candidates[venue]):
                    candidates[venue].add((False, float('-inf'), symbols[i]))
        selected = {}
        for venue, items in candidates.items():
            ranked = sorted(items)
            ranked = self.stabilize_subscription_rank(
                ranked, self.futures_symbols.get(venue, ()), FUTURES_WS_LIMIT,
                key=lambda item: item[2], volume_index=1,
            )
            selected[venue] = frozenset(item[2] for item in ranked)
        return selected

    def update_futures_pairs(self, markets, now):
        pairs = []
        for coin, group in markets.items():
            quoted = []
            for leg, venue, symbol in self.futures_candidates(group):
                quote = self.futures_feeds[venue].quote(symbol, now)
                if quote is not None:
                    quoted.append((leg, quote))
            for (a, first), (b, second) in combinations(quoted, 2):
                if a['exchange'] == b['exchange'] or not compatible_assets(a, b):
                    continue
                expires_at = min(first['validUntil'], second['validUntil'])
                if expires_at > now:
                    changed_a, changed_b = first.get('changedAt'), second.get('changedAt')
                    suspect = (changed_a is not None and changed_b is not None
                               and abs(changed_a - changed_b) > 1500)
                    pair_legs = [dict(a, **first), dict(b, **second)]
                    pairs.append(dict(coin=coin, expiresAt=expires_at, timestampSuspect=suspect,
                                      legs=pair_legs))
        if pairs != self.futures_pairs:
            self.futures_pairs = pairs
            self.notify_quotes_changed()

    def set_early_pair(self, pair):
        self.early_pair = pair
        self.notify_quotes_changed()

    def notify_quotes_changed(self):
        self.early_pair_changed.set()
        self.early_pair_changed = asyncio.Event()

    def remember_snapshot(self, snapshot, body):
        if not self.compact_snapshot or self.compact_snapshot[0] is not snapshot:
            self.snapshot_revision += 1
        etag = f'W/"{self.snapshot_run}-{self.snapshot_revision}-full"'
        self.encoded_snapshot = snapshot, body, etag

    async def publish_snapshot(self, snapshot):
        body, version, metadata, metadata_body = await asyncio.to_thread(
            encode_compact, snapshot, self.snapshot_metadata, self.metadata_version,
            f'{self.snapshot_run}-{self.snapshot_revision + 1}',
        )
        self.snapshot_revision += 1
        etag = f'W/"{self.snapshot_run}-{self.snapshot_revision}-compact"'
        self.compact_snapshot = snapshot, body, etag
        self.encoded_snapshot = None
        self.snapshot_metadata, self.metadata_version = metadata, version
        if metadata_body is not None:
            self.metadata_packets[version] = metadata_body
            if len(self.metadata_packets) > 2:
                self.metadata_packets.pop(next(iter(self.metadata_packets)))
        self.snapshot = snapshot
        self.notify_quotes_changed()

    def market_descriptors(self, venue, kind, source):
        catalog = source.catalogs.get(venue)
        if catalog is None:
            self.catalog_descriptors.pop((venue, kind), None)
            return ()
        currency_source = self.sources.get((venue, 'spot'))
        currencies = currency_source.currencies if currency_source else None
        cached = self.catalog_descriptors.get((venue, kind))
        if cached and cached[0] is catalog and cached[1] is currencies:
            return cached[2]
        descriptors = []
        for market in catalog.values():
            binding = self.registry.match(venue, kind, market)
            group = binding.get('group') if binding else None
            if not valid_market(market, kind, venue=venue, group=group):
                continue
            base, quote = market['base'], market['quote']
            group = group or base
            key = f'{venue}.{kind}.{group}.{quote}'
            currency = (currencies or {}).get(base, {})
            networks = [dict(name=code, deposit=item.get('deposit'), withdraw=item.get('withdraw'))
                        for code, item in (currency.get('networks') or {}).items()
                        if item.get('deposit') is not None or item.get('withdraw') is not None]
            descriptors.append((market, binding, group, key, networks))
        if isinstance(source, Source):
            self.catalog_descriptors[venue, kind] = catalog, currencies, descriptors
        return descriptors

    def snapshot_markets(self, now, *, source_keys=None):
        started = monotonic()
        try:
            return self._build_snapshot_markets(now, source_keys=source_keys)
        finally:
            duration = round((monotonic() - started) * 1000)
            self.snapshot_build_ms = max(self.snapshot_build_ms or 0, duration)

    async def snapshot_markets_async(self, now, *, source_keys=None):
        worker = asyncio.create_task(asyncio.to_thread(self.snapshot_markets, now, source_keys=source_keys))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(worker)
            except Exception:
                LOG.exception('Snapshot worker failed while cancellation was pending')
            raise

    def _build_snapshot_markets(self, now, *, source_keys=None):
        groups, instruments = defaultdict(list), {}
        for venue, kind in self.sources if source_keys is None else source_keys:
            source = self.sources[venue, kind]
            batch = source.batches.get(venue, {})
            batch_received = batch.get('received_at_ms')
            tickers = batch.get('tickers', {})
            clock = self.clocks.get(venue)
            offset = clock.offset() if clock is not None else None
            for market, binding, group, key, networks in self.market_descriptors(venue, kind, source):
                symbol = market['symbol']
                base, quote = market['base'], market['quote']
                ticker = tickers.get(symbol, {})
                received = ticker.get('quote_received_at_ms', batch_received)
                raw_stamp = numeric(ticker.get('timestamp'))
                stamp = raw_stamp - offset if raw_stamp is not None and offset is not None else raw_stamp
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
                detail = self.cached_detail(key, now, (source, market)) or {}
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
                    size=market.get('contractSize') if kind == 'swap' else 1,
                    sourceAt=stamp, receivedAt=received if current else None,
                    index=numeric(raw.get('indexPrice') or raw_info.get('indexPrice')
                                  or raw_info.get('index_price') or ticker_info.get('indexPrice')),
                    oi=numeric(ticker.get('openInterestValue') or ticker_info.get('openInterestValue')),
                    volume=volume / 1_000_000 if volume is not None and volume >= 0 else None,
                    totals=detail.get('totals', {}) if kind == 'swap' else {'1': 0, '3': 0, '7': 0},
                    networks=networks, **fields,
                )
                if group != base:
                    leg['nativeBase'] = base
                if current and raw_stamp != stamp:
                    leg['sourceAtRaw'] = raw_stamp
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
            book = normalized_book(raw, market['contractSize'], now_ms(), clock=self.clocks.get('binanceusdm'))
            if book['sourceAt'] is None:
                raise ValueError('order book has no exchange timestamp')
            quote = dict(bid=book['bids'][0][0], ask=book['asks'][0][0], sourceAt=book['sourceAt'],
                         receivedAt=book['receivedAt'], quoteSource='depth')
            if 'sourceAtRaw' in book:
                quote['sourceAtRaw'] = book['sourceAtRaw']
            return quote
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
        markets = await self.snapshot_markets_async(ready, source_keys=EARLY_BINANCE_SOURCES)
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
        """Repair skew for the verified KuCoin/MEXC PYBOBO contracts as one update."""
        by_id = {leg['id']: leg for leg in markets.get('PYBOBO', ())}
        if any(key not in by_id or key not in self.instruments for key in PYBOBO_DEPTH_KEYS):
            return None
        legs = [by_id[key] for key in PYBOBO_DEPTH_KEYS]
        if any(leg['asset'] != 'capybobo' or not (leg['bid'] and leg['ask'])
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
                return normalized_book(raw, market['contractSize'], now_ms(),
                                       clock=self.clocks.get(getattr(source, 'venue', None)))
            except (aiohttp.ClientError, ccxt.BaseError, TimeoutError, ValueError, KeyError, TypeError) as exc:
                LOG.debug('PYBOBO depth refresh %s: %s', key, str(exc)[:160] or type(exc).__name__)
                return None

        books = await asyncio.gather(*(read(key) for key in PYBOBO_DEPTH_KEYS))
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

    async def run(self):
        last_slot = None
        retry_delay = 1
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session, asyncio.TaskGroup() as tasks:
            tasks.create_task(self.asset_metrics.run(session))
            for source in self.sources.values():
                source.open(session)
            self.schedule_event_loop_lag_probe()
            while True:
                started = monotonic()
                self.cycle_started = started
                self.snapshot_build_ms = None
                previous_snapshot, previous_instruments = self.snapshot, self.instruments
                try:
                    last_slot = await self._collect_cycle(session, started, last_slot)
                except Exception:
                    if self.snapshot is previous_snapshot:
                        self.instruments = previous_instruments
                    self.collection_failed = True
                    delay = retry_delay
                    retry_delay = min(INTERVAL, retry_delay * 2)
                    LOG.exception('Market collection cycle failed, retrying in %s s', delay)
                else:
                    self.collection_failed = False
                    retry_delay = 1
                    delay = max(1, INTERVAL - (monotonic() - started))
                finally:
                    self.cycle_started = None
                await asyncio.sleep(delay)

    async def _collect_cycle(self, session, started, last_slot):
        source_durations = {}
        tickers_ready = ({key: asyncio.Event() for key in EARLY_BINANCE_SOURCES}
                         if self.snapshot['generatedAt'] and all(key in self.sources for key in EARLY_BINANCE_SOURCES)
                         else {})

        async def collect_source(key, source):
            source_started = monotonic()
            try:
                await source.collect(tickers_ready.get(key))
            finally:
                source_durations[key] = round((monotonic() - source_started) * 1000)

        *outcomes, quotes = await asyncio.gather(
            *(collect_source(key, source) for key, source in self.sources.items()),
            self.binance_cycle_depth(tickers_ready), return_exceptions=True)
        statuses = {}
        for ((venue, kind), source), outcome in zip(self.sources.items(), outcomes):
            status = source.status()
            status['batchReceivedAt'] = source.batches.get(venue, {}).get('received_at_ms')
            if isinstance(outcome, Exception):
                LOG.warning('%s %s: %s', venue, kind, str(outcome)[:250])
                status = {**status, 'state': 'error', 'error': str(outcome)[:250]}
            statuses[f'{VENUES[venue]} · {kind}'] = status
        now = now_ms()
        markets = await self.snapshot_markets_async(now)
        pybobo_depth = await self.recheck_pybobo(markets)
        now = now_ms()
        if isinstance(quotes, Exception):
            LOG.warning('Binance depth refresh: %s', str(quotes)[:200])
        elif quotes:
            applied = self.apply_binance_depth(markets, quotes, now)
            statuses['Binance · swap'] = {**statuses['Binance · swap'], 'depth_refreshed': applied}
        if self.futures_feeds:
            ready = {venue for venue in self.futures_feeds
                     if statuses[f'{VENUES[venue]} · swap'].get('state') == 'ok'}
            await self.refresh_futures_feeds(session, markets, now, refresh=bool(ready), ready_venues=ready)
            await self.check_quiet_futures_symbols(now_ms())
            now = now_ms()
        next_snapshot = dict(
            state='ready' if all(s.get('state') == 'ok' for s in statuses.values()) else 'partial',
            venues=statuses, markets=markets, valuations=self.asset_metrics.snapshot(now), generatedAt=now,
        )
        if pybobo_depth is not None:
            next_snapshot['pyboboDepth'] = pybobo_depth
        await self.publish_snapshot(next_snapshot)
        self.source_duration_ms = source_durations
        self.cycle_duration_ms = round((monotonic() - started) * 1000)
        if self.futures_feeds:
            self.update_futures_pairs(markets, now)
        if self.early_pair is not None:
            self.set_early_pair(None)
        self.details = {key: item for key, item in self.details.items()
                        if item[1]['refreshAfter'] > now and self.instruments.get(key) == item[0]}
        if now // 60_000 != last_slot and markets:
            try:
                await self.history.write(eligible_legs(markets, now, compatible_assets), now)
                last_slot = now // 60_000
            except (sqlite3.Error, OSError):
                LOG.exception('Unable to store market history')
        return last_slot

    def health(self, now):
        def age(stamp):
            return max(0, now - stamp) if stamp is not None else None

        sources = {}
        rest = {}
        for (venue, kind), source in self.sources.items():
            label = f'{VENUES[venue]} · {kind}'
            batch = source.batches.get(venue, {})
            sources[label] = dict(
                state=self.snapshot['venues'].get(label, {}).get('state', 'starting'),
                durationMs=self.source_duration_ms.get((venue, kind)),
                batchAgeMs=age(batch.get('received_at_ms')),
            )
            budget = source.budget
            rest[venue] = dict(requests=budget.requests, rateLimits=budget.rate_limits)
        streams = {venue: feed for venue, feed in self.futures_feeds.items()}
        whitebit_spot = self.sources.get(('whitebit', 'spot'))
        if whitebit_spot and whitebit_spot.book_ticker:
            streams['whitebit_spot'] = whitebit_spot.book_ticker
        snapshot_age = age(self.snapshot['generatedAt'])
        state = 'error' if self.collection_failed else self.snapshot['state']
        if snapshot_age is not None and snapshot_age > 3 * INTERVAL * 1000:
            state = 'stalled'
        return dict(
            state=state, generatedAt=self.snapshot['generatedAt'],
            snapshotAgeMs=snapshot_age, cycleDurationMs=self.cycle_duration_ms,
            snapshotBuildMs=self.snapshot_build_ms,
            eventLoopLagMs=self.event_loop_lag_ms, eventLoopLagMaxMs=self.event_loop_lag_max_ms,
            activeCycleMs=(round((monotonic() - self.cycle_started) * 1000)
                           if self.cycle_started is not None else None),
            sources=sources, rest=rest,
            clocks={venue: clock.status() for venue, clock in self.clocks.items()},
            streams={name: dict(connected=getattr(feed, 'connected', False),
                                reconnects=getattr(feed, 'reconnects', 0)) for name, feed in streams.items()},
        )

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

    def current_instrument(self, key, expected):
        if self.instruments.get(key) != expected:
            return False
        source, market = expected
        return not isinstance(source, Source) or source.has_market(market)

    def cached_detail(self, key, now, instrument=None):
        cached = self.details.get(key)
        instrument = self.instruments.get(key) if instrument is None else instrument
        if cached and cached[0] == instrument and now < cached[1]['refreshAfter']:
            return cached[1]
        return None

    async def detail(self, key):
        instrument = self.instruments.get(key)
        if instrument is None or not self.current_instrument(key, instrument):
            raise web.HTTPServiceUnavailable(text='Каталог инструмента обновляется')
        cached = self.cached_detail(key, now_ms())
        if cached is not None:
            return cached
        task = self.pending.get(key)
        if task is None:
            if len(self.pending) >= 8:
                raise web.HTTPTooManyRequests(text='Лимит запросов карточек')
            task = self.pending[key] = asyncio.create_task(self.cache_detail(key, instrument))
        result = await asyncio.shield(task)
        if not self.current_instrument(key, instrument):
            raise web.HTTPServiceUnavailable(text='Каталог инструмента обновляется')
        return result

    async def cache_detail(self, key, instrument):
        try:
            source, market = instrument
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
            if not self.current_instrument(key, instrument):
                raise web.HTTPServiceUnavailable(text='Каталог инструмента обновляется')
            self.details[key] = (instrument, result)
            return result
        finally:
            self.pending.pop(key, None)

    async def load_index(self, source, market):
        try:
            async with asyncio.timeout(6):
                catalog_at = source.catalog_status.get(source.venue, {}).get('lastSuccessAt')
                return {'indexComposition': await contract_index(source.venue, source.client, market, catalog_at)}
        except (aiohttp.ClientError, ccxt.BaseError, TimeoutError, ValueError, AttributeError, KeyError, TypeError) as exc:
            LOG.warning('Index %s %s: %s', source.venue, market['id'], str(exc)[:180] or type(exc).__name__)
            return {'indexComposition': None, 'indexUnavailable': True}

    async def close(self):
        self.closing = True
        self.cancel_event_loop_lag_probe()
        if self.futures_update is not None:
            self.futures_update.cancel()
            self.futures_update = None
        try:
            await asyncio.gather(*(feed.close() for feed in self.futures_feeds.values()))
            for task in self.pending.values():
                task.cancel()
            await asyncio.gather(*self.pending.values(), return_exceptions=True)
            await asyncio.gather(*(source.close() for source in self.sources.values()))
        finally:
            await self.history.aclose()


def create_app(monitor, *, collect=True, open_url=None):
    return create_http_app(monitor, frontend=FRONTEND, now_ms=lambda: now_ms(), interval=INTERVAL,
                           collect=collect, open_url=open_url)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8780)
    parser.add_argument('--venues', nargs='+', choices=VENUES, default=tuple(VENUES))
    parser.add_argument('--history', type=Path, default=ROOT / 'data' / 'monitor.sqlite3')
    parser.add_argument('--futures-ws', action='store_true', help='Public futures best bid/ask WebSocket streams')
    parser.add_argument('--open', action='store_true', help='Open the screener in the default browser')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    url = f'http://127.0.0.1:{args.port}/' if args.open else None
    web.run_app(create_app(Monitor(dict.fromkeys(args.venues), args.history, futures_ws=args.futures_ws), open_url=url),
                host='127.0.0.1', port=args.port, handler_cancellation=True)


if __name__ == '__main__':
    main()
