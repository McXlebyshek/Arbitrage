"""Manual, offline-capable paper ledger. No exchange connections or orders."""
import argparse
from contextlib import closing
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .economics import amount_step, check_limits, evaluate_pair, fee_rate, fill, normalize_book
from .scanner import Registry, decimal, positive


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def evidence(document, replay, now_ms=None):
    stamp = document['generated_at_ms']
    if type(stamp) is not int or stamp <= 0:
        raise ValueError('invalid_snapshot_time')
    now = int(time.time()*1000) if now_ms is None else now_ms
    if not replay and not -1000 <= now-stamp <= 5000:
        raise ValueError('snapshot_stale_use_explicit_replay_for_history')
    economics = document.get('economics') or {}
    if economics.get('state') in (None, 'stopped') or economics.get('generated_at_ms') != stamp:
        raise ValueError('economic_snapshot_unavailable')
    legs = {}
    for item in economics.get('inputs', []):
        key = item['exchange'], item['native_id']
        if key in legs:
            raise ValueError('duplicate_paper_input')
        legs[key] = item
    return stamp, legs, sha256(encode(document).encode()).hexdigest(), stamp if replay else now


def identity(pair, registry):
    result = {}
    for side in ('long', 'short'):
        key = pair[f'{side}_exchange'], pair[f'{side}_symbol']
        binding = registry.bindings[key]
        if binding['asset'] != pair['asset']:
            raise ValueError('paper_asset_mismatch')
        result[side] = {key: str(value) for key, value in binding.items()}
    if pair['long_exchange'] == pair['short_exchange']:
        raise ValueError('paper_requires_two_exchanges')
    return result


def exit_value(position, legs, registry, stamp, policy, validation_ms=None):
    pair, quantity = position['pair'], positive(position['base_quantity'])
    if identity(pair, registry) != position['bindings']:
        raise ValueError('paper_instrument_identity_changed')
    fills, times, fees = {}, {}, Decimal(0)
    for side, book_side in (('long', 'bids'), ('short', 'asks')):
        key = pair[f'{side}_exchange'], pair[f'{side}_symbol']
        if key not in legs:
            raise ValueError('paper_awaiting_depth')
        leg = legs[key]
        if leg['market']['symbol'] != leg['book'].get('symbol'):
            raise ValueError('depth_symbol_mismatch')
        book = normalize_book(leg['book'], registry.bindings[key], leg['market'], leg['received_at_ms'], stamp if validation_ms is None else validation_ms)
        contracts = quantity/book['units_per_contract']
        step = amount_step(leg['market'], leg['precision_mode'])
        if contracts % step:
            raise ValueError('paper_quantity_step_changed')
        price, value = fill(book[book_side], quantity)
        check_limits(leg['market'], contracts, value)
        impact = 1-price/book[book_side][0][0] if side == 'long' else price/book[book_side][0][0]-1
        if impact > decimal(policy['max_impact_pct'])/100:
            raise ValueError('price_impact_exceeded')
        rate, source = fee_rate(key[0], leg['market'], policy['fee_overrides'])
        fee = value*rate
        fees += fee
        fills[side] = dict(price=str(price), value=str(value), fee_usdt=str(fee), fee_rate=str(rate), fee_source=source)
        times[side] = book['source_timestamp_ms']
        previous_times = position.get('valuation', {}).get('depth_source_times', position['entry_depth_source_times'])
        if times[side] < previous_times[side]:
            raise ValueError('paper_depth_time_reversed')
    if abs(times['long']-times['short']) > policy['max_skew_ms']:
        raise ValueError('depth_time_skew')
    entry = position['entry_fills']
    gross = decimal(entry['short']['value'])-decimal(entry['long']['value'])+decimal(fills['long']['value'])-decimal(fills['short']['value'])
    entry_fees = decimal(position['entry_fees_usdt'])
    net = gross-entry_fees-fees-decimal(position['extra_cost_usdt'])
    return dict(timestamp_ms=stamp, fills=fills, depth_source_times=times,
        price_pnl_usdt=str(gross), entry_fees_usdt=str(entry_fees), exit_fees_usdt=str(fees),
        pnl_before_funding_usdt=str(net), funding_usdt=None, net_pnl_usdt=None,
        funding_status='not_accounted', classification='paper_simulation', execution_verified=False)


