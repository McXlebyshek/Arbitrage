"""Reconcile settled public funding rates, never forecasts or account cashflows."""
from contextlib import closing
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time

from .scanner import decimal, positive

MAX_EVENTS = 2000


def price_index(rows):
    if not isinstance(rows, list):
        raise ValueError('funding_prices_must_be_a_list')
    result = {}
    for row in rows:
        stamp = row['timestamp_ms']
        if type(stamp) is not int or stamp <= 0:
            raise ValueError('invalid_funding_price_timestamp')
        key = row['exchange'], row['native_id'], stamp
        source = row.get('source')
        if not isinstance(source, str) or not source.strip() or len(source) > 500:
            raise ValueError('funding_price_requires_source')
        item = dict(mark_price_native=str(positive(row['mark_price_native'])), source=source)
        if key in result and result[key] != item:
            raise ValueError('conflicting_funding_mark_prices')
        result[key] = item
    return result


def read_leg(db, tables, key, asset, start, end, now):
    reasons, events, anchors, imported = [], [], [], None
    if 'funding_history_events' not in tables or 'funding_history_imports' not in tables:
        return dict(events=[], anchors=[], import_end_ms=None, covered=False, reasons=['funding_archive_missing'])
    rows = db.execute('SELECT timestamp_ms,rate,asset,observed_at_ms FROM funding_history_events WHERE exchange=? AND native_id=? AND timestamp_ms>? AND timestamp_ms<=? ORDER BY timestamp_ms LIMIT ?', (*key, start, end, MAX_EVENTS+1)).fetchall()
    before = db.execute('SELECT timestamp_ms,rate,asset,observed_at_ms FROM funding_history_events WHERE exchange=? AND native_id=? AND timestamp_ms<=? ORDER BY timestamp_ms DESC LIMIT 3', (*key, start)).fetchall()[::-1]
    truncated = len(rows) > MAX_EVENTS
    if truncated:
        reasons.append('funding_event_limit')
    for source_rows, target in ((rows[:MAX_EVENTS], events), (before, anchors)):
        for stamp, rate, event_asset, observed in source_rows:
            if type(stamp) is not int or type(observed) is not int or stamp > now+1000 or observed > now+1000 or observed < stamp:
                reasons.append('funding_event_time_invalid')
                continue
            if event_asset != asset:
                reasons.append('funding_asset_mismatch')
                continue
            target.append(dict(timestamp_ms=stamp, rate=str(decimal(rate)), observed_at_ms=observed))
    report = db.execute('SELECT end_ms,payload FROM funding_history_imports WHERE exchange=? AND native_id=?', key).fetchone()
    if not report:
        reasons.append('funding_import_missing')
    else:
        imported, payload = report[0], json.loads(report[1])
        if (payload.get('exchange'), payload.get('native_id'), payload.get('asset')) != (*key, asset):
            reasons.append('funding_import_identity_mismatch')
        if payload.get('error'):
            reasons.append('funding_import_failed')
        if type(imported) is not int or imported > now+1000 or imported < end or payload.get('end_ms') != imported:
            reasons.append('funding_import_does_not_cover_valuation')
    context = anchors+events
    interval = None
    if len(anchors) < 3:
        reasons.append('funding_left_context_missing')
    else:
        gaps = [b['timestamp_ms']-a['timestamp_ms'] for a, b in zip(context, context[1:])]
        if gaps and 60000 <= gaps[0] <= 86400000 and all(gap == gaps[0] for gap in gaps):
            interval = gaps[0]
            if context[-1]['timestamp_ms']+interval <= end:
                reasons.append('funding_tail_missing')
        else:
            reasons.append('funding_cadence_irregular_or_gap')
    return dict(events=events, anchors=anchors, import_end_ms=imported, interval_ms=interval,
        covered=not reasons, reasons=sorted(set(reasons)), truncated=truncated)


def reconcile(position, database, mark_prices=None, now_ms=None):
    now = int(time.time()*1000) if now_ms is None else now_ms
    start, end = position['opened_at_ms'], position['valuation']['timestamp_ms']
    if end < start or end > now+1000:
        raise ValueError('invalid_paper_funding_window')
    prices = price_index([] if mark_prices is None else mark_prices)
    path = Path(database)
    coverage = {}
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
            db.execute('BEGIN')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for side in ('long', 'short'):
                key = position['pair'][side+'_exchange'], position['pair'][side+'_symbol']
                coverage[side] = read_leg(db, tables, key, position['pair']['asset'], start, end, now)
    else:
        coverage = {side: dict(events=[], anchors=[], covered=False, reasons=['funding_archive_missing'], import_end_ms=None) for side in ('long', 'short')}
    events, observed, entry_estimate = [], Decimal(0), Decimal(0)
    missing = []
    quantity = positive(position['base_quantity'])
    for side in ('long', 'short'):
        exchange, native = position['pair'][side+'_exchange'], position['pair'][side+'_symbol']
        units = positive(position['bindings'][side]['price_units'])
        sign = -1 if side == 'long' else 1
        for row in coverage[side]['events']:
            key = exchange, native, row['timestamp_ms']
            price = prices.get(key)
            rate = decimal(row['rate'])
            estimate = quantity*decimal(position['entry_fills'][side]['price'])*rate*sign
            cashflow, reference = None, None
            if price:
                reference = positive(price['mark_price_native'])/units
                cashflow = quantity*reference*rate*sign
                observed += cashflow
            else:
                missing.append(dict(exchange=exchange, native_id=native, timestamp_ms=row['timestamp_ms']))
            entry_estimate += estimate
            events.append(dict(side=side, exchange=exchange, native_id=native, **row,
                mark_price_native=price['mark_price_native'] if price else None,
                mark_price_per_base_usdt=str(reference) if reference is not None else None,
                mark_price_source=price['source'] if price else None,
                paper_cashflow_usdt=str(cashflow) if cashflow is not None else None,
                entry_notional_estimate_usdt=str(estimate)))
    events.sort(key=lambda row: (row['timestamp_ms'], row['side']))
    rates_covered = all(row['covered'] for row in coverage.values())
    complete = rates_covered and not missing
    result = dict(start_exclusive_ms=start, end_inclusive_ms=end, coverage=coverage, events=events,
        missing_mark_prices=missing, rates_covered=rates_covered, complete=complete,
        status='covered_paper_cashflows' if complete else 'rates_covered_marks_missing' if rates_covered else 'partial_history',
        observed_priced_cashflow_usdt=str(observed),
        observed_entry_notional_estimate_usdt=str(entry_estimate),
        entry_notional_estimate_usdt=str(entry_estimate) if rates_covered else None,
        funding_usdt=str(observed) if complete else None,
        interpretation='paper_cashflows_from_observed_public_rates_and_supplied_settlement_marks_not_account_payments',
        price_evidence_verified=False, execution_verified=False,
        boundary_rule='opened_at_excluded_valuation_or_close_included',
        coverage_rule='three_left_anchors_regular_observed_cadence_and_successful_import_through_end')
    result['evidence_hash'] = sha256(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return result
