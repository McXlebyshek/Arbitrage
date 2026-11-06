from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

import aiohttp

from arbitrage.alerts import persist
from arbitrage.delivery import DeliveryError, DeliveryPolicy, DeliveryWorker, TelegramSender, format_notice
from arbitrage.recovery import SessionInUse, SessionLease
from test_alerts import confirmed, NOW
from test_signals import update


class Sender:
    identity = 'test-destination'

    def __init__(self):
        self.messages, self.errors = [], []

    async def send(self, text):
        if self.errors:
            raise self.errors.pop(0)
        self.messages.append(text)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name)/'market.sqlite3'
        self.state = Path(self.temp.name)/'delivery.sqlite3'
        with closing(sqlite3.connect(self.source)) as db:
            db.execute('CREATE TABLE other(value)')
        self.sender = Sender()
        self.worker = self.make_worker()

    def make_worker(self, policy=None, sender=None):
        worker = DeliveryWorker(self.source, self.state, sender or self.sender, policy)
        self.addCleanup(worker.close)
        return worker

    def opening(self, stamp=NOW):
        tracker, signals = confirmed(stamp)
        with closing(sqlite3.connect(self.source)) as db, db:
            persist(db, signals, now_ms=stamp+2000)
        return tracker, signals

    def closing(self, tracker, stamp):
        signals = update(tracker, stamp)
        with closing(sqlite3.connect(self.source)) as db, db:
            persist(db, signals, now_ms=stamp)

    async def test_starts_at_tail_then_sends_once_and_survives_restart_read_only_source(self):
        self.opening(NOW-70000)
        self.assertEqual((await self.worker.tick(NOW))[0]['status'], 'armed_new_events_only')
        self.opening()
        before = self.source.read_bytes()
        self.assertEqual((await self.worker.tick(NOW+2000))[0]['status'], 'sent')
        again = self.make_worker()
        self.assertEqual(await again.tick(NOW+3000), [])
        self.assertEqual(len(self.sender.messages), 1)
        self.assertIn('Условный net: 2.0000 USDT', self.sender.messages[0])
        self.assertIn('исполнение не подтверждено', self.sender.messages[0])
        self.assertEqual(self.source.read_bytes(), before)

    async def test_min_net_filters_opening_and_does_not_send_orphan_closure(self):
        worker = self.make_worker(DeliveryPolicy(min_net_usdt=2))
        await worker.tick(NOW)
        tracker, _ = self.opening()
        self.assertEqual((await worker.tick(NOW+2000))[0]['error'], 'net_filter')
        self.closing(tracker, NOW+3000)
        self.assertEqual((await worker.tick(NOW+3000))[0]['error'], 'opening_not_delivered')
        self.assertEqual(self.sender.messages, [])

    async def test_closure_follows_sent_opening_even_when_net_is_gone(self):
        await self.worker.tick(NOW)
        tracker, _ = self.opening()
        await self.worker.tick(NOW+2000)
        self.closing(tracker, NOW+3000)
        self.assertEqual((await self.worker.tick(NOW+3000))[0]['status'], 'sent')
        self.assertIn('Условия сигнала потеряны', self.sender.messages[-1])
        self.assertNotIn('Условный net', self.sender.messages[-1])

    async def test_expired_and_already_closed_openings_never_deliver(self):
        await self.worker.tick(NOW)
        self.opening()
        self.assertEqual((await self.worker.tick(NOW+63000))[0]['error'], 'expired')
        tracker, _ = self.opening(NOW+70000)
        self.closing(tracker, NOW+73000)
        result = await self.worker.tick(NOW+74000)
        self.assertEqual([row['error'] for row in result], ['episode_already_closed', 'opening_not_delivered'])
        self.assertEqual(self.sender.messages, [])

    async def test_retry_after_is_durable_and_expiry_wins_over_retry(self):
        await self.worker.tick(NOW)
        self.opening()
        self.sender.errors = [DeliveryError('telegram_rate_limit', True, 10)]
        first = await self.worker.tick(NOW+2000)
        self.assertEqual(first[0]['status'], 'pending')
        again = self.make_worker()
        self.assertEqual(await again.tick(NOW+3000), [])
        second = await again.tick(NOW+12000)
        self.assertEqual(second[0]['attempts'], 2)
        self.assertEqual(len(self.sender.messages), 1)
        self.opening(NOW+70000)
        self.sender.errors = [DeliveryError('telegram_rate_limit', True, 120)]
        await again.tick(NOW+72000)
        self.assertEqual((await again.tick(NOW+133000))[0]['error'], 'expired')
        self.opening(NOW+140000)
        self.assertEqual(await again.tick(NOW+142000), [])
        self.assertEqual((await again.tick(NOW+192000))[0]['status'], 'sent')

    async def test_attempt_limit_and_permanent_rejection_do_not_loop(self):
        worker = self.make_worker(DeliveryPolicy(max_attempts=2))
        await worker.tick(NOW)
        self.opening()
        self.sender.errors = [DeliveryError('transport', True), DeliveryError('transport', True)]
        await worker.tick(NOW+2000)
        self.assertEqual((await worker.tick(NOW+4000))[0]['status'], 'failed')
        self.assertEqual(await worker.tick(NOW+8000), [])
        self.opening(NOW+70000)
        self.sender.errors = [DeliveryError('telegram_rejected')]
        self.assertEqual((await worker.tick(NOW+72000))[0]['status'], 'failed')

    async def test_crash_before_ack_waits_and_consumes_attempt_budget(self):
        await self.worker.tick(NOW)
        self.opening()
        async def crash(text):
            raise RuntimeError('simulated process death')
        self.sender.send = crash
        with self.assertRaises(RuntimeError):
            await self.worker.tick(NOW+2000)
        replacement = Sender()
        again = self.make_worker(sender=replacement)
        self.assertEqual(await again.tick(NOW+3000), [])
        result = await again.tick(NOW+32000)
        self.assertEqual(result[0]['attempts'], 2)
        self.assertEqual(len(replacement.messages), 1)

    async def test_source_replacement_and_destination_change_skip_old_history(self):
        await self.worker.tick(NOW)
        self.opening()
        other = Sender()
        other.identity = 'different-recipient'
        changed = self.make_worker(sender=other)
        self.assertEqual((await changed.tick(NOW+2000))[0]['status'], 'armed_new_events_only')
        await self.worker.tick(NOW+2000)
        self.source.rename(Path(self.temp.name)/'old.sqlite3')
        self.opening(NOW+70000)
        self.assertEqual((await self.worker.tick(NOW+72000))[0]['status'], 'armed_new_events_only')
        self.assertEqual(len(self.sender.messages), 1)
        self.assertEqual(other.messages, [])

    async def test_missing_source_is_not_created_and_event_filter_applies(self):
        self.source.unlink()
        self.assertEqual((await self.worker.tick(NOW))[0]['status'], 'waiting_for_source')
        self.assertFalse(self.source.exists())
        self.opening()
        worker = self.make_worker(DeliveryPolicy(events=('closed',)))
        await worker.tick(NOW)
        self.opening(NOW+70000)
        self.assertEqual((await worker.tick(NOW+72000))[0]['error'], 'event_filter')

    def test_config_state_separation_and_single_worker_lease(self):
        for value in (dict(min_net_usdt=float('nan')), dict(min_net_usdt=True), dict(max_attempts=True),
                dict(max_age_seconds=0), dict(events='opened'), dict(events=['suppressed']), dict(poll_seconds=.1)):
            with self.assertRaises(ValueError): DeliveryPolicy(**value)
        with self.assertRaises(ValueError): DeliveryWorker(self.source, self.source, self.sender)
        with SessionLease(self.state):
            with self.assertRaises(SessionInUse):
                with SessionLease(self.state): pass


