"""Public exchange catalogs, quotes, order books and funding adapters."""

import asyncio
import logging

import aiohttp
import ccxt.async_support as ccxt

from time import monotonic

from .exchange_clock import ExchangeClock
from .funding_history import DAY
from .market import MAX_AGE, decimal, fresh, is_target, now_ms, numeric
from .rest_budget import guard
from .usdc import EXTENDED_INFO, EdgeXPublic, ExtendedPublic
from .whitebit import WhitebitBookTicker


VENUES = {
    'binanceusdm': 'Binance', 'bybit': 'Bybit', 'okx': 'OKX',
    'bitget': 'Bitget', 'gate': 'Gate', 'mexc': 'MEXC',
    'kucoinfutures': 'KuCoin', 'aster': 'Aster', 'htx': 'HTX',
    'whitebit': 'WhiteBIT', 'edgex': 'EdgeX', 'extended': 'Extended',
}
SPOT_CLIENTS = {
    'binanceusdm': 'binance', 'bybit': 'bybit', 'okx': 'okx',
    'bitget': 'bitget', 'gate': 'gate', 'mexc': 'mexc',
    'kucoinfutures': 'kucoin', 'htx': 'htx', 'whitebit': 'whitebit',
}

EDGEX_PUBLIC = 'https://edgex-prod-v2.edgex.exchange/api/v2/public'
HTX_LINEAR = 'https://api.hbdm.com/linear-swap-api/v1'
MEXC_FUNDING = 'https://api.mexc.com/api/v1/contract/funding_rate'
KUCOIN_UTA_TICKER = 'https://api.kucoin.com/api/ua/v1/market/ticker?tradeType=FUTURES'
CYCLE_FUNDING_VENUES = frozenset(('okx', 'mexc', 'htx'))
TURNOVER_VENUES = frozenset(('okx', 'kucoinfutures', 'aster'))
CATALOG_REFRESH_MS = 30 * 60_000
CATALOG_MAX_AGE_MS = 2 * 60 * 60_000
OPEN_INTEREST_REFRESH_MS = 60_000
OPEN_INTEREST_MAX_AGE_MS = 120_000
TURNOVER_REFRESH_MS = 180_000
TURNOVER_MAX_AGE_MS = 600_000
OKX_BAR_MS = 900_000
OKX_WINDOW_GRACE_MS = 300_000
LOG = logging.getLogger(__name__)


def okx_turnover(candles, end):
    """Sum confirmed quote-currency volumes for the 24 hours ending at end."""
    start = end - DAY
    bars = {}
    for row in candles:
        if not isinstance(row, list) or len(row) < 9 or row[8] != '1':
            continue
        stamp, volume = numeric(row[0]), numeric(row[7])
        if stamp is None or not start <= stamp < end or stamp % OKX_BAR_MS:
            continue
        if volume is None or volume < 0 or (stamp in bars and bars[stamp] != volume):
            return None
        bars[stamp] = volume
    return numeric(sum(bars.values())) if len(bars) == DAY // OKX_BAR_MS else None


def okx_open_interest(response, now, clock):
    if response.get('code') != '0' or not isinstance(response.get('data'), list):
        raise ValueError('invalid open interest response')
    quotes = {}
    for row in response['data']:
        if not isinstance(row, dict) or row.get('instType') != 'SWAP':
            continue
        native = row.get('instId')
        value = numeric(row.get('oiUsd'))
        stamp = clock.correct(numeric(row.get('ts')), now)
        if not isinstance(native, str) or value is None or value < 0 or not fresh(stamp, now, OPEN_INTEREST_MAX_AGE_MS):
            continue
        if native not in quotes or stamp > quotes[native][0]:
            quotes[native] = (stamp, value)
    if not quotes:
        raise ValueError('no current open interest data')
    return quotes


