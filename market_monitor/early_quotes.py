"""Early synchronized quotes and configured REST depth rechecks."""

import asyncio
import logging

import aiohttp
import ccxt.async_support as ccxt

from .books import normalized_book
from .market import compatible_assets, fresh


BINANCE_DEPTH_LIMIT = 4
EARLY_BINANCE_SOURCES = (('binanceusdm', 'swap'), ('kucoinfutures', 'swap'))
EARLY_QUOTE_MAX_AGE = 3000
LOG = logging.getLogger(__name__)


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


class EarlyQuotes:
    def __init__(self, monitor, clock):
        self.monitor = monitor
        self.clock = clock
        self.binance_depth_cursor = self.binance_early_cursor = ''

    async def binance_depth_quote(self, key):
        source, market = self.monitor.instruments[key]
        try:
            async with asyncio.timeout(3):
                raw = await source.client.fetch_order_book(market['symbol'], limit=5)
            if not isinstance(raw, dict) or raw.get('symbol') != market['symbol']:
                raise ValueError('order book symbol does not match the market')
            book = normalized_book(raw, market['contractSize'], self.clock(), self.monitor.clocks.get(getattr(source, 'venue', None)))
            if book['sourceAt'] is None:
                raise ValueError('order book has no exchange timestamp')
            return dict(bid=book['bids'][0][0], ask=book['asks'][0][0], sourceAt=book['sourceAt'],
                        receivedAt=book['receivedAt'], quoteSource='depth')
        except (aiohttp.ClientError, ccxt.BaseError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            LOG.debug('Binance depth refresh %s: %s', key, str(exc)[:160] or type(exc).__name__)
            return None

    async def binance_depth_quotes(self, reserved=None):
        candidates = [leg['id'] for leg, _ in binance_depth_candidates(self.monitor.snapshot['markets'])
                      if leg['id'] in self.monitor.instruments and leg['id'] != reserved]
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

        ready = self.clock()
        markets = self.monitor.snapshot_markets(ready, source_keys=EARLY_BINANCE_SOURCES)
        candidates = {}
        for leg, peers in binance_depth_candidates(markets):
            key = leg['id']
            peer = peers[0]
            if (leg['quote'] != 'USDT' or not leg['asset'] or leg['asset'] != peer['asset']
                    or any(l['id'] not in self.monitor.instruments for l in (leg, peer))
                    or not fresh(peer['sourceAt'], ready, EARLY_QUOTE_MAX_AGE)):
                continue
            if any(source.catalogs.get(source.venue, {}).get(market['symbol'], {}).get('id') != market['id']
                   for source, market in (self.monitor.instruments[l['id']] for l in (leg, peer))):
                continue
            candidates[key] = (leg, peer)
        if not candidates:
            return await self.binance_depth_quotes()

        key = min(candidates, key=lambda k: (k <= self.binance_early_cursor, k))
        self.binance_early_cursor = key
        leg, peer = candidates[key]
        quote = await self.binance_depth_quote(key)
        quotes = {key: quote} if quote is not None else {}
        now = self.clock()
        if quote is not None:
            stamps = [q[field] for q in (quote, peer) for field in ('sourceAt', 'receivedAt')]
            if all(fresh(stamp, now, EARLY_QUOTE_MAX_AGE) for stamp in stamps):
                early_legs = [dict(leg), peer]
                if self.apply_binance_depth({'early': early_legs}, quotes, now):
                    self.monitor.set_early_pair(dict(
                        baseAt=self.monitor.snapshot['generatedAt'], coin=key.split('.')[2],
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

    async def recheck_configured_pair(self, markets):
        """Repair skew for the configured verified pair as one update."""
        rule = self.monitor.registry.depth_recheck
        if rule is None:
            return None
        keys = rule['markets']
        by_id = {leg['id']: leg for leg in markets.get(rule['group'], ())}
        if any(key not in by_id or key not in self.monitor.instruments for key in keys):
            return None
        legs = [by_id[key] for key in keys]
        if any(leg['asset'] != rule['asset'] or not (leg['bid'] and leg['ask'])
               or leg['sourceAt'] is None for leg in legs) or not compatible_assets(*legs):
            return None
        bulk_skew = abs(legs[0]['sourceAt'] - legs[1]['sourceAt'])
        if bulk_skew <= 1500:
            return dict(state='bulk_aligned', bulkSkewMs=bulk_skew)

        if self.monitor.futures_feeds:
            now = self.clock()
            streamed = []
            for key in keys:
                source, market = self.monitor.instruments[key]
                feed = self.monitor.futures_feeds.get(source.venue)
                streamed.append(feed.quote(market['id'], now) if feed is not None else None)
            if all(streamed):
                skew = abs(streamed[0]['sourceAt'] - streamed[1]['sourceAt'])
                if skew <= 1500:
                    return dict(state='ws_aligned', bulkSkewMs=bulk_skew, streamSkewMs=skew)

        async def read(key):
            source, market = self.monitor.instruments[key]
            try:
                async with asyncio.timeout(3):
                    raw = await source.public_book(market)
                if not isinstance(raw, dict) or raw.get('symbol') != market['symbol']:
                    raise ValueError('order book symbol does not match the market')
                return normalized_book(raw, market['contractSize'], self.clock(), self.monitor.clocks.get(getattr(source, 'venue', None)))
            except (aiohttp.ClientError, ccxt.BaseError, TimeoutError, ValueError, KeyError, TypeError) as exc:
                LOG.debug('Paired depth refresh %s: %s', key, str(exc)[:160] or type(exc).__name__)
                return None

        books = await asyncio.gather(*(read(key) for key in keys))
        now = self.clock()
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
