import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import ccxt.async_support as ccxt

from arbitrage.collector import public_client_config
from arbitrage.live import LiveExchange


class MetadataLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_gate_loads_only_usdt_swap_endpoint_without_currency_or_other_catalogs(self):
        client=ccxt.gate(public_client_config('gate'))
        rows=json.loads((Path(__file__).resolve().parents[1]/'docs/reports/REGISTRY_V033_CATALOG.json').read_text(encoding='utf-8'))['gate']
        client.publicFuturesGetSettleContracts=AsyncMock(return_value=[row['info'] for row in rows])
        client.fetch_currencies=AsyncMock(side_effect=AssertionError('Unneeded currency request'))
        client.fetch_spot_markets=AsyncMock(side_effect=AssertionError('Unneeded spot request'))
        client.fetch_future_markets=AsyncMock(side_effect=AssertionError('Unneeded delivery request'))
        client.fetch_option_markets=AsyncMock(side_effect=AssertionError('Unneeded option request'))
        try:
            markets=await client.load_markets()
            self.assertEqual(len(markets),20)
            self.assertTrue(all(row['swap'] and row['settle']=='USDT' for row in markets.values()))
            client.publicFuturesGetSettleContracts.assert_awaited_once_with({'settle':'usdt'})
            client.fetch_currencies.assert_not_awaited()
            client.fetch_spot_markets.assert_not_awaited()
            client.fetch_future_markets.assert_not_awaited()
            client.fetch_option_markets.assert_not_awaited()
        finally:await client.close()

    async def test_gate_transient_metadata_timeout_retries_once_and_closes_client(self):
        market=dict(id='BTC_USDT',symbol='BTC/USDT:USDT',base='BTC',swap=True,linear=True,quote='USDT',settle='USDT',active=True)
        client=SimpleNamespace(load_markets=AsyncMock(side_effect=[ccxt.RequestTimeout('timeout'),{'BTC':market}]),close=AsyncMock())
        worker=LiveExchange('gate',{'BTC'})
        with patch('ccxt.async_support.gate',return_value=client),patch('arbitrage.live.asyncio.sleep',new=AsyncMock()):
            await worker.load_markets()
        self.assertEqual(client.load_markets.await_count,2)
        self.assertIn('BTC_USDT',worker.markets)
        client.close.assert_awaited_once()

    async def test_gate_rate_limit_and_bad_response_are_not_retried(self):
        for error in (ccxt.RateLimitExceeded('429'),ccxt.ExchangeError('invalid response')):
            with self.subTest(error=type(error).__name__):
                client=SimpleNamespace(load_markets=AsyncMock(side_effect=error),close=AsyncMock())
                with patch('ccxt.async_support.gate',return_value=client),self.assertRaises(type(error)):
                    await LiveExchange('gate',None).load_markets()
                self.assertEqual(client.load_markets.await_count,1)
                client.close.assert_awaited_once()

    async def test_persistent_gate_timeout_is_bounded_to_two_attempts(self):
        client=SimpleNamespace(load_markets=AsyncMock(side_effect=ccxt.RequestTimeout('timeout')),close=AsyncMock())
        with patch('ccxt.async_support.gate',return_value=client),patch('arbitrage.live.asyncio.sleep',new=AsyncMock()),self.assertRaises(ccxt.RequestTimeout):
            await LiveExchange('gate',None).load_markets()
        self.assertEqual(client.load_markets.await_count,2)
        client.close.assert_awaited_once()

    async def test_cancellation_during_metadata_request_closes_without_retry(self):
        entered=asyncio.Event()
        async def waiting(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        client=SimpleNamespace(load_markets=AsyncMock(side_effect=waiting),close=AsyncMock())
        with patch('ccxt.async_support.gate',return_value=client):
            task=asyncio.create_task(LiveExchange('gate',None).load_markets())
            await entered.wait();task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(client.load_markets.await_count,1)
        client.close.assert_awaited_once()
