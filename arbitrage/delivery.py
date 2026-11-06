"""Independent delivery of local notices. Default: dry run, no network requests."""
import argparse
import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

import aiohttp

from .alerts import read_feed
from .recovery import SessionInUse, SessionLease


@dataclass(frozen=True)
class DeliveryPolicy:
    events: tuple = ('opened', 'closed')
    min_net_usdt: float = 0
    max_age_seconds: float = 60
    max_attempts: int = 5
    poll_seconds: float = 2

    def __post_init__(self):
        if (not isinstance(self.events, (list, tuple)) or not self.events
                or any(value not in ('opened', 'closed') for value in self.events)):
            raise ValueError('events must contain opened and/or closed')
        for key, low, high in (('min_net_usdt', 0, 1e9), ('max_age_seconds', 1, 3600), ('poll_seconds', 1, 60)):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'Invalid {key}: expected {low}–{high}')
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 10:
            raise ValueError('max_attempts must be 1–10')


class DeliveryError(Exception):
    """Only sanitized, locally generated error codes may be logged."""
    def __init__(self, code, retryable=False, retry_after=0):
        super().__init__(code)
        self.retryable, self.retry_after = retryable, retry_after


class TelegramSender:
    def __init__(self, token, chat_id, session):
        if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token or ''):
            raise ValueError('Set ARBITRAGE_TELEGRAM_BOT_TOKEN in the process environment')
        if not re.fullmatch(r'-?[0-9]+|@[A-Za-z0-9_]{5,}', chat_id or ''):
            raise ValueError('Set ARBITRAGE_TELEGRAM_CHAT_ID in the process environment')
        self.token, self.chat_id, self.session = token, chat_id, session
        self.identity = sha256((token.split(':')[0]+':'+chat_id).encode()).hexdigest()[:24]

    async def send(self, text):
        try:
            async with self.session.post('https://api.telegram.org/bot'+self.token+'/sendMessage',
                    json=dict(chat_id=self.chat_id, text=text, link_preview_options=dict(is_disabled=True)),
                    timeout=aiohttp.ClientTimeout(total=10), allow_redirects=False) as response:
                try:
                    value = await response.json()
                except (ValueError, aiohttp.ContentTypeError):
                    raise DeliveryError('telegram_invalid_response', retryable=True) from None
                if not isinstance(value, dict):
                    raise DeliveryError('telegram_invalid_response', retryable=True)
                if response.status == 200 and value.get('ok') is True:
                    return
                code = value.get('error_code', response.status)
                if code == 429:
                    parameters = value.get('parameters')
                    delay = parameters.get('retry_after', 30) if isinstance(parameters, dict) else 30
                    if type(delay) is not int or not 1 <= delay <= 86400:
                        delay = 30
                    raise DeliveryError('telegram_rate_limit', retryable=True, retry_after=delay)
                if response.status >= 500 or type(code) is int and code >= 500:
                    raise DeliveryError('telegram_unavailable', retryable=True)
                raise DeliveryError('telegram_rejected')
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # aiohttp exception messages can contain the token-bearing URL.
            raise DeliveryError('telegram_transport_error', retryable=True) from None


class DryRunSender:
    identity = 'dry-run'

    async def send(self, text):
        print(json.dumps(dict(mode='dry_run_no_network', text=text), ensure_ascii=False), flush=True)


def format_notice(row):
    title = 'Условный сценарий подтверждён' if row['kind'] == 'opened' else 'Условия сигнала потеряны'
    stamp = datetime.fromtimestamp(row['timestamp_ms']/1000, timezone.utc).isoformat(timespec='seconds')
    net = row.get('conditional_net_usdt')
    details = f'Условный net: {net:.4f} USDT\n' if row['kind'] == 'opened' and isinstance(net, (float, int)) and math.isfinite(net) else ''
    return (f'{title}\n{str(row["pair_id"])[:500]}\n{details}Событие: {stamp}\n'
            f'ID: {str(row["event_id"])[:200]}\nИстория monitor-only. Текущие условия могли измениться; исполнение не подтверждено.')


