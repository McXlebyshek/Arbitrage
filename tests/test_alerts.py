from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from arbitrage.alerts import AlertPolicy, persist, read_alerts
from arbitrage.__main__ import save
from arbitrage.autocare import CarePolicy, prune_cycle, read_archive
from arbitrage.signals import SignalTracker
from test_signals import update, row, NOW


def confirmed(stamp=NOW):
    tracker=SignalTracker()
    for offset in (0,1000,2000):
        signals=update(tracker,stamp+offset,row(stamp+offset))
    return tracker, signals


class AlertTests(unittest.TestCase):
    def test_confirmed_notice_is_durable_and_idempotent_per_event_and_episode(self):
        _, signals=confirmed()
        with TemporaryDirectory() as directory:
            path=Path(directory)/'alerts.sqlite3'
            with closing(sqlite3.connect(path)) as db,db:
                self.assertEqual(len(persist(db,signals,now_ms=NOW+2000)),1)
                self.assertEqual(persist(db,signals,now_ms=NOW+2000),[])
                duplicate=deepcopy(signals)
                duplicate['events'][0]['event_id']='another-id-same-episode'
                self.assertEqual(persist(db,duplicate,now_ms=NOW+2000),[])
            before=path.read_bytes()
            values=read_alerts(path)
            self.assertEqual(len(values),1)
            self.assertEqual(values[0]['kind'],'opened')
            self.assertFalse(values[0]['execution_verified'])
            self.assertEqual(before,path.read_bytes())

    def test_old_or_inconsistent_confirmation_cannot_raise_a_notice(self):
        _, signals=confirmed()
        for mutation in ('stale','count','mismatch'):
            value=deepcopy(signals)
            now=NOW+2000
            if mutation=='stale':
                now+=6000
            elif mutation=='count':
                value['rows'][0]['confirmations']=1
            else:
                value['rows'][0]['conditional_net_usdt']=100
            with closing(sqlite3.connect(':memory:')) as db:
                self.assertEqual(persist(db,value,now_ms=now),[])
                self.assertEqual(db.execute("SELECT kind FROM local_alerts").fetchone()[0],'suppressed')

    def test_pair_cooldown_persists_across_new_sessions(self):
        with closing(sqlite3.connect(':memory:')) as db:
            _,first=confirmed()
            self.assertEqual(len(persist(db,first,now_ms=NOW+2000)),1)
            _,second=confirmed(NOW+10000)
            self.assertEqual(persist(db,second,now_ms=NOW+12000),[])
            _,third=confirmed(NOW+70000)
            self.assertEqual(len(persist(db,third,now_ms=NOW+72000)),1)

    def test_lost_stopped_and_recovery_close_only_notified_episodes_once(self):
        for ending in ('lost','stopped','recovered'):
            tracker,signals=confirmed()
            with closing(sqlite3.connect(':memory:')) as db:
                persist(db,signals,now_ms=NOW+2000)
                if ending=='lost':
                    value=update(tracker,NOW+3000,row(NOW+3000,value=-1))
                elif ending=='stopped':
                    value=update(tracker,NOW+3000,state='stopped')
                else:
                    event=dict(signals['events'][0],event_id='recovery-event',kind='ended',to_state='ended',timestamp_ms=NOW+3000,reasons=['previous_session_interrupted'])
                    value=dict(rows=[],events=[],recovered_events=[event])
                notices=persist(db,value,now_ms=NOW+3000)
                self.assertEqual([x['kind'] for x in notices],['closed'])
                self.assertEqual(persist(db,value,now_ms=NOW+3000),[])
            with closing(sqlite3.connect(':memory:')) as db:
                self.assertEqual(persist(db,value,now_ms=NOW+3000),[])

    def test_alert_failure_rolls_back_source_events_and_success_is_atomic(self):
        _,signals=confirmed()
        real=persist
        def fail(db,snapshot,policy):
            real(db,snapshot,policy,now_ms=NOW+2000)
            raise RuntimeError('Injected journal failure')
        with TemporaryDirectory() as directory:
            path=Path(directory)/'atomic.sqlite3'
            with patch('arbitrage.alerts.persist',side_effect=fail), self.assertRaises(RuntimeError):
                save(path,[],signals=signals)
            self.assertEqual(read_alerts(path),[])
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM signal_snapshots').fetchone()[0],0)
            with patch('arbitrage.alerts.time.time',return_value=(NOW+2000)/1000):
                self.assertEqual(len(save(path,[],signals=signals)),1)
                self.assertEqual(save(path,[],signals=signals),[])

    def test_disabled_policy_and_invalid_cooldowns(self):
        _,signals=confirmed()
        with closing(sqlite3.connect(':memory:')) as db:
            self.assertEqual(persist(db,signals,AlertPolicy(False),now_ms=NOW+2000),[])
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='local_alerts'").fetchone())
        for value in (-1,float('nan'),86401):
            with self.assertRaises(ValueError):
                AlertPolicy(cooldown_seconds=value)

    def test_retention_archives_journal_and_does_not_reuse_cursor_ids(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'care.sqlite3'
            _,signals=confirmed()
            with closing(sqlite3.connect(path)) as db,db:
                persist(db,signals,now_ms=NOW+2000)
            identity=read_alerts(path)[0]['id']
            result=prune_cycle(path,CarePolicy(raw_days=1,summary_days=2),now=NOW+3*86400000)
            self.assertEqual(result['deleted_rows']['local_alerts'],1)
            self.assertEqual(len(read_archive(result['backup'],'local_alerts')['rows']),1)
            self.assertEqual(read_alerts(path),[])
            _,new=confirmed(NOW+3*86400000)
            with closing(sqlite3.connect(path)) as db,db:
                persist(db,new,now_ms=NOW+3*86400000+2000)
            self.assertGreater(read_alerts(path)[0]['id'],identity)
            self.assertEqual(len(read_alerts(path,after_id=identity)),1)

    def test_reading_absent_or_legacy_database_never_creates_a_journal(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'missing.sqlite3'
            self.assertEqual(read_alerts(path),[])
            self.assertFalse(path.exists())
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE unrelated(value INTEGER)')
            before=path.read_bytes()
            self.assertEqual(read_alerts(path),[])
            self.assertEqual(before,path.read_bytes())
