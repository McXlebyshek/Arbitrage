"""Compare observed public funding events over a shared window, never account PnL."""
from decimal import Decimal
import json
from .funding_history import DAY, window_stats


def compare(db, legs, end, days):
    if days not in (1, 3, 7, 14, 28, 30):
        raise ValueError('Unsupported funding window')
    start = end-days*DAY
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='funding_history_events' AND type='table'").fetchone()
    imports = db.execute("SELECT 1 FROM sqlite_master WHERE name='funding_history_imports' AND type='table'").fetchone()
    series, coverage, limited = {}, {}, False
    for side, leg in legs.items():
        key = leg['exchange'], leg['native_id']
        values = db.execute('SELECT timestamp_ms,rate FROM funding_history_events WHERE exchange=? AND native_id=? AND timestamp_ms>? AND timestamp_ms<=? ORDER BY timestamp_ms DESC LIMIT 1001', (*key, start, end)).fetchall() if exists else []
        truncated = len(values) > 1000
        limited |= truncated
        series[side] = {stamp: Decimal(rate) for stamp, rate in reversed(values[:1000])}
        anchor = db.execute('SELECT timestamp_ms,rate FROM funding_history_events WHERE exchange=? AND native_id=? AND timestamp_ms<=? ORDER BY timestamp_ms DESC LIMIT 1', (*key, start)).fetchone() if exists else None
        context = ([dict(timestamp=anchor[0], rate=anchor[1])] if anchor else [])+[dict(timestamp=t, rate=str(r)) for t, r in series[side].items()]
        stats = window_stats(context, end, days)
        # An incomplete/failed import must not become complete just because its few rows look regular.
        report = db.execute('SELECT payload FROM funding_history_imports WHERE exchange=? AND native_id=?', key).fetchone() if imports else None
        import_error = bool(report and json.loads(report[0]).get('error'))
        covered = stats['coverage'] == 'regular_observed_coverage' and not truncated and not import_error
        coverage[side] = dict(stats, covered=covered, truncated=truncated, import_error=import_error)
    sums = dict(long=Decimal(0), short=Decimal(0))
    seen, rows = set(), []
    for stamp in sorted(set(series['long']) | set(series['short'])):
        value = dict(time=stamp)
        for side in ('long', 'short'):
            rate = series[side].get(stamp)
            if rate is not None:
                sums[side] += rate*100
                seen.add(side)
            value[side+'_rate_pct'] = float(rate*100) if rate is not None else None
            value[side+'_sum_pct'] = float(sums[side]) if side in seen else None
        value['observed_net_pct'] = float(sums['short']-sums['long']) if len(seen) == 2 else None
        rows.append(value)
    totals = {side: float(sums[side]) if side in seen else None for side in sums}
    observed = float(sums['short']-sums['long']) if len(seen) == 2 else None
    return dict(days=days, start_exclusive_ms=start, end_inclusive_ms=end, rows=rows, coverage=coverage,
                totals_pct=totals, observed_net_pct=observed,
                covered_net_pct=observed if all(x['covered'] for x in coverage.values()) else None,
                truncated=limited, max_events_per_leg=1000,
                interpretation='public_rate_sums_equal_illustrative_notional_not_account_cashflows')