class Response:
    def __init__(self, status, value): self.status, self.value = status, value
    async def __aenter__(self): return self
    async def __aexit__(self, *unused): pass
    async def json(self): return self.value


class FakeSession:
    def __init__(self, response=None, error=None): self.response, self.error, self.kwargs = response, error, None
    def post(self, url, **kwargs):
        self.kwargs = kwargs
        if self.error: raise self.error
        return self.response


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    async def test_https_json_request_and_no_redirects(self):
        session = FakeSession(Response(200, {'ok':True, 'result':{'message_id':1}}))
        sender = TelegramSender('12345:private_TEST_token', '-12345', session)
        await sender.send('plain <text>')
        self.assertEqual(session.kwargs['json']['text'], 'plain <text>')
        self.assertNotIn('parse_mode', session.kwargs['json'])
        self.assertFalse(session.kwargs['allow_redirects'])
        self.assertEqual(session.kwargs['timeout'].total, 10)

    async def test_errors_are_sanitized_and_retry_after_is_respected(self):
        for response, code, retry in ((Response(429, {'error_code':429,'parameters':{'retry_after':17}}), 'telegram_rate_limit', True),
                (Response(503, {}), 'telegram_unavailable', True), (Response(401, {'description':'SECRET'}), 'telegram_rejected', False)):
            sender = TelegramSender('12345:SECRET', '12345', FakeSession(response))
            with self.assertRaises(DeliveryError) as caught: await sender.send('test')
            self.assertEqual(str(caught.exception), code)
            self.assertEqual(caught.exception.retryable, retry)
            if code == 'telegram_rate_limit': self.assertEqual(caught.exception.retry_after, 17)
        sender = TelegramSender('12345:SECRET', '12345', FakeSession(error=aiohttp.ClientError('URL contains SECRET')))
        with self.assertRaises(DeliveryError) as caught: await sender.send('test')
        self.assertNotIn('SECRET', str(caught.exception))

    def test_invalid_credentials_and_long_plain_text(self):
        for token, chat in (('', '1'), ('123:bad/url', '1'), ('123:valid', 'bad recipient')):
            with self.assertRaises(ValueError): TelegramSender(token, chat, None)
        self.assertLessEqual(len(format_notice(dict(kind='opened',timestamp_ms=NOW,pair_id='x'*5000,event_id='1'))), 4000)
