"""Opt-in automatic marking of existing live paper positions. Never opens/closes."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .paper import PaperLedger, encode
from .paper_collection import read_positions
from .recovery import SessionInUse, SessionLease
from .scanner import Registry


def tick(database, snapshot, registry, now_ms=None):
    now = int(time.time()*1000) if now_ms is None else now_ms
    report = dict(classification='paper_watch_status', generated_at_ms=now, state='idle',
        positions=[], execution_verified=False, opens_positions=False, closes_positions=False)
    try:
        collection = read_positions(database, registry)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        return dict(report, state='ledger_error', error=str(exc))
    report['collection'] = {key: value for key, value in collection.items() if key != 'positions'}
    if not collection['positions']:
        if collection['state'] == 'partial':
            report['state'] = 'partial'
        return report
    try:
        document = json.loads(Path(snapshot).read_text(encoding='utf-8-sig'))
        stamp = document['generated_at_ms']
        if type(stamp) is not int or not -1000 <= now-stamp <= 5000:
            raise ValueError('snapshot_stale')
        digest = sha256(encode(document).encode()).hexdigest()
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return dict(report, state='snapshot_unavailable', error=str(exc))
    ledger = PaperLedger(database)
    for row in collection['positions']:
        item = dict(id=row['id'], pair_id=row['pair']['id'], last_valuation_ms=row['valuation']['timestamp_ms'])
        try:
            position = ledger.value(document, row['id'], registry, now_ms=now)
            report['positions'].append(dict(item, state='already_current' if row['valuation'].get('source_hash') == digest else 'marked',
                last_valuation_ms=position['valuation']['timestamp_ms']))
        except (ValueError, KeyError, TypeError, ArithmeticError, OSError, sqlite3.Error) as exc:
            report['positions'].append(dict(item, state='blocked', error=str(exc)))
    report['state'] = 'partial' if collection['state'] == 'partial' or any(row['state'] == 'blocked' for row in report['positions']) else 'ready'
    return report


def check_status_path(path, sources):
    path = Path(path).resolve()
    if path.suffix.lower() != '.json':
        raise ValueError('paper_watch_status_must_be_json')
    for source in sources:
        if source is None:
            continue
        source = Path(source).resolve()
        if path == source or path.exists() and source.exists() and path.samefile(source):
            raise ValueError('paper_watch_status_must_differ_from_inputs')
    if path.exists():
        existing = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(existing, dict) or existing.get('classification') != 'paper_watch_status':
            raise ValueError('refusing_to_overwrite_unrelated_status_file')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=Path('data/paper.sqlite3'))
    parser.add_argument('--snapshot', type=Path, default=Path('data/latest.json'))
    parser.add_argument('--status', type=Path, default=Path('data/paper-watch.json'))
    parser.add_argument('--registry', type=Path)
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    try:
        registry = Registry.load(args.registry)
        check_status_path(args.status, [args.database, args.snapshot, args.registry])
        with SessionLease(str(args.database.resolve())+'.paper-watch'):
            previous = None
            while True:
                report = tick(args.database, args.snapshot, registry)
                args.status.parent.mkdir(parents=True, exist_ok=True)
                temporary = args.status.with_name(args.status.name+'.'+uuid4().hex+'.tmp')
                # Never reuse a temporary file that might belong to another writer.
                with temporary.open('x', encoding='utf-8') as handle:
                    handle.write(encode(report))
                temporary.replace(args.status)
                summary = {key: value for key, value in report.items() if key != 'generated_at_ms'}
                if summary != previous:
                    print(json.dumps(report, ensure_ascii=False), flush=True)
                    previous = summary
                if not args.watch:
                    break
                time.sleep(2)
    except KeyboardInterrupt:
        pass
    except SessionInUse:
        parser.error('Another paper watcher is using this ledger')
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
