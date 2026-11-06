"""Bounded read-only projection of the paper ledger for the local dashboard."""
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import time

from .scanner import decimal
from .paper_statistics import closed_statistics


def read_dashboard(database, state='open', limit=25, offset=0, now_ms=None):
    if state not in ('open','closed','all') or type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 1000000:
        raise ValueError('invalid_paper_page')
    now = int(time.time()*1000) if now_ms is None else now_ms
    result = dict(classification='paper_dashboard', execution_verified=False, generated_at_ms=now,
        source='absent', mode=None, rows=[], filter=state, limit=limit, offset=offset, total_filtered=0, has_next=False,
        summary=dict(open_count=0,closed_count=0,reserved_usdt='0',closed_before_funding_usdt='0',
            covered_closed_count=0,closed_net_usdt=None,covered_wins=0,covered_losses=0,covered_flat=0))
    path = Path(database)
    result['statistics']=closed_statistics([],0)
    if not path.is_file(): return result
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=.2)) as db:
        db.execute('BEGIN')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'paper_meta','paper_positions'} <= tables:
            return dict(result, source='unsupported')
        mode = db.execute('SELECT mode FROM paper_meta').fetchone()
        result.update(source='saved_ledger',mode=mode[0] if mode else None)
        summary = result['summary']
        reserve, before, net = Decimal(0), Decimal(0), Decimal(0)
        for status, reserved, pnl, final in db.execute("SELECT state,reserved_usdt,json_extract(payload,'$.valuation.pnl_before_funding_usdt'),json_extract(payload,'$.valuation.net_pnl_usdt') FROM paper_positions"):
            if status == 'open':
                summary['open_count'] += 1
                reserve += decimal(reserved)
            elif status == 'closed':
                summary['closed_count'] += 1
                before += decimal(pnl)
                if final is not None:
                    value = decimal(final)
                    net += value
                    summary['covered_closed_count'] += 1
                    summary['covered_wins' if value > 0 else 'covered_losses' if value < 0 else 'covered_flat'] += 1
            else:
                raise ValueError('invalid_paper_position_state')
        summary.update(reserved_usdt=str(reserve),closed_before_funding_usdt=str(before),
            closed_net_usdt=str(net) if summary['closed_count'] and summary['covered_closed_count'] == summary['closed_count'] else None)
        closed_rows=db.execute("SELECT coalesce(json_extract(payload,'$.closed_at_ms'),json_extract(payload,'$.valuation.timestamp_ms')),json_extract(payload,'$.opened_at_ms'),json_extract(payload,'$.valuation.pnl_before_funding_usdt'),json_extract(payload,'$.valuation.net_pnl_usdt') FROM paper_positions WHERE state='closed' ORDER BY 1,rowid")
        result['statistics']=closed_statistics(closed_rows,summary['closed_count'])
        where, params = ('', []) if state == 'all' else ('WHERE state=?', [state])
        total = db.execute('SELECT count(*) FROM paper_positions '+where, params).fetchone()[0]
        if offset >= total: offset = 0
        result.update(total_filtered=total,offset=offset,has_next=offset+limit<total)
        # SQLite projects only display fields; raw books, signals and funding-event arrays stay local.
        fields = "json_object('id',id,'state',state,'reserved_usdt',reserved_usdt,'pair',json_extract(payload,'$.pair'),'mode',json_extract(payload,'$.mode'),'base_quantity',json_extract(payload,'$.base_quantity'),'opened_at_ms',json_extract(payload,'$.opened_at_ms'),'closed_at_ms',json_extract(payload,'$.closed_at_ms'),'valuation',json_extract(payload,'$.valuation'),'funding_status',json_extract(payload,'$.funding.status'))"
        for (payload,) in db.execute('SELECT '+fields+' FROM paper_positions '+where+' ORDER BY rowid DESC LIMIT ? OFFSET ?', [*params,limit,offset]):
            row = json.loads(payload)
            valuation = row['valuation']
            stamp = valuation['timestamp_ms']
            times = valuation.get('depth_source_times') or {}
            if not isinstance(times,dict): times = {}
            row['valuation_age_ms'] = now-stamp
            row['valuation_fresh'] = (row['state'] == 'open' and row['mode'] == 'live_snapshot' and -1000 <= now-stamp <= 5000
                and len(times) == 2 and all(type(value) is int and -1000 <= now-value <= 3000 for value in times.values()))
            row['fresh_until_ms'] = min(stamp+5000,*(value+3000 for value in times.values())) if row['valuation_fresh'] else 0
            result['rows'].append(row)
    return result
