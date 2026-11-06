from contextlib import closing
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from arbitrage.economics import evaluate_pair
from arbitrage.paper import PaperLedger
from arbitrage.signals import SignalTracker
from test_economics import fixture, NOW


def document():
    pair, legs, registry = fixture()
    result = evaluate_pair(pair, legs, registry, NOW)
    tracker = SignalTracker()
    for stamp in (NOW-2000, NOW-1000, NOW):
        signals = tracker.update(dict(state='ready', rows=[dict(result, depth_source_times=dict(long=stamp, short=stamp))]), dict(rows=[pair]), stamp)
    policy = dict(capital_usdt=1000, hold_hours=8, max_impact_pct=.1, min_budget_utilization=.9,
        extra_cost_usdt=0, fee_overrides={}, max_skew_ms=1000, min_volume_usdt=1000000)
    snapshot = dict(generated_at_ms=NOW, signals=signals, economics=dict(state='ready', generated_at_ms=NOW,
        policy=policy, rows=[result], inputs=[dict(exchange=key[0], native_id=key[1], **value) for key, value in legs.items()]))
    return snapshot, registry


def later(original, offset=1000):
    value = deepcopy(original)
    stamp = NOW+offset
    value['generated_at_ms'] = value['economics']['generated_at_ms'] = stamp
    for leg in value['economics']['inputs']:
        leg['book']['timestamp'] = leg['received_at_ms'] = stamp
        # New depth: both venues meet around 101, with a two-tick cross-spread.
        leg['book']['bids'] = [[100.9, 100]]
        leg['book']['asks'] = [[101.1, 100]]
    return value


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'paper.sqlite3'
        self.ledger = PaperLedger(self.path, replay=True)
        self.snapshot, self.registry = document()

    def open(self):
        return self.ledger.open(self.snapshot, 'test', self.registry)

    def test_entry_equal_quantity_fees_budget_and_evidence_are_saved(self):
        position = self.open()
        self.assertEqual(position['base_quantity'], '4.944')
        self.assertEqual(position['entry_fills']['long']['contracts'], position['entry_fills']['short']['contracts'])
        self.assertLessEqual(Decimal(position['reserved_usdt']), 1000)
        self.assertEqual(Decimal(position['entry_fees_usdt']), Decimal('0.499344'))
        self.assertAlmostEqual(float(position['valuation']['pnl_before_funding_usdt']), -10.886688)
        self.assertIsNone(position['valuation']['funding_usdt'])
        self.assertIsNone(position['valuation']['net_pnl_usdt'])
        self.assertFalse(position['execution_verified'])
        self.assertEqual(len(position['entry_evidence']['inputs']), 2)

    def test_fixed_quantity_close_uses_later_vwap_and_four_fees_without_double_slippage(self):
        position = self.open()
        snapshot = later(self.snapshot)
        long = snapshot['economics']['inputs'][0]
        long['book']['bids'] = [[100.9, 2], [100.8, 100]]
        closed = self.ledger.value(snapshot, position['id'], self.registry, close=True)
        q = Decimal('4.944')
        long_value = Decimal('2')*Decimal('100.9')+(q-2)*Decimal('100.8')
        short_value = q*Decimal('101.1')
        gross = q*2+long_value-short_value
        fees = Decimal('0.499344')+(long_value+short_value)*Decimal('.0005')
        self.assertAlmostEqual(float(closed['valuation']['price_pnl_usdt']), float(gross))
        self.assertAlmostEqual(float(closed['valuation']['pnl_before_funding_usdt']), float(gross-fees))
        self.assertEqual(closed['base_quantity'], position['base_quantity'])
        self.assertEqual(closed['state'], 'closed')
        self.assertEqual(self.ledger.report()['closed_count'], 1)

    def test_idempotent_entry_mark_and_close_and_no_reentry_same_episode(self):
        position = self.open()
        self.assertEqual(self.open()['id'], position['id'])
        snapshot = later(self.snapshot)
        self.ledger.value(snapshot, position['id'], self.registry)
        self.ledger.value(snapshot, position['id'], self.registry)
        first = self.ledger.value(snapshot, position['id'], self.registry, close=True)
        self.assertEqual(self.ledger.value(snapshot, position['id'], self.registry, close=True), first)
        self.assertEqual(self.open()['state'], 'closed')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM paper_events').fetchone()[0], 3)
        changed = deepcopy(self.snapshot)
        changed['irrelevant'] = 'different snapshot, same episode'
        with self.assertRaisesRegex(ValueError, 'episode_already_used'):
            self.ledger.open(changed, 'test', self.registry)

    def test_stale_live_snapshot_rejected_and_replay_cannot_mix(self):
        live = PaperLedger(self.path)
        with self.assertRaisesRegex(ValueError, 'snapshot_stale'):
            live.open(self.snapshot, 'test', self.registry, now_ms=NOW+6000)
        self.assertFalse(self.path.exists())
        with self.assertRaisesRegex(ValueError, 'depth_stale'):
            live.open(self.snapshot, 'test', self.registry, now_ms=NOW+4000)
        self.open()
        with self.assertRaisesRegex(ValueError, 'separate_ledgers'):
            live.open(self.snapshot, 'test', self.registry, now_ms=NOW)

    def test_confirmed_and_recalculated_positive_scenario_required(self):
        for alteration in ('state', 'count', 'net', 'source', 'time'):
            snapshot = deepcopy(self.snapshot)
            signal = snapshot['signals']['rows'][0]
            if alteration == 'state': signal['state'] = 'watching'
            if alteration == 'count': signal['confirmations'] = 1
            if alteration == 'net': signal['conditional_net_usdt'] = 99
            if alteration == 'source': signal['last_sources']['long'] -= 1
            if alteration == 'time': snapshot['signals']['generated_at_ms'] -= 1
            with self.assertRaises(ValueError): self.ledger.open(snapshot, 'test', self.registry)
        self.assertFalse(self.path.exists())

    def test_insufficient_exit_depth_keeps_position_and_last_good_mark(self):
        position = self.open()
        snapshot = later(self.snapshot)
        snapshot['economics']['inputs'][1]['book']['asks'] = [[101.1, 1]]
        with self.assertRaisesRegex(ValueError, 'insufficient_depth'):
            self.ledger.value(snapshot, position['id'], self.registry, close=True)
        report = self.ledger.report()
        self.assertEqual(report['open_count'], 1)
        self.assertEqual(report['positions'][0]['valuation']['timestamp_ms'], NOW)

    def test_stale_skew_future_and_changed_instrument_cannot_close(self):
        position = self.open()
        for alteration in ('stale', 'skew', 'future', 'size', 'step'):
            snapshot = later(self.snapshot, 5000)
            leg = snapshot['economics']['inputs'][0]
            if alteration == 'stale': leg['book']['timestamp'] = NOW
            if alteration == 'skew': leg['book']['timestamp'] -= 2000
            if alteration == 'future': leg['book']['timestamp'] += 2000
            if alteration == 'size': leg['market']['contractSize'] = 2
            if alteration == 'step': leg['market']['precision']['amount'] = 7
            with self.assertRaises(ValueError): self.ledger.value(snapshot, position['id'], self.registry, close=True)
        self.assertEqual(self.ledger.report()['open_count'], 1)

    def test_current_selection_can_omit_pair_and_exit_uses_original_quantity(self):
        position = self.open()
        snapshot = later(self.snapshot)
        snapshot['economics']['rows'] = []
        snapshot['signals'] = None
        snapshot['economics']['policy']['capital_usdt'] = 100
        marked = self.ledger.value(snapshot, position['id'], self.registry)
        self.assertEqual(marked['base_quantity'], position['base_quantity'])
        self.assertEqual(marked['policy']['capital_usdt'], 1000)
        older = later(self.snapshot, 500)
        with self.assertRaisesRegex(ValueError, 'time_reversed'):
            self.ledger.value(older, position['id'], self.registry)

    def test_same_snapshot_cannot_close_and_unknown_position_does_not_alter_book(self):
        position = self.open()
        with self.assertRaisesRegex(ValueError, 'later_depth'):
            self.ledger.value(self.snapshot, position['id'], self.registry, close=True)
        with self.assertRaisesRegex(ValueError, 'not_found'):
            self.ledger.value(later(self.snapshot), 'unknown', self.registry)
        self.assertEqual(self.ledger.report()['open_count'], 1)

    def test_duplicate_pair_and_aggregate_budget_enforced(self):
        self.open()
        snapshot = deepcopy(self.snapshot)
        snapshot['signals']['rows'][0]['episode_id'] = 'new-episode'
        with self.assertRaisesRegex(ValueError, 'pair_already_open'):
            self.ledger.open(snapshot, 'test', self.registry)
        snapshot['economics']['rows'][0]['id'] = 'second-direction'
        snapshot['signals']['rows'][0]['id'] = 'second-direction'
        with self.assertRaisesRegex(ValueError, 'portfolio_budget_exceeded'):
            self.ledger.open(snapshot, 'second-direction', self.registry)
        self.assertEqual(self.ledger.report()['open_count'], 1)

    def test_failure_inside_transaction_rolls_back_position_and_event(self):
        self.open()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER fail_paper_event BEFORE INSERT ON paper_events BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.value(later(self.snapshot), self.ledger.report()['positions'][0]['id'], self.registry, close=True)
        report = self.ledger.report()
        self.assertEqual(report['open_count'], 1)
        self.assertEqual(report['positions'][0]['valuation']['timestamp_ms'], NOW)

    def test_status_read_only_and_market_database_protected(self):
        self.assertEqual(self.ledger.report()['open_count'], 0)
        self.assertFalse(self.path.exists())
        self.open()
        before = self.path.read_bytes()
        self.ledger.report()
        self.assertEqual(before, self.path.read_bytes())
        market = Path(self.temp.name)/'market.sqlite3'
        with closing(sqlite3.connect(market)) as db:
            db.execute('CREATE TABLE snapshots(id)')
        before = market.read_bytes()
        with self.assertRaisesRegex(ValueError, 'dedicated_database'):
            PaperLedger(market, replay=True).open(self.snapshot, 'test', self.registry)
        self.assertEqual(before, market.read_bytes())

    def test_live_entry_restart_and_close_release_reserved_budget(self):
        live = PaperLedger(self.path)
        position = live.open(self.snapshot, 'test', self.registry, now_ms=NOW)
        restarted = PaperLedger(self.path)
        restarted.value(later(self.snapshot), position['id'], self.registry, close=True, now_ms=NOW+1000)
        snapshot = deepcopy(self.snapshot)
        snapshot['signals']['rows'][0]['episode_id'] = 'new-episode-after-close'
        with self.assertRaisesRegex(ValueError, 'reentry_requires_later_snapshot'):
            restarted.open(snapshot, 'test', self.registry, now_ms=NOW)
        stamp = NOW+2000
        snapshot['generated_at_ms'] = snapshot['economics']['generated_at_ms'] = snapshot['signals']['generated_at_ms'] = stamp
        for leg in snapshot['economics']['inputs']:
            leg['book']['timestamp'] = leg['received_at_ms'] = stamp
        snapshot['signals']['rows'][0]['last_sources'] = dict(long=stamp, short=stamp)
        snapshot['economics']['rows'][0]['depth_source_times'] = dict(long=stamp, short=stamp)
        new = restarted.open(snapshot, 'test', self.registry, now_ms=stamp)
        self.assertNotEqual(new['id'], position['id'])
        self.assertEqual(restarted.report()['open_count'], 1)
        self.assertEqual(restarted.report()['closed_count'], 1)

    def test_marks_cannot_move_source_depth_backwards(self):
        position = self.open()
        self.ledger.value(later(self.snapshot, 2000), position['id'], self.registry)
        snapshot = later(self.snapshot, 3000)
        snapshot['economics']['inputs'][0]['book']['timestamp'] = NOW+1000
        with self.assertRaisesRegex(ValueError, 'depth_time_reversed'):
            self.ledger.value(snapshot, position['id'], self.registry)
