import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from market_monitor.contract_details import INDEX_MAX_AGE, contract_index, contract_limits

NOW = 1_800_000_000_000


class ContractDetailsTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_okx_underlying_and_rejects_wrong_or_expired_composition(self):
        market = {'id': 'BTC-USDC-SWAP', 'info': {'uly': 'BTC-USDC'}}
        data = dict(index='BTC-USDC', ts=str(NOW),
                    components=[dict(exch='Test', symbol='BTC/USDC', wgt='0')])
        get = AsyncMock(return_value={'data': data})
        client = SimpleNamespace(public_get_market_index_components=get)
        with patch('market_monitor.contract_details.now_ms', return_value=NOW):
            result = await contract_index('okx', client, market, NOW)
            get.assert_awaited_once_with({'index': 'BTC-USDC'})
            self.assertEqual(result['components'][0]['weight'], 0)
            data['index'] = 'BTC-USD'
            with self.assertRaises(ValueError):
                await contract_index('okx', client, market, NOW)
            data.update(index='BTC-USDC', ts=str(NOW - INDEX_MAX_AGE - 1))
            with self.assertRaises(ValueError):
                await contract_index('okx', client, market, NOW)

    async def test_catalog_components_keep_unpublished_weights_unknown(self):
        market = {'id': 'BTC_USDT', 'info': {'indexOrigin': ['MEXC', 'MEXC_FUTURE']}}
        result = await contract_index('mexc', None, market, NOW)
        self.assertTrue(result['fromCatalog'])
        self.assertEqual(result['components'], [
            dict(exchange=name, symbol=None, weight=None) for name in ('MEXC', 'MEXC_FUTURE')
        ])

    def test_keeps_order_caps_separate_from_directional_risk_limits(self):
        market = dict(contractSize=0.01, info=dict(minVol=1, maxVol=100, limitMaxVol=250,
                      riskLimitType='BY_VOLUME', riskLongShortSwitch=1, riskBaseVolLong=500,
                      riskBaseVolShort=700, riskBaseVol=900))
        self.assertEqual(contract_limits('mexc', market), dict(
            inContracts=True, contractSize=0.01, orderMin=1, marketMax=100, limitMax=250,
            riskBaseLong=500, riskBaseShort=700,
        ))
        market['info']['riskLimitType'] = 'BY_VALUE'
        self.assertNotIn('riskBaseLong', contract_limits('mexc', market))