def public_client_config(exchange):
    options = {"defaultType": "swap", "defaultSubType": "linear"}
    if exchange == "bitget":
        # This monitor needs swaps only. Loading unrelated UTA spot instruments
        # can fail when Bitget returns an instrument without its base asset.
        options["fetchMarkets"] = {"types": ["swap"]}
    config = {"enableRateLimit": True, "timeout": 15000, "options": options}
    if exchange == 'gate':
        options['fetchMarkets'] = {'types': ['swap']}
        options['swap'] = {'fetchMarkets': {'settlementCurrencies': ['usdt']}}
        # Linear USDT contract metadata supplies everything this monitor uses.
        # Currency/deposit networks and unrelated market catalogs are not needed.
        config['has'] = {'fetchCurrencies': False}
    return config


class Source:
    """One public market type on a venue, with independently refreshed data."""

    def __init__(self, venue, kind, budget, clock=None):
        self.venue, self.kind = venue, kind
        self.budget = budget
        self.clock = clock if clock is not None else ExchangeClock()
        self.clock_task = None
        self.clients, self.catalogs, self.catalog_status = {}, {}, {}
        self.batches, self.ticker_status = {}, {}
        self.updated = self.funding_at = self.currencies_at = 0
        self.funding, self.currencies, self.volume = {}, {}, {}
        self.catalog_client = None
        self.catalog_task = None
        self.catalog_at = None
        self.collect_ms = None
        self.turnover_error = None
        self.turnover_due = 0
        self.turnover_task = None
        self.open_interest = {}
        self.open_interest_due = 0
        self.open_interest_task = None
        self.turnover_client = None
        self.schedule = None
        self.book_ticker = None

    def open(self, session):
        venue, kind = self.venue, self.kind
        if venue == 'edgex':
            self.clients[venue] = EdgeXPublic(session)
        elif venue == 'extended':
            self.clients[venue] = ExtendedPublic(session)
        else:
            self.clients[venue] = self.ccxt_client()
            self.catalog_client = self.ccxt_client(background=True)
            if kind == 'spot' and venue == 'whitebit':
                self.book_ticker = WhitebitBookTicker(session)
            if kind == 'swap' and venue == 'okx':
                self.turnover_client = self.ccxt_client(background=True)

    def ccxt_client(self, *, background=False):
        venue = self.venue
        if self.kind == 'spot':
            types = {'spot': True} if venue == 'htx' else ['spot']
            options = {'defaultType': 'spot', 'fetchMarkets': {'types': types}}
            client = getattr(ccxt, SPOT_CLIENTS[venue])({
                'enableRateLimit': True, 'timeout': 12_000, 'options': options,
            })
        else:
            client = getattr(ccxt, venue)(public_client_config(venue))
        return guard(client, self.budget, background=background)

    @property
    def client(self):
        return self.clients[self.venue]

    def start_catalog(self, now):
        venue = self.venue
        if self.catalog_at is not None and now - self.catalog_at >= CATALOG_MAX_AGE_MS:
            if self.catalogs.pop(venue, None) is not None:
                self.catalog_status[venue].update(state='error', contracts=0)
        state = self.catalog_status.get(venue, {}).get('state')
        interval = CATALOG_REFRESH_MS if venue in self.catalogs and state != 'stale' else MAX_AGE
        if self.catalog_task is not None and not self.catalog_task.done():
            return
        if self.updated and now - self.updated < interval:
            return
        self.catalog_task = asyncio.create_task(self.catalog(venue))

    async def catalog(self, venue):
        loader = self.catalog_client if self.catalog_client is not None else self.client
        try:
            async with asyncio.timeout(60):
                markets = await loader.load_markets(reload=True)
            if not isinstance(markets, dict) or not markets:
                raise ValueError('empty market catalog')
            contracts = sum(is_target(market) for market in markets.values())
            if loader is not self.client:
                self.client.set_markets_from_exchange(loader)
                # Refresh uses a separate client so CCXT ticker calls never wait
                # on its shared load_markets future.
                self.client.markets_loading = None
            self.catalogs[venue] = markets
            self.catalog_at = now_ms()
            self.catalog_status[venue] = dict(
                state='ok', checked_at_ms=self.catalog_at, last_success_at_ms=self.catalog_at,
                contracts=contracts, error=None,
            )
        except Exception as exc:
            status = self.catalog_status.get(venue, {})
            self.catalog_status[venue] = dict(
                state='stale' if venue in self.catalogs else 'error', checked_at_ms=now_ms(),
                last_success_at_ms=self.catalog_at, contracts=status.get('contracts', 0),
                error=f'{type(exc).__name__}: {exc}'[:500],
            )
        finally:
            self.updated = now_ms()

    def start_clock(self, now):
        client = self.catalog_client
        if (self.kind == 'swap' and client is not None and client.has.get('fetchTime') is True
                and now >= self.clock.due and (self.clock_task is None or self.clock_task.done())):
            self.clock_task = asyncio.create_task(self.measure_clock(client))

    async def measure_clock(self, client):
        try:
            await self.clock.measure(client.fetch_time)
        except Exception as exc:
            LOG.warning('%s public clock: %s', self.venue, type(exc).__name__)

    async def tickers(self, venue):
        try:
            async with asyncio.timeout(10):
                if venue in ('binanceusdm', 'aster'):
                    values = await self.client.fetch_bids_asks()
                elif venue == 'kucoinfutures':
                    try:
                        async with asyncio.timeout(4):
                            values = await self.kucoin_tickers()
                        feed = 'uta'
                    except ccxt.RateLimitExceeded:
                        raise
                    except (aiohttp.ClientError, ccxt.BaseError, TimeoutError, ValueError) as exc:
                        LOG.warning('KuCoin Futures UTA tickers: %s %s', type(exc).__name__, str(exc)[:180])
                        values = await self.client.fetch_tickers(params={'method': 'futuresPublicGetAllTickers'})
                        feed = 'classic'
                else:
                    values = await self.client.fetch_tickers()
                if venue == 'aster':
                    # CCXT leaves the unified timestamp empty for this bookTicker response.
                    values = {symbol: dict(ticker, timestamp=numeric(ticker.get('timestamp'))
                                           or numeric((ticker.get('info') or {}).get('time')))
                              for symbol, ticker in values.items()}
            if not isinstance(values, dict) or not values:
                raise ValueError('invalid_bulk_tickers')
            received = now_ms()
            self.batches[venue] = dict(tickers=values, received_at_ms=received)
            self.ticker_status[venue] = dict(
                state='ok', received_at_ms=received, returned=len(values),
                with_bid_ask=sum(numeric(row.get('bid')) is not None and numeric(row.get('ask')) is not None
                                 for row in values.values()),
                error=None,
            )
            if venue == 'kucoinfutures':
                self.ticker_status[venue]['feed'] = feed
        except Exception as exc:
            self.batches.pop(venue, None)
            self.ticker_status[venue] = dict(
                state='error', received_at_ms=now_ms(), returned=0, error=f'{type(exc).__name__}: {exc}'[:500],
            )

    async def kucoin_tickers(self):
        response = await self.client.fetch(KUCOIN_UTA_TICKER)
        data = response.get('data') if isinstance(response, dict) else None
        if not isinstance(data, dict) or response.get('code') != '200000' or data.get('tradeType') != 'FUTURES':
            raise ValueError('invalid futures ticker response')
        rows, stamp = data.get('list'), data.get('ts')
        stamp = stamp // 1_000_000 if type(stamp) is int else None
        now = now_ms()
        if not isinstance(rows, list) or not fresh(self.clock.correct(stamp, now), now, 10_000):
            raise ValueError('stale or incomplete futures ticker snapshot')
        indexed = {row['symbol']: row for row in rows if isinstance(row, dict) and isinstance(row.get('symbol'), str)}
        values = {}
        for symbol, market in self.catalogs['kucoinfutures'].items():
            row = indexed.get(market['id'])
            if row is None:
                continue
            values[symbol] = dict(symbol=symbol, native_id=market['id'], timestamp=stamp,
                                  bid=row.get('bestBidPrice'), ask=row.get('bestAskPrice'),
                                  last=row.get('lastPrice'), quoteVolume=row.get('quoteVolume'), info=row)
        if not values:
            raise ValueError('futures tickers did not match the market catalog')
        return values

    async def collect(self, tickers_ready=None):
        started = monotonic()
        try:
            await self.collect_cycle(tickers_ready)
        finally:
            self.collect_ms = round((monotonic() - started) * 1000, 1)
            if tickers_ready is not None:
                tickers_ready.set()

    async def collect_cycle(self, tickers_ready):
        venue = self.venue
        now = now_ms()
        self.start_catalog(now)
        if venue not in self.catalogs:
            return
        self.start_clock(now)
        if self.kind == 'swap':
            if venue == 'okx' and now >= self.open_interest_due and self.turnover_client is not None:
                if self.open_interest_task is None or self.open_interest_task.done():
                    self.open_interest_due = now + OPEN_INTEREST_REFRESH_MS
                    self.open_interest_task = asyncio.create_task(self.refresh_open_interest())
            await asyncio.gather(self.collect_swap_tickers(tickers_ready), self.collect_aux(now))
        else:
            try:
                if self.book_ticker is not None:
                    self.book_ticker.start()
                async with asyncio.timeout(12):
                    tickers = await self.client.fetch_tickers(params={'type': 'spot'})
                if not tickers:
                    raise ValueError('empty_spot_tickers')
                received = now_ms()
                status = dict(state='ok')
                if self.book_ticker is not None:
                    quotes = self.book_ticker.snapshot(self.catalogs[venue], received)
                    # Only the timestamped stream supplies executable prices.
                    for ticker in tickers.values():
                        ticker.update(bid=None, ask=None)
                    for symbol, quote in quotes.items():
                        tickers.setdefault(symbol, {}).update(quote)
                    status['with_bid_ask'] = len(quotes)
                    if not quotes:
                        status.update(state='partial', error='WhiteBIT book ticker: no fresh spot quotes')
                self.batches[venue] = dict(tickers=tickers, received_at_ms=received)
                self.ticker_status[venue] = status
            except (ccxt.BaseError, TimeoutError, ValueError) as exc:
                self.batches.pop(venue, None)
                self.ticker_status[venue] = dict(state='error', error=str(exc)[:300])
        if (self.kind == 'spot' and now - self.currencies_at >= 900_000
                and self.client.has.get('fetchCurrencies') is True
                and self.client.currencies):
            self.currencies_at = now_ms()
            self.currencies = self.client.currencies

    async def collect_swap_tickers(self, tickers_ready):
        venue = self.venue
        if venue == 'bitget':
            _, extra = await asyncio.gather(self.tickers(venue), self.bitget_usdc_tickers())
            if venue in self.batches:
                if extra is None:
                    self.ticker_status[venue].update(state='partial', error='USDC: тикеры недоступны')
                else:
                    self.batches[venue]['tickers'].update(extra)
        else:
            await self.tickers(venue)
        if tickers_ready is not None:
            tickers_ready.set()
        if venue in TURNOVER_VENUES:
            self.start_turnover(now_ms())

    async def bitget_usdc_tickers(self):
        try:
            async with asyncio.timeout(8):
                return await self.client.fetch_tickers(params={'productType': 'USDC-FUTURES'})
        except (aiohttp.ClientError, ccxt.BaseError, TimeoutError) as exc:
            LOG.warning('Bitget USDC tickers: %s', type(exc).__name__)
            return None

    async def collect_aux(self, now):
        venue = self.venue
        if venue not in CYCLE_FUNDING_VENUES and now - self.funding_at < MAX_AGE:
            return
        self.funding_at = now_ms()
        requests = []
        funding = {
            'htx': self.htx_funding, 'okx': self.okx_funding, 'mexc': self.mexc_funding,
            'edgex': self.edgex_funding, 'bitget': self.funding_by_product,
            'kucoinfutures': self.funding_by_product,
        }.get(venue)
        if funding is not None:
            requests.append(self.refresh_aux('funding', funding))
        elif venue not in ('bybit', 'extended'):
            requests.append(self.refresh_aux('funding', self.client.fetch_funding_rates))
        if venue == 'htx':
            requests.append(self.refresh_aux('schedule', self.htx_schedule))
        if venue == 'binanceusdm':
            requests.append(self.refresh_aux('volume', self.client.fetch_tickers))
        if venue in ('binanceusdm', 'aster') and self.client.has.get('fetchFundingIntervals') is True:
            requests.append(self.refresh_aux('schedule', self.client.fetch_funding_intervals))
        await asyncio.gather(*requests)

    async def close(self):
        tasks = [task for task in (self.catalog_task, self.clock_task, self.turnover_task, self.open_interest_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.catalog_client is not None:
            await self.catalog_client.close()
        if self.turnover_client is not None:
            await self.turnover_client.close()
        if self.book_ticker is not None:
            await self.book_ticker.close()
        if self.venue in self.clients:
            await self.client.close()

    async def refresh_open_interest(self):
        try:
            async with asyncio.timeout(10):
                response = await self.turnover_client.publicGetPublicOpenInterest({'instType': 'SWAP'})
            self.open_interest = okx_open_interest(response, now_ms(), self.clock)
        except (aiohttp.ClientError, ccxt.BaseError, TimeoutError, ValueError, TypeError, AttributeError) as exc:
            LOG.warning('OKX open interest: %s', type(exc).__name__)

    def start_turnover(self, now):
        if self.turnover_task is not None:
            if not self.turnover_task.done():
                return
            self.turnover_task = None
        if now < self.turnover_due:
            return
        if self.venue == 'okx':
            # Allow time for the exchange to mark the last candle as confirmed.
            end = (now - 10_000) // OKX_BAR_MS * OKX_BAR_MS
            self.turnover_due = end + OKX_BAR_MS + 10_000
            self.turnover_task = asyncio.create_task(self.run_turnover(self.refresh_okx_turnover(end)))
        else:
            self.turnover_due = now + TURNOVER_REFRESH_MS
            self.turnover_task = asyncio.create_task(self.run_turnover(self.refresh_bulk_turnover()))

    async def run_turnover(self, operation):
        self.turnover_error = None
        try:
            await operation
        except Exception as exc:
            LOG.exception('%s turnover update failed', self.venue)
            self.turnover_error = type(exc).__name__
            self.turnover_due = now_ms() + MAX_AGE

    async def refresh_bulk_turnover(self):
        try:
            async with asyncio.timeout(12):
                tickers = await self.client.fetch_tickers()
            if not tickers:
                raise ValueError('empty 24h tickers')
            expires = now_ms() + TURNOVER_MAX_AGE_MS
            updated = {}
            for symbol, market in self.catalogs.get(self.venue, {}).items():
                if not any(is_target(market, quote) for quote in ('USDT', 'USDC')):
                    continue
                ticker = tickers.get(symbol, {})
                value = numeric(ticker.get('quoteVolume'))
                native = (ticker.get('info') or {}).get('symbol', market['id'])
                if value is not None and value >= 0 and ticker.get('symbol') == symbol and native == market['id']:
                    updated[symbol] = dict(quoteVolume=value, native_id=market['id'], expiresAt=expires)
            if not updated:
                raise ValueError('24h tickers have no quote turnover')
            self.volume = updated
        except (aiohttp.ClientError, ccxt.BaseError, TimeoutError, ValueError) as exc:
            self.turnover_error = type(exc).__name__
            LOG.warning('%s turnover: %s %s', self.venue, type(exc).__name__, str(exc)[:200])
            self.turnover_due = now_ms() + MAX_AGE

    async def refresh_okx_turnover(self, end):
        tickers = self.batches.get('okx', {}).get('tickers', {})
        markets = [market for market in self.catalogs.get('okx', {}).values() if is_target(market)]
        # Prioritize active markets during warm-up. Only candle sums enter the filter.
        def priority(market):
            ticker = tickers.get(market['symbol'], {})
            base_volume = numeric((ticker.get('info') or {}).get('volCcy24h')) or 0
            return base_volume * (numeric(ticker.get('last')) or 0)

        markets.sort(key=priority, reverse=True)
        self.volume = {m['symbol']: self.volume[m['symbol']] for m in markets if m['symbol'] in self.volume}
        retry, failed = [], []
        expires = end + OKX_BAR_MS + OKX_WINDOW_GRACE_MS
        for batch in (markets, retry):
            for market in batch:
                if now_ms() >= expires:
                    break
                try:
                    async with asyncio.timeout(12):
                        response = await self.turnover_client.publicGetMarketCandles({
                            'instId': market['id'], 'bar': '15m', 'limit': '100',
                        })
                    rows = response.get('data')
                    if response.get('code') != '0' or not isinstance(rows, list):
                        raise ValueError('invalid candle response')
                    value = okx_turnover(rows, end)
                    if value is not None:
                        self.volume[market['symbol']] = dict(quoteVolume=value, native_id=market['id'], expiresAt=expires)
                except (aiohttp.ClientError, ccxt.NetworkError, TimeoutError) as exc:
                    if batch is markets:
                        retry.append(market)
                    else:
                        failed.append((market['id'], type(exc).__name__))
                except (ccxt.BaseError, ValueError) as exc:
                    failed.append((market['id'], type(exc).__name__))
        if failed:
            self.turnover_error = f'{len(failed)} candle requests failed'
            LOG.warning('okx turnover: %s/%s candle requests failed, first failure %s',
                        len(failed), len(markets), failed[0])

    async def refresh_aux(self, field, method):
        try:
            async with asyncio.timeout(12):
                setattr(self, field, await method() or {})
        except (aiohttp.ClientError, ccxt.BaseError, TimeoutError, ValueError) as exc:
            setattr(self, field, None if field == 'schedule' else {})
            LOG.warning('%s %s: %s %s', self.venue, field, type(exc).__name__, str(exc)[:200])

    async def htx_funding(self):
        response = await self.client.fetch(f'{HTX_LINEAR}/swap_batch_funding_rate')
        return self.catalog_funding(response, 'contract_code', response.get('status') == 'ok')

    async def htx_schedule(self):
        response = await self.client.fetch(f'{HTX_LINEAR}/swap_contract_info')
        return self.catalog_funding(response, 'contract_code', response.get('status') == 'ok')

    async def okx_funding(self):
        response = await self.client.publicGetPublicFundingRate({'instId': 'ANY'})
        return self.catalog_funding(response, 'instId', response.get('code') == '0', timestamp='ts')

    async def mexc_funding(self):
        # The public endpoint accepts an omitted symbol and returns a list.
        response = await self.client.fetch(MEXC_FUNDING)
        return self.catalog_funding(response, 'symbol', response.get('success') is True, timestamp='timestamp')

    def catalog_funding(self, response, id_field, success, *, timestamp=None):
        rows = response.get('data')
        if not success or not isinstance(rows, list) or not rows:
            raise ValueError('invalid_bulk_funding')
        now = now_ms()
        indexed = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get(id_field), str):
                raise ValueError('invalid_bulk_funding_row')
            if self.venue == 'okx' and row.get('instType') != 'SWAP':
                continue
            stamp = numeric(row.get(timestamp)) if timestamp else None
            if timestamp and not fresh(stamp, now, 120_000):
                continue
            native = row[id_field]
            if native in indexed and indexed[native]['info'] != row:
                raise ValueError('conflicting_bulk_funding')
            indexed[native] = dict(info=row, timestamp=stamp)
        return {market['symbol']: indexed[market['id']] for market in self.catalogs[self.venue].values()
                if market['id'] in indexed}

    async def funding_by_product(self):
        method = self.client.fetch_funding_intervals if self.venue == 'bitget' else self.client.fetch_funding_rates
        result = {}
        for quote in ('USDT', 'USDC'):
            try:
                result.update(await method(params={'productType': f'{quote}-FUTURES'}))
            except ccxt.BaseError as exc:
                LOG.warning('%s %s funding: %s', self.venue, quote, str(exc)[:200])
        return result

    async def public_data(self, url, **params):
        await self.budget.wait()
        async with self.client.session.get(url, params=params) as response:
            if response.status == 429:
                self.budget.limited()
            response.raise_for_status()
            document = await response.json()
        if document.get('code') != 'SUCCESS' and document.get('status') != 'OK':
            raise ValueError('Публичный источник вернул ошибку')
        return document['data']

    async def edgex_funding(self):
        markets = self.catalogs[self.venue].values()
        rows = await self.public_data(f'{EDGEX_PUBLIC}/funding/getLatestFundingRate',
                                      contractId=[market['id'] for market in markets])
        by_id = {row['contractId']: row for row in rows if fresh(numeric(row.get('fundingTimestamp')), now_ms(), 180000)}
        return {market['symbol']: {'info': by_id[market['id']], 'indexPrice': by_id[market['id']].get('indexPrice')}
                for market in markets if market['id'] in by_id}

    async def public_book(self, market):
        market_id = market['id']
        if self.venue == 'extended':
            book = await self.public_data(f'{EXTENDED_INFO}/markets/{market_id}/orderbook')
            if book.get('market') != market_id:
                raise ValueError('Стакан другого инструмента')
            sides = {side: [[row['price'], row['qty']] for row in book[side[:-1]]] for side in ('bids', 'asks')}
        elif self.venue == 'edgex':
            books = await self.public_data(f'{EDGEX_PUBLIC}/quote/getDepth', contractId=market_id, level=15)
            book = next((row for row in books if row.get('contractId') == market_id), None)
            if not book or book.get('depthType') != 'SNAPSHOT':
                raise ValueError('EdgeX не вернул полный снимок стакана')
            sides = {side: [[row['price'], row['size']] for row in book[side]] for side in ('bids', 'asks')}
        else:
            return await self.client.fetch_order_book(market['symbol'])
        return dict(sides, symbol=market['symbol'], timestamp=None)

    async def public_funding_history(self, market, now):
        market_id = market['id']
        start = now - 8 * DAY
        events = {}
        if self.venue == 'extended':
            rows = await self.public_data(f'{EXTENDED_INFO}/{market_id}/funding', startTime=start, endTime=now, limit=250)
            records = ((row['m'], row['T'], row['f']) for row in rows)
        else:
            records = []
            offset = ''
            for _ in range(4):
                page = await self.public_data(f'{EDGEX_PUBLIC}/funding/getFundingRatePage',
                                              contractId=market_id, size=100, offsetData=offset,
                                              filterSettlementFundingRate='true',
                                              filterBeginTimeInclusive=start, filterEndTimeExclusive=now + 1)
                records.extend((row['contractId'], row['fundingTime'], row['fundingRate'])
                               for row in page['dataList'] if row.get('isSettlement') is True)
                next_offset = page.get('nextPageOffsetData')
                if not next_offset or not page['dataList']:
                    break
                if offset == next_offset:
                    raise ValueError('Повтор страницы истории фандинга')
                offset = next_offset
        for identity, stamp, raw in records:
            if identity != market_id:
                raise ValueError('Ставка другого инструмента')
            stamp, rate = int(stamp), decimal(raw)
            if start <= stamp <= now:
                if stamp in events and events[stamp] != rate:
                    raise ValueError('Противоречивые ставки в истории фандинга')
                events[stamp] = rate
        return [dict(timestamp=stamp, rate=rate) for stamp, rate in sorted(events.items())]
