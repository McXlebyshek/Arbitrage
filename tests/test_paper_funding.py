from contextlib import closing
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from arbitrage.funding_history import persist
from arbitrage.paper import PaperLedger
from arbitrage.paper_funding import reconcile
from test_economics import NOW, HOUR
from test_paper import document, later


class PaperFundingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.market = Path(self.temp.name)/'market.sqlite3'
        self.path = Path(self.temp.name)/'paper.sqlite3'
        self.ledger = PaperLedger(self.path, replay=True)
        snapshot, registry = document()
        opened = self.ledger.open(snapshot, 'test', registry)
        self.position = self.ledger.value(later(snapshot, 9*HOUR), opened['id'], registry, close=True)
        self.snapshot, self.registry = snapshot, registry
        self.prices = [dict(exchange='alpha', native_id='BTC_USDT', timestamp_ms=NOW+8*HOUR, mark_price_native='100', source='ARTIFICIAL settlement mark'),
            dict(exchange='beta', native_id='BTC_USDT', timestamp_ms=NOW+4*HOUR, mark_price_native='102', source='ARTIFICIAL settlement mark'),
            dict(exchange='beta', native_id='BTC_USDT', timestamp_ms=NOW+8*HOUR, mark_price_native='104', source='ARTIFICIAL settlement mark')]
        self.archive()

    def archive(self):
        for exchange, hours, rates in (('alpha', [-16,-8,0,8], ['.001']*4),
                ('beta', [-8,-4,0,4,8], ['.001','.001','.001','.002','.001'])):
            persist(self.market, exchange, dict(id='BTC_USDT', asset='bitcoin'),
                dict(events=[dict(timestamp=NOW+hour*HOUR, rate=rate) for hour,rate in zip(hours,rates)], requests=1, error=None), NOW+9*HOUR)

    def apply(self, prices=None):
        return self.ledger.funding(self.position['id'], self.market, self.prices if prices is None else prices)

    def test_rates_signs_different_intervals_and_marks_produce_paper_net(self):
        result = self.apply()
        funding = result['funding']
        self.assertTrue(funding['complete'])
        self.assertEqual(len(funding['events']), 3)
        self.assertEqual([row['side'] for row in funding['events']], ['short','long','short'])
        expected = Decimal('4.944')*(-Decimal('100')*Decimal('.001')+Decimal('102')*Decimal('.002')+Decimal('104')*Decimal('.001'))
        self.assertEqual(Decimal(funding['funding_usdt']), expected)
        self.assertEqual(Decimal(result['valuation']['net_pnl_usdt']), Decimal('7.900512')+expected)
        self.assertFalse(funding['execution_verified'])
        self.assertFalse(funding['price_evidence_verified'])
        self.assertEqual(self.ledger.report()['funding_covered_closed_count'], 1)

    def test_missing_marks_preserve_explicit_estimate_and_null_net(self):
        result = self.apply([])
        self.assertEqual(result['funding']['status'], 'rates_covered_marks_missing')
        self.assertEqual(len(result['funding']['missing_mark_prices']), 3)
        self.assertIsNone(result['valuation']['net_pnl_usdt'])
        estimate = Decimal('4.944')*(-Decimal('100')*Decimal('.001')+Decimal('102')*Decimal('.003'))
        self.assertEqual(Decimal(result['funding']['entry_notional_estimate_usdt']), estimate)
        self.assertIsNotNone(result['valuation']['net_entry_notional_estimate_usdt'])
        self.assertIsNone(self.ledger.report()['net_pnl_usdt'])

    def test_zero_window_with_verified_no_due_event_is_zero_not_unknown(self):
        position = deepcopy(self.position)
        position['valuation']['timestamp_ms'] = NOW+HOUR
        funding = reconcile(position, self.market)
        self.assertTrue(funding['complete'])
        self.assertEqual(funding['events'], [])
        self.assertEqual(funding['funding_usdt'], '0')

    def test_entry_excluded_and_close_event_included(self):
        position = deepcopy(self.position)
        position['valuation']['timestamp_ms'] = NOW+8*HOUR
        result = reconcile(position, self.market, self.prices)
        self.assertEqual(len(result['events']), 3)
        self.assertTrue(all(NOW < row['timestamp_ms'] <= NOW+8*HOUR for row in result['events']))

    def test_gap_tail_or_missing_left_anchor_prevent_complete(self):
        for kind in ('gap', 'tail', 'anchor'):
            with self.subTest(kind=kind):
                self.archive()
                with closing(sqlite3.connect(self.market)) as db, db:
                    stamp = NOW+4*HOUR if kind == 'gap' else NOW+8*HOUR if kind == 'tail' else NOW-8*HOUR
                    db.execute('DELETE FROM funding_history_events WHERE exchange=? AND timestamp_ms=?', ('beta', stamp))
                result = self.apply()
                self.assertFalse(result['funding']['rates_covered'])
                self.assertIsNone(result['valuation']['net_pnl_usdt'])
                self.assertIsNone(result['funding']['entry_notional_estimate_usdt'])

    def test_import_failure_lag_and_wrong_asset_invalidate_old_complete_total(self):
        self.apply()
        for kind in ('error','lag','identity'):
            with self.subTest(kind=kind):
                self.archive()
                with closing(sqlite3.connect(self.market)) as db, db:
                    if kind == 'error': db.execute("UPDATE funding_history_imports SET payload=json_set(payload,'$.error','timeout') WHERE exchange='alpha'")
                    if kind == 'lag': db.execute("UPDATE funding_history_imports SET end_ms=? WHERE exchange='alpha'", (NOW,))
                    if kind == 'identity': db.execute("UPDATE funding_history_events SET asset='different' WHERE exchange='alpha'")
                result = self.apply()
                self.assertFalse(result['funding']['complete'])
                self.assertIsNone(result['valuation']['funding_usdt'])
                self.assertIsNone(result['valuation']['net_pnl_usdt'])

    def test_repeated_reconcile_restart_and_revised_rate_never_double_add(self):
        first = self.apply()
        restarted = PaperLedger(self.path, replay=True)
        second = restarted.funding(self.position['id'], self.market, self.prices)
        self.assertEqual(first, second)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM paper_events WHERE kind='funding_reconciled'").fetchone()[0], 1)
        with closing(sqlite3.connect(self.market)) as db, db:
            db.execute("UPDATE funding_history_events SET rate='-.001' WHERE exchange='alpha' AND timestamp_ms=?", (NOW+8*HOUR,))
        revised = self.apply()
        delta = Decimal('4.944')*Decimal('100')*Decimal('.002')
        self.assertEqual(Decimal(revised['valuation']['funding_usdt'])-Decimal(first['valuation']['funding_usdt']), delta)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM paper_events WHERE kind='funding_reconciled'").fetchone()[0], 2)

    def test_invalid_prices_conflicts_nonfinite_and_future_observations_rejected(self):
        for mutation in ('zero','nan','conflict','source','timestamp'):
            rows = deepcopy(self.prices)
            if mutation == 'zero': rows[0]['mark_price_native'] = 0
            if mutation == 'nan': rows[0]['mark_price_native'] = 'NaN'
            if mutation == 'conflict': rows.append(dict(rows[0], mark_price_native=999))
            if mutation == 'source': rows[0]['source'] = ''
            if mutation == 'timestamp': rows[0]['timestamp_ms'] = True
            with self.assertRaises(ValueError): self.apply(rows)
        with closing(sqlite3.connect(self.market)) as db, db:
            db.execute('UPDATE funding_history_events SET observed_at_ms=?', (int(time.time()*1000)+86400000,))
        self.assertFalse(self.apply()['funding']['complete'])

    def test_native_price_units_and_partial_prices(self):
        position = deepcopy(self.position)
        position['bindings']['long']['price_units'] = '1000'
        prices = deepcopy(self.prices)
        prices[0]['mark_price_native'] = '100000'
        normalized = reconcile(position, self.market, prices)
        regular = reconcile(self.position, self.market, self.prices)
        self.assertEqual(normalized['funding_usdt'], regular['funding_usdt'])
        partial = self.apply(self.prices[:1])
        self.assertIsNone(partial['valuation']['net_pnl_usdt'])
        self.assertEqual(len(partial['funding']['missing_mark_prices']), 2)

    def test_missing_legacy_archive_no_file_creation_and_read_only_source(self):
        missing = Path(self.temp.name)/'missing.sqlite3'
        result = self.ledger.funding(self.position['id'], missing)
        self.assertIsNone(result['valuation']['funding_usdt'])
        self.assertFalse(missing.exists())
        with closing(sqlite3.connect(missing)) as db: db.execute('CREATE TABLE unrelated(value)')
        self.assertFalse(self.ledger.funding(self.position['id'], missing)['funding']['complete'])
        before = self.market.read_bytes()
        self.apply()
        self.assertEqual(before, self.market.read_bytes())
        with self.assertRaisesRegex(ValueError, 'must_differ'):
            self.ledger.funding(self.position['id'], self.path)

    def test_transaction_failure_rolls_back_totals_and_audit(self):
        self.apply([])
        before = self.ledger.report()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER fail_funding BEFORE INSERT ON paper_events BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.apply()
        self.assertEqual(self.ledger.report(), before)

    def test_new_valuation_invalidates_old_funding_while_repeated_mark_preserves_it(self):
        # A second open ledger provides a still-open position for marking.
        ledger = PaperLedger(Path(self.temp.name)/'open.sqlite3', replay=True)
        position = ledger.open(self.snapshot, 'test', self.registry)
        mark = later(self.snapshot, HOUR)
        ledger.value(mark, position['id'], self.registry)
        funded = ledger.funding(position['id'], self.market)
        self.assertEqual(funded['valuation']['funding_usdt'], '0')
        self.assertEqual(ledger.value(mark, position['id'], self.registry)['valuation']['funding_usdt'], '0')
        new = ledger.value(later(self.snapshot, 2*HOUR), position['id'], self.registry)
        self.assertNotIn('funding', new)
        self.assertIsNone(new['valuation']['net_pnl_usdt'])

    def test_event_limit_cannot_turn_truncated_history_into_complete(self):
        with patch('arbitrage.paper_funding.MAX_EVENTS', 1):
            result = self.apply()
        self.assertFalse(result['funding']['complete'])
        self.assertIn('funding_event_limit', result['funding']['coverage']['short']['reasons'])