class PaperLedger:
    def __init__(self, database, replay=False):
        self.path, self.replay = Path(database), replay

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables-set(('paper_meta', 'paper_positions', 'paper_events', 'sqlite_sequence')):
            db.close()
            raise ValueError('paper_requires_dedicated_database')
        db.execute('CREATE TABLE IF NOT EXISTS paper_meta(mode TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS paper_positions(id TEXT PRIMARY KEY,pair_id TEXT NOT NULL,state TEXT NOT NULL,reserved_usdt TEXT NOT NULL,episode_id TEXT NOT NULL UNIQUE,source_hash TEXT NOT NULL,payload TEXT NOT NULL,UNIQUE(source_hash,pair_id))')
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS paper_one_pair ON paper_positions(pair_id) WHERE state='open'")
        db.execute('CREATE TABLE IF NOT EXISTS paper_events(id INTEGER PRIMARY KEY AUTOINCREMENT,position_id TEXT NOT NULL,kind TEXT NOT NULL,source_hash TEXT NOT NULL,payload TEXT NOT NULL,UNIQUE(position_id,kind,source_hash))')
        db.commit()
        return db

    def _mode(self, db):
        mode = 'replay' if self.replay else 'live_snapshot'
        known = db.execute('SELECT mode FROM paper_meta').fetchone()
        if known and known[0] != mode:
            raise ValueError('paper_live_and_replay_require_separate_ledgers')
        if not known:
            db.execute('INSERT INTO paper_meta VALUES (?)', (mode,))

    def open(self, document, pair_id, registry, now_ms=None):
        stamp, legs, digest, clock = evidence(document, self.replay, now_ms)
        economics, signals = document['economics'], document.get('signals') or {}
        pair = next((row for row in economics.get('rows', []) if row['id'] == pair_id), None)
        signal = next((row for row in signals.get('rows', []) if row['id'] == pair_id), None)
        count = signals.get('policy', {}).get('confirmations', 3)
        if (not pair or not signal or signals.get('state') != 'monitoring'
                or signals.get('generated_at_ms') != stamp or signal.get('state') != 'confirmed'
                or count < 2 or signal.get('confirmations', 0) < count
                or signal.get('last_sources') != pair.get('depth_source_times')):
            raise ValueError('paper_requires_current_confirmed_signal')
        policy = economics['policy']
        result = evaluate_pair(pair, legs, registry, stamp, policy['capital_usdt'], policy['hold_hours'],
            decimal(policy['max_impact_pct'])/100, decimal(policy['min_budget_utilization']),
            policy['extra_cost_usdt'], policy['fee_overrides'], policy['max_skew_ms'], policy['min_volume_usdt'])
        if (result['status'] != 'scenario_estimated' or result['reasons']
                or decimal(result['convergence_net_scenario_usdt']) <= max(Decimal(0), decimal(signals['policy'].get('min_usdt', 0)))
                or decimal(signal['conditional_net_usdt']) != decimal(result['convergence_net_scenario_usdt'])):
            raise ValueError('paper_entry_recalculation_failed')
        quantity = positive(result['base_quantity'])
        entries, fees = {}, Decimal(0)
        for side in ('long', 'short'):
            item = result['fills'][side+'_entry']
            rate = decimal(result['fee_rates'][side])
            value = decimal(item['value'])
            fees += value*rate
            entries[side] = dict(price=str(decimal(item['price'])), value=str(value), fee_rate=str(rate),
                contracts=str(decimal(result[side+'_contracts'])), fee_source=result['fee_sources'][side])
        reserved = sum((decimal(row['value']) for row in entries.values()), Decimal(0))+fees+decimal(policy['extra_cost_usdt'])
        position = dict(id=str(uuid4()), pair={key: pair[key] for key in ('id', 'asset', 'long_exchange', 'short_exchange', 'long_symbol', 'short_symbol')},
            state='open', mode='replay' if self.replay else 'live_snapshot', opened_at_ms=stamp,
            base_quantity=str(quantity), entry_fills=entries, entry_fees_usdt=str(fees), extra_cost_usdt=str(decimal(policy['extra_cost_usdt'])),
            reserved_usdt=str(reserved), bindings=identity(pair, registry), entry_depth_source_times=result['depth_source_times'],
            source_hash=digest, episode_id=signal['episode_id'], policy=policy, entry_evidence=dict(pair=pair, inputs=list(legs.values()), signals=signals),
            classification='paper_simulation', execution_verified=False)
        position['valuation'] = exit_value(position, legs, registry, stamp, policy, clock)
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._mode(db)
            repeated = db.execute('SELECT payload FROM paper_positions WHERE source_hash=? AND pair_id=?', (digest, pair_id)).fetchone()
            if repeated:
                return json.loads(repeated[0])
            if db.execute('SELECT 1 FROM paper_positions WHERE episode_id=?', (signal['episode_id'],)).fetchone():
                raise ValueError('paper_episode_already_used')
            existing = db.execute("SELECT payload FROM paper_positions WHERE pair_id=? AND state='open'", (pair_id,)).fetchone()
            if existing:
                old = json.loads(existing[0])
                if old['source_hash'] == digest:
                    return old
                raise ValueError('paper_pair_already_open')
            last = db.execute("SELECT max(json_extract(payload,'$.valuation.timestamp_ms')) FROM paper_positions WHERE pair_id=?", (pair_id,)).fetchone()[0]
            if last is not None and stamp <= last:
                raise ValueError('paper_reentry_requires_later_snapshot')
            used = sum((decimal(row[0]) for row in db.execute("SELECT reserved_usdt FROM paper_positions WHERE state='open'")), Decimal(0))
            if used+reserved > 1000:
                raise ValueError('paper_portfolio_budget_exceeded')
            db.execute('INSERT INTO paper_positions VALUES (?,?,?,?,?,?,?)', (position['id'], pair_id, 'open', str(reserved), signal['episode_id'], digest, encode(position)))
            db.execute('INSERT INTO paper_events(position_id,kind,source_hash,payload) VALUES (?,?,?,?)', (position['id'], 'opened', digest, encode(position)))
        return position

    def value(self, document, position_id, registry, close=False, now_ms=None):
        stamp, legs, digest, clock = evidence(document, self.replay, now_ms)
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._mode(db)
            row = db.execute('SELECT payload FROM paper_positions WHERE id=?', (position_id,)).fetchone()
            if not row:
                raise ValueError('paper_position_not_found')
            position = json.loads(row[0])
            if position['state'] == 'closed':
                if close: return position
                raise ValueError('paper_position_already_closed')
            if stamp < position['valuation']['timestamp_ms']:
                raise ValueError('paper_valuation_time_reversed')
            valuation = exit_value(position, legs, registry, stamp, position['policy'], clock)
            if close and (stamp <= position['opened_at_ms'] or any(valuation['depth_source_times'][side] <= position['entry_depth_source_times'][side] for side in ('long', 'short'))):
                raise ValueError('paper_close_requires_later_depth')
            kind = 'closed' if close else 'marked'
            if db.execute('SELECT 1 FROM paper_events WHERE position_id=? AND kind=? AND source_hash=?', (position_id, kind, digest)).fetchone():
                return position
            position['valuation'] = valuation
            position.pop('funding', None)
            if close:
                position.update(state='closed', closed_at_ms=stamp)
            db.execute('UPDATE paper_positions SET state=?,payload=? WHERE id=?', (position['state'], encode(position), position_id))
            db.execute('INSERT INTO paper_events(position_id,kind,source_hash,payload) VALUES (?,?,?,?)',
                (position_id, kind, digest, encode(dict(valuation=valuation, inputs=list(legs.values())))))
        return position

    def funding(self, position_id, market_database, mark_prices=None, now_ms=None):
        from .paper_funding import reconcile
        source = Path(market_database).resolve()
        if self.path.resolve() == source or self.path.exists() and source.exists() and self.path.samefile(source):
            raise ValueError('paper_and_market_databases_must_differ')
        if not self.path.is_file():
            raise ValueError('paper_position_not_found')
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._mode(db)
            row = db.execute('SELECT payload FROM paper_positions WHERE id=?', (position_id,)).fetchone()
            if not row:
                raise ValueError('paper_position_not_found')
            position = json.loads(row[0])
            result = reconcile(position, source, mark_prices, now_ms)
            if position.get('funding', {}).get('evidence_hash') == result['evidence_hash']:
                return position
            result['reconciled_at_ms'] = int(time.time()*1000) if now_ms is None else now_ms
            position['funding'] = result
            valuation = position['valuation']
            valuation.update(funding_status=result['status'], funding_usdt=result['funding_usdt'],
                net_pnl_usdt=str(decimal(valuation['pnl_before_funding_usdt'])+decimal(result['funding_usdt'])) if result['complete'] else None,
                net_entry_notional_estimate_usdt=str(decimal(valuation['pnl_before_funding_usdt'])+decimal(result['entry_notional_estimate_usdt'])) if result['rates_covered'] else None)
            db.execute('UPDATE paper_positions SET payload=? WHERE id=?', (encode(position), position_id))
            db.execute('INSERT INTO paper_events(position_id,kind,source_hash,payload) VALUES (?,?,?,?)',
                (position_id, 'funding_reconciled', str(uuid4()), encode(result)))
        return position

    def report(self):
        positions = []
        if self.path.is_file():
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='paper_positions'").fetchone():
                    positions = [json.loads(row[0]) for row in db.execute('SELECT payload FROM paper_positions ORDER BY rowid')]
        closed = [row for row in positions if row['state'] == 'closed']
        covered = [row for row in closed if row['valuation'].get('net_pnl_usdt') is not None]
        return dict(classification='paper_simulation', execution_verified=False, positions=positions,
            open_count=len(positions)-len(closed), closed_count=len(closed),
            closed_pnl_before_funding_usdt=str(sum((decimal(row['valuation']['pnl_before_funding_usdt']) for row in closed), Decimal(0))),
            funding_usdt=str(sum((decimal(row['valuation']['funding_usdt']) for row in covered), Decimal(0))) if closed and len(covered) == len(closed) else None,
            net_pnl_usdt=str(sum((decimal(row['valuation']['net_pnl_usdt']) for row in covered), Decimal(0))) if closed and len(covered) == len(closed) else None,
            funding_covered_closed_count=len(covered),
            funding_status='covered_paper_cashflows' if closed and len(covered) == len(closed) else 'partial_or_not_accounted')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('open', 'mark', 'close', 'status', 'funding'))
    parser.add_argument('--database', type=Path, default=Path('data/paper.sqlite3'))
    parser.add_argument('--snapshot', type=Path, default=Path('data/latest.json'))
    parser.add_argument('--registry', type=Path)
    parser.add_argument('--pair')
    parser.add_argument('--position')
    parser.add_argument('--market-database', type=Path, default=Path('data/market.sqlite3'))
    parser.add_argument('--funding-prices', type=Path, help='Optional supplied settlement mark prices; native price units, JSON list')
    parser.add_argument('--replay', action='store_true', help='Explicit historical simulation; use a separate ledger')
    args = parser.parse_args()
    try:
        ledger = PaperLedger(args.database, args.replay)
        if args.action == 'status':
            result = ledger.report()
        elif args.action == 'funding':
            if not args.position: raise ValueError('--position is required')
            prices = json.loads(args.funding_prices.read_text(encoding='utf-8-sig')) if args.funding_prices else None
            result = ledger.funding(args.position, args.market_database, prices)
        else:
            if args.database.resolve() == args.snapshot.resolve():
                raise ValueError('paper_database_must_differ_from_snapshot')
            document = json.loads(args.snapshot.read_text(encoding='utf-8-sig'))
            registry = Registry.load(args.registry)
            if args.action == 'open':
                if not args.pair: raise ValueError('--pair is required')
                result = ledger.open(document, args.pair, registry)
            else:
                if not args.position: raise ValueError('--position is required')
                result = ledger.value(document, args.position, registry, close=args.action == 'close')
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (ValueError, KeyError, TypeError, ArithmeticError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