class DeliveryWorker:
    """One ordered persistent cursor per source/destination. Source is always read-only."""
    def __init__(self, database, state, sender, policy=None):
        self.database, self.state = Path(database).resolve(), Path(state).resolve()
        if self.database == self.state or self.state in [Path(str(self.database)+suffix) for suffix in ('-wal', '-shm', '.monitor.lock')]:
            raise ValueError('Delivery state must be separate from the market database')
        self.sender, self.policy = sender, policy or DeliveryPolicy()
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.state)
        self.db.execute('CREATE TABLE IF NOT EXISTS checkpoints(binding TEXT PRIMARY KEY,cursor INTEGER NOT NULL,next_send_ms INTEGER NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS receipts(binding TEXT NOT NULL,event_id TEXT NOT NULL,episode_id TEXT NOT NULL,kind TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL,next_ms INTEGER NOT NULL,updated_ms INTEGER NOT NULL,error TEXT,PRIMARY KEY(binding,event_id))')
        self.db.execute('CREATE INDEX IF NOT EXISTS receipts_episode ON receipts(binding,episode_id,kind,status)')
        self.db.commit()

    def close(self):
        self.db.close()

    def _record(self, binding, row, status, attempts, now, next_ms=0, error=None, advance=True):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?,?,?,?,?)',
                (binding, row['event_id'], row['episode_id'], row['kind'], status, attempts, next_ms, now, error))
            if advance:
                self.db.execute('UPDATE checkpoints SET cursor=? WHERE binding=?', (row['id'], binding))
        return dict(event_id=row['event_id'], status=status, attempts=attempts, error=error)

    def _has_closed(self, row):
        with closing(sqlite3.connect(self.database.as_uri()+'?mode=ro', uri=True)) as source:
            return bool(source.execute("SELECT 1 FROM local_alerts WHERE episode_id=? AND kind='closed' LIMIT 1", (row['episode_id'],)).fetchone())

    async def tick(self, now_ms=None):
        now = int(time.time()*1000) if now_ms is None else now_ms
        if not self.database.is_file():
            return [dict(status='waiting_for_source')]
        feed = read_feed(self.database, 1)
        binding = feed['scope']+':'+self.sender.identity
        checkpoint = self.db.execute('SELECT cursor,next_send_ms FROM checkpoints WHERE binding=?', (binding,)).fetchone()
        if checkpoint is None:
            with self.db:
                self.db.execute('INSERT INTO checkpoints VALUES (?,?,0)', (binding, feed['latest_id']))
            return [dict(status='armed_new_events_only', cursor=feed['latest_id'])]
        if feed['latest_id'] < checkpoint[0]:
            raise ValueError('Source cursor moved backwards; use a new delivery state file')
        batch = read_feed(self.database, 100, after_id=checkpoint[0])
        if batch['scope'] != feed['scope']:
            return [dict(status='source_changed_retry')]
        results = []
        for row in batch['rows']:
            previous = self.db.execute('SELECT status,attempts,next_ms FROM receipts WHERE binding=? AND event_id=?', (binding, row['event_id'])).fetchone()
            attempts = previous[1] if previous else 0
            age = now-row['timestamp_ms']
            net = row.get('conditional_net_usdt')
            reason = None
            if row['kind'] not in self.policy.events:
                reason = 'event_filter'
            elif age < -1000 or age > self.policy.max_age_seconds*1000:
                reason = 'expired'
            elif row['kind'] == 'opened':
                if not isinstance(net, (float, int)) or not math.isfinite(net) or net <= self.policy.min_net_usdt:
                    reason = 'net_filter'
                elif self._has_closed(row):
                    reason = 'episode_already_closed'
            elif not self.db.execute("SELECT 1 FROM receipts WHERE binding=? AND episode_id=? AND kind='opened' AND status='sent'", (binding, row['episode_id'])).fetchone():
                reason = 'opening_not_delivered'
            if reason:
                results.append(self._record(binding, row, 'skipped', attempts, now, error=reason))
                continue
            if attempts >= self.policy.max_attempts:
                results.append(self._record(binding, row, 'failed', attempts, now, error='attempt_limit'))
                continue
            if checkpoint[1] > now or previous and previous[2] > now:
                break
            attempts += 1
            # Persist BEFORE network I/O: crash/restart consumes an attempt and waits.
            self._record(binding, row, 'pending', attempts, now, now+30000, advance=False)
            try:
                await self.sender.send(format_notice(row))
            except DeliveryError as exc:
                retry = exc.retryable and attempts < self.policy.max_attempts
                delay = max(exc.retry_after, min(60, 2**attempts))
                with self.db:
                    self.db.execute('UPDATE checkpoints SET next_send_ms=? WHERE binding=?', (now+delay*1000, binding))
                results.append(self._record(binding, row, 'pending' if retry else 'failed', attempts,
                    now, now+delay*1000, str(exc), advance=not retry))
                break  # No flooding other notices when the destination is unavailable.
            else:
                results.append(self._record(binding, row, 'sent', attempts, now))
                break  # At most one outgoing message per poll.
        with self.db:
            # Keep 30 days of receipts; successful opens protect closing correlation.
            self.db.execute('DELETE FROM receipts WHERE updated_ms<? AND status!=?', (now-30*86400000, 'pending'))
        return results


async def run(args, policy):
    async with aiohttp.ClientSession() as session:
        sender = TelegramSender(os.environ.get('ARBITRAGE_TELEGRAM_BOT_TOKEN'),
            os.environ.get('ARBITRAGE_TELEGRAM_CHAT_ID'), session) if args.send else DryRunSender()
        if args.test:
            await sender.send('ТЕСТ Arbitrage Monitor. Проверка доставки; это не рыночный сигнал.')
            print(json.dumps(dict(status='test_sent' if args.send else 'test_dry_run')), flush=True)
            return
        with SessionLease(args.state):
            worker = DeliveryWorker(args.database, args.state, sender, policy)
            try:
                while True:
                    for result in await worker.tick():
                        print(json.dumps(dict(mode='telegram' if args.send else 'dry_run_no_network', **result), ensure_ascii=False), flush=True)
                    if not args.watch:
                        break
                    await asyncio.sleep(policy.poll_seconds)
            finally:
                worker.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=Path('data/market.sqlite3'))
    parser.add_argument('--state', type=Path, default=Path('data/delivery.sqlite3'))
    parser.add_argument('--config', type=Path)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--send', action='store_true', help='Enable Telegram using environment variables; default is dry run')
    parser.add_argument('--test', action='store_true', help='Test message only; does not consume the journal')
    args = parser.parse_args()
    try:
        settings = json.loads(args.config.read_text(encoding='utf-8-sig')) if args.config else {}
        if not isinstance(settings, dict) or set(settings)-set(DeliveryPolicy.__dataclass_fields__):
            raise ValueError('Unknown delivery configuration fields; credentials belong in environment variables')
        policy = DeliveryPolicy(**settings)
        asyncio.run(run(args, policy))
    except KeyboardInterrupt:
        pass
    except SessionInUse:
        parser.error('Another delivery worker is using this state file')
    except (ValueError, OSError, sqlite3.Error, DeliveryError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
