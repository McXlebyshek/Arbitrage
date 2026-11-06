"""Read bounded live paper positions without creating or modifying a ledger."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .paper import identity

MAX_POSITIONS = 10


def read_positions(database, registry, allowed_exchanges=None):
    result = dict(state='absent', positions=[], skipped=[], open_count=0, limit=MAX_POSITIONS)
    if database is None:
        return dict(result, state='disabled')
    path = Path(database)
    if not path.is_file():
        return result
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=.2)) as db:
        db.execute('BEGIN')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'paper_meta', 'paper_positions'} <= tables:
            return dict(result, state='unsupported_ledger')
        mode = db.execute('SELECT mode FROM paper_meta').fetchone()
        if not mode or mode[0] != 'live_snapshot':
            return dict(result, state='ignored_replay_or_empty')
        result['open_count'] = db.execute("SELECT count(*) FROM paper_positions WHERE state='open'").fetchone()[0]
        rows = db.execute("SELECT payload FROM paper_positions WHERE state='open' ORDER BY rowid LIMIT ?", (MAX_POSITIONS,)).fetchall()
        for (payload,) in rows:
            position = json.loads(payload)
            if not isinstance(position, dict):
                result['skipped'].append(dict(id=None, reason='paper_position_payload_invalid'))
                continue
            try:
                pair = position['pair']
                valuation = position['valuation']
                if (not isinstance(position['id'], str) or not position['id'] or not isinstance(valuation, dict)
                        or type(valuation.get('timestamp_ms')) is not int or valuation['timestamp_ms'] <= 0):
                    raise ValueError('paper_position_payload_invalid')
                if position['mode'] != 'live_snapshot' or position['state'] != 'open':
                    raise ValueError('paper_position_not_live_open')
                if allowed_exchanges is not None and any(pair[side+'_exchange'] not in allowed_exchanges for side in ('long','short')):
                    raise ValueError('paper_exchange_not_enabled')
                if identity(pair, registry) != position['bindings']:
                    raise ValueError('paper_registry_identity_changed')
                result['positions'].append({key: position[key] for key in ('id','pair','valuation')})
            except (ValueError, KeyError, TypeError) as exc:
                result['skipped'].append(dict(id=position.get('id'), reason=str(exc)))
    result['omitted_by_limit'] = max(0, result['open_count']-MAX_POSITIONS)
    result['state'] = 'partial' if result['skipped'] or result['omitted_by_limit'] else 'ready'
    return result
