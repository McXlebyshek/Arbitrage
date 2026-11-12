"""Offline parser contracts using excerpts of recorded public responses."""

from contextlib import asynccontextmanager
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

import ccxt.async_support as ccxt

from market_monitor.current_funding import funding_fields
from market_monitor.exchange_clock import ExchangeClock
from market_monitor.funding_history import DAY, fetch_history, window_stats
from market_monitor.market_admission import valid_market
from market_monitor.sources import okx_open_interest
from market_monitor.usdc import EdgeXPublic, ExtendedPublic
from market_monitor.whitebit import WhitebitBookTicker


RESPONSES = json.loads((Path(__file__).parent / 'fixtures' / 'public_responses.json').read_text(
    encoding='utf-8'))['responses']
SYMBOL = 'BTC/USDT:USDT'


def body(key):
    return RESPONSES[key]['body']


class RecordedSession:
    @asynccontextmanager
    async def get(self, url):
        reply = next(item for item in RESPONSES.values() if item['url'] == url)
        yield RecordedResponse(reply['body'])


class RecordedResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    async def json(self):
        return self.payload


class RecordedAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_edgex_catalog_and_extended_crypto_admission(self):
        session = RecordedSession()
        edgex = EdgeXPublic(session)
        edge_market = next(iter((await edgex.load_markets()).values()))
        self.assertEqual(edge_market['symbol'], 'BTC/USDC:USDC')
        extended = ExtendedPublic(session)
        self.assertEqual(set(await extended.load_markets()), {'BTC-USD'})
        ticks = await extended.fetch_tickers()
        self.assertEqual(ticks['BTC/USDC:USDC']['native_id'], 'BTC-USD')
        self.assertEqual(ticks['BTC/USDC:USDC']['bid'], body('extended_markets')['data'][0]['marketStats']['bidPrice'])

    async def test_recorded_whitebit_book_ticker(self):
        update = body('whitebit_book_ticker')['params'][0]
        stamp = int(float(update[0]) * 1000)
        feed = WhitebitBookTicker(None)
        market = dict(id='BTC_USDT', symbol='BTC/USDT', spot=True, active=True, quote='USDT')
        feed.consume([update], stamp + 50)
        quotes = feed.snapshot({'BTC': market}, stamp + 100)
        self.assertEqual(quotes['BTC/USDT']['bid'], float(update[4]))
        self.assertEqual(quotes['BTC/USDT']['ask'], float(update[6]))
        self.assertEqual(feed.snapshot({'BTC': market}, stamp + 60_001), {})

    async def test_real_tradfi_prelaunch_and_batch_oi(self):
        prelaunch = body('bybit_prelaunch')['result']['list'][0]
        market = dict(base=prelaunch['baseCoin'], active=True, quote='USDT', settle='USDT',
                      swap=True, linear=True, contractSize=1, info=prelaunch)
        self.assertFalse(valid_market(market, 'swap', venue='bybit'))
        reply = body('okx_open_interest')
        row = reply['data'][0]
        now = int(row['ts'])
        self.assertEqual(okx_open_interest(reply, now, ExchangeClock())[row['instId']], (now, float(row['oiUsd'])))

    async def test_recorded_bybit_funding_ticker(self):
        reply = body('bybit_ticker')
        row = reply['result']['list'][0]
        funding = funding_fields({'info': row}, {'symbol': SYMBOL}, 'bybit', reply['time'])
        self.assertAlmostEqual(funding['rate'], float(row['fundingRate']) * 100)
        self.assertEqual(funding['next'], int(row['nextFundingTime']))

    async def test_replay_funding_history_with_real_ccxt_parsers(self):
        setups = (
            ('binanceusdm', ccxt.binanceusdm, 'BTCUSDT', 'fapiPublicGetFundingRate',
             body('binance_funding_history'), 'fundingTime', 'fundingRate'),
            ('bybit', ccxt.bybit, 'BTCUSDT', 'publicGetV5MarketFundingHistory',
             body('bybit_funding_history'), 'fundingRateTimestamp', 'fundingRate'),
            ('okx', ccxt.okx, 'BTC-USDT-SWAP', 'publicGetPublicFundingRateHistory',
             body('okx_funding_history'), 'fundingTime', 'fundingRate'),
        )
        for venue, factory, native, method, reply, stamp_key, rate_key in setups:
            with self.subTest(venue=venue):
                client = factory()
                try:
                    client.set_markets([dict(id=native, symbol=SYMBOL, base='BTC', quote='USDT',
                                             settle='USDT', type='swap', spot=False, swap=True, linear=True,
                                             inverse=False, contract=True, active=True, contractSize=1)])
                    transport = AsyncMock(return_value=reply)
                    setattr(client, method, transport)
                    raw = reply if isinstance(reply, list) else reply.get('data') or reply['result']['list']
                    end = max(int(row[stamp_key]) for row in raw)
                    result = await fetch_history(client, venue, SYMBOL, end - 2 * DAY, end)
                    self.assertIsNone(result['error'])
                    transport.assert_awaited_once()
                    self.assertEqual([event['timestamp'] for event in result['events']],
                                     sorted(int(row[stamp_key]) for row in raw))
                    self.assertEqual(window_stats(result['events'], end, 1)['coverage'], 'regular_observed_coverage')
                    expected = {int(row[stamp_key]): float(row[rate_key]) for row in raw}
                    self.assertEqual({item['timestamp']: float(item['rate']) for item in result['events']}, expected)
                finally:
                    await client.close()
