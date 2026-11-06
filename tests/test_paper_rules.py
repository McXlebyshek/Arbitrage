from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from arbitrage.paper import PaperLedger
from arbitrage.paper_rules import RulePolicy, tick
from test_paper import document, later
from test_economics import NOW


def shifted(source, offset, pair='test', episode='new-episode'):
    def shift(value):
        if isinstance(value, dict): return {key: shift(item) for key, item in value.items()}
        if isinstance(value, list): return [shift(item) for item in value]
        if type(value) is int and value >= NOW-10000: return value+offset
        return value
    result = shift(deepcopy(source))
    result['economics']['rows'][0]['id'] = pair
    result['signals']['rows'][0].update(id=pair, episode_id=episode)
    return result


class PaperRulesTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'paper.sqlite3'
        self.snapshot = Path(self.temp.name)/'latest.json'
        self.doc, self.registry = document()
        self.ledger = PaperLedger(self.path)
        self.policy = RulePolicy()

    def run_tick(self, doc=None, policy=None, apply=True):
        doc = self.doc if doc is None else doc
        self.snapshot.write_text(json.dumps(doc), encoding='utf-8')
        return tick(self.path, self.snapshot, self.registry, policy or self.policy, apply, doc['generated_at_ms'])

    def open(self, policy=None):
        return self.ledger.open(self.doc, 'test', self.registry, now_ms=NOW, automation=(policy or self.policy).document())

    def test_preview_does_not_create_ledger_or_claim_validated_entry(self):
        report = self.run_tick(apply=False)
        self.assertEqual(report['decisions'][0]['action'], 'entry_candidate')
        self.assertTrue(report['decisions'][0]['revalidation_required'])
        self.assertFalse(self.path.exists())

    def test_apply_restart_and_duplicate_snapshot_do_not_duplicate_entry(self):
        self.assertEqual(self.run_tick()['decisions'][0]['action'], 'opened')
        self.run_tick()
        self.assertEqual(PaperLedger(self.path).report()['open_count'], 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM paper_events WHERE kind='opened'").fetchone()[0], 1)

    def test_take_profit_closes_with_durable_reason_and_unknown_net(self):
        position = self.open()
        report = self.run_tick(later(self.doc))
        self.assertEqual(report['decisions'][0]['reason'], 'take_profit_before_funding')
        closed = self.ledger.report()['positions'][0]
        self.assertEqual(closed['id'], position['id'])
        self.assertEqual(closed['state'], 'closed')
        self.assertIsNone(closed['valuation']['net_pnl_usdt'])
        with closing(sqlite3.connect(self.path)) as db:
            event = json.loads(db.execute("SELECT payload FROM paper_events WHERE kind='closed'").fetchone()[0])
        self.assertEqual(event['close_reason'], closed['close_reason'])

    def test_exit_uses_persisted_policy_despite_new_process_config(self):
        self.open()
        report = self.run_tick(later(self.doc), RulePolicy(take_profit_usdt='999'))
        self.assertEqual(report['decisions'][0]['action'], 'closed')

    def test_stop_loss_closes_from_fresh_exit_depth(self):
        self.open()
        doc = later(self.doc)
        for leg in doc['economics']['inputs']:
            leg['book']['bids'] = [[90, 100]]
            leg['book']['asks'] = [[110, 100]]
        self.assertEqual(self.run_tick(doc)['decisions'][0]['reason'], 'stop_loss_before_funding')

    def test_time_exit_and_preview_do_not_mutate(self):
        self.open(RulePolicy(take_profit_usdt='999', stop_loss_usdt='999', max_hold_seconds=1))
        report = self.run_tick(later(self.doc), apply=False)
        self.assertEqual(report['decisions'][0]['action'], 'would_close')
        self.assertEqual(report['decisions'][0]['reason'], 'max_hold')
        self.assertEqual(self.ledger.report()['open_count'], 1)
        self.assertEqual(self.run_tick(later(self.doc))['decisions'][0]['action'], 'closed')

    def test_manual_positions_are_never_closed(self):
        self.ledger.open(self.doc, 'test', self.registry, now_ms=NOW)
        self.assertEqual(self.run_tick(later(self.doc))['decisions'], [])
        self.assertEqual(self.ledger.report()['open_count'], 1)

    def test_missing_depth_does_not_force_time_exit(self):
        self.open(RulePolicy(max_hold_seconds=1))
        doc = later(self.doc)
        doc['economics']['inputs'] = []
        report = self.run_tick(doc)
        self.assertIn('awaiting_depth', report['decisions'][0]['error'])
        self.assertEqual(self.ledger.report()['open_count'], 1)

    def test_stopped_or_stale_snapshot_cannot_open(self):
        doc = deepcopy(self.doc)
        doc['economics']['state'] = 'stopped'
        self.assertEqual(self.run_tick(doc)['state'], 'blocked')
        self.snapshot.write_text(json.dumps(self.doc), encoding='utf-8')
        self.assertEqual(tick(self.path, self.snapshot, self.registry, self.policy, True, NOW+6000)['state'], 'blocked')
        self.assertFalse(self.path.exists())

    def test_rule_entry_threshold_is_recomputed_by_ledger(self):
        with self.assertRaisesRegex(ValueError, 'below_threshold'):
            self.open(RulePolicy(min_entry_usdt='999'))
        self.assertFalse(self.path.exists())

    def test_capacity_and_cooldown_survive_restart_inside_ledger(self):
        self.open()
        candidate = shifted(self.doc, 1000, pair='other')
        with self.assertRaisesRegex(ValueError, 'position_limit'):
            self.ledger.open(candidate, 'other', self.registry, now_ms=NOW+1000, automation=self.policy.document())
        self.run_tick(later(self.doc))
        candidate = shifted(self.doc, 2000)
        with self.assertRaisesRegex(ValueError, 'cooldown'):
            PaperLedger(self.path).open(candidate, 'test', self.registry, now_ms=NOW+2000, automation=self.policy.document())

    def test_automatic_close_revalidates_trigger_and_ownership(self):
        position = self.open(RulePolicy(take_profit_usdt='999'))
        with self.assertRaisesRegex(ValueError, 'exit_not_triggered'):
            self.ledger.value(later(self.doc), position['id'], self.registry, close=True, now_ms=NOW+1000, automatic=True)
        self.assertEqual(self.ledger.report()['open_count'], 1)

    def test_direct_automatic_close_refuses_manual_position(self):
        position = self.ledger.open(self.doc, 'test', self.registry, now_ms=NOW)
        with self.assertRaisesRegex(ValueError, 'not_owned'):
            self.ledger.value(later(self.doc), position['id'], self.registry, close=True, now_ms=NOW+1000, automatic=True)
        self.assertEqual(self.ledger.report()['open_count'], 1)

    def test_later_new_episode_can_reenter_after_cooldown(self):
        self.open()
        self.run_tick(later(self.doc))
        candidate = shifted(self.doc, 302000)
        result = self.ledger.open(candidate, 'test', self.registry, now_ms=NOW+302000, automation=self.policy.document())
        self.assertEqual(result['state'], 'open')
        self.assertEqual(self.ledger.report()['closed_count'], 1)

    def test_same_entry_depth_cannot_trigger_stop_close(self):
        self.open(RulePolicy(stop_loss_usdt='1'))
        report = self.run_tick()
        self.assertIn('requires_later_depth', report['decisions'][0]['error'])
        self.assertEqual(self.ledger.report()['open_count'], 1)

    def test_replay_ledger_rejected(self):
        replay = PaperLedger(self.path, replay=True)
        replay.open(self.doc, 'test', self.registry)
        self.assertIn('require_live_ledger', self.run_tick()['error'])
        with self.assertRaisesRegex(ValueError, 'require_live_ledger'):
            replay.open(self.doc, 'test', self.registry, automation=self.policy.document())

    def test_invalid_config_is_rejected(self):
        for config in ({'take_profit_usdt':'NaN'}, {'stop_loss_usdt':0}, {'min_entry_usdt':True}, {'max_positions':11}, {'cooldown_seconds':-1}, {'max_hold_seconds':True}):
            with self.subTest(config=config), self.assertRaises(ValueError): RulePolicy(**config)

    def test_corrupted_confirmed_evidence_cannot_open(self):
        doc = deepcopy(self.doc)
        doc['signals']['rows'][0]['confirmations'] = 1
        report = self.run_tick(doc)
        self.assertIn('requires_current_confirmed', report['decisions'][0]['error'])
        self.assertEqual(self.ledger.report()['open_count'], 0)
