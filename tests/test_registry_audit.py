from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from dataclasses import asdict
import json
import unittest

from arbitrage.models import normalize
from arbitrage.pools import ExchangePool
from arbitrage.scanner import Registry, scan
from validate_registry import audit


class RegistryAuditTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[1]
        self.registry=Registry.load()
        self.catalog=json.loads((root/'docs/reports/REGISTRY_V033_CATALOG.json').read_text(encoding='utf-8'))

    def test_all_pinned_contracts_match_saved_public_metadata(self):
        result=audit(self.registry,self.catalog)
        self.assertTrue(result['passed'])
        self.assertEqual(len(result['checked']),120)

    def test_missing_or_duplicate_native_id_is_not_accepted(self):
        for change in ('missing','duplicate'):
            with self.subTest(change=change):
                catalog=deepcopy(self.catalog)
                if change=='missing': catalog['okx'].pop(0)
                else: catalog['okx'].append(catalog['okx'][0])
                result=audit(self.registry,catalog)
                self.assertFalse(result['passed'])
                self.assertEqual(result['errors'][0]['reason'],'missing_or_ambiguous_contract')

    def test_identity_settlement_or_activity_drift_is_rejected(self):
        for key,value,reason in (('base','OTHER','base_identity_changed'),('settle','USDC','not_active_linear_usdt_perpetual'),('active',False,'not_active_linear_usdt_perpetual'),('linear',False,'not_active_linear_usdt_perpetual')):
            with self.subTest(key=key):
                catalog=deepcopy(self.catalog);catalog['okx'][0][key]=value
                self.assertEqual(audit(self.registry,catalog)['errors'][0]['reason'],reason)

    def test_changed_missing_or_nonfinite_contract_size_is_rejected(self):
        for size,reason in ((999,'contract_size_changed'),(None,'invalid_contract_size'),('NaN','invalid_contract_size')):
            with self.subTest(size=size):
                catalog=deepcopy(self.catalog);catalog['okx'][0]['contractSize']=size
                self.assertEqual(audit(self.registry,catalog)['errors'][0]['reason'],reason)

    def test_all_600_synthetic_directions_preserve_base_exposure_across_contract_sizes(self):
        snapshots=[]
        for exchange,markets in self.catalog.items():
            quotes=[asdict(normalize(exchange,m,dict(bid=100,ask=101,timestamp=10000),10000,
                'ws_depth_verified' if exchange=='mexc' else 'ws_bbo')) for m in markets]
            snapshots.append(dict(status=dict(exchange=exchange,state='streaming'),instruments=markets,quotes=quotes))
        result=scan(snapshots,self.registry,10000)
        self.assertEqual(len(result['rows']),600)
        for row in result['rows']:
            for side in ('long','short'):
                binding=self.registry.bindings[(row[side+'_exchange'],row[side+'_symbol'])]
                self.assertAlmostEqual(row['reference_'+side+'_contracts_unrounded']*float(binding['contract_size']*binding['price_units']),row['reference_base_quantity'])
        trx=next(row for row in result['rows'] if row['asset']=='tron' and row['long_exchange']=='okx' and row['short_exchange']=='binanceusdm')
        self.assertEqual(trx['long_base_units_per_contract'],1000)
        self.assertEqual(trx['long_ask_usdt_per_unit'],101)

    def test_twenty_mexc_registered_markets_fit_default_depth_capacity(self):
        markets={m['id']:m for m in self.catalog['mexc']}
        bases={binding['base'] for (exchange,_),binding in self.registry.bindings.items() if exchange=='mexc'}
        pool=ExchangePool('mexc',bases,self.registry)
        chosen,omitted=pool.choose(markets)
        self.assertEqual(len(chosen),20)
        self.assertEqual(omitted,0)
        self.assertEqual(set(chosen),{native for exchange,native in self.registry.bindings if exchange=='mexc'})
