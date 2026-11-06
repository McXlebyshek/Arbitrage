"""Artificial wall-clock CLI rules check. Offline; never a market result."""
import argparse
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from arbitrage.atomic_json import write_json
from arbitrage.paper import PaperLedger
from arbitrage.recovery import SessionLease


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    root = args.output.resolve()
    sys.path.insert(0, str(Path(__file__).parent/'tests'))
    from test_paper import document, later
    from test_economics import NOW
    source, registry = document()
    label = 'ARTIFICIAL_OFFLINE_RULES_CHECK_NOT_MARKET_RESULT'
    offset = int(time.time()*1000)-NOW

    def shift(value):
        if isinstance(value, dict): return {key: shift(item) for key, item in value.items()}
        if isinstance(value, list): return [shift(item) for item in value]
        if type(value) is int and value >= NOW-10000: return value+offset
        return value

    snapshot, database, status, registry_path = (root/name for name in ('snapshot.json','paper.sqlite3','rules.json','registry.json'))
    write_json(registry_path, json.loads(json.dumps(dict(version=label, bindings=list(registry.bindings.values())), default=str)))
    write_json(snapshot, dict(shift(source), validation_source=label))
    command = [sys.executable, '-m', 'arbitrage.paper_rules', '--database', str(database),
        '--snapshot', str(snapshot), '--status', str(status), '--registry', str(registry_path)]

    def run(apply=False):
        result = subprocess.run(command+(['--apply'] if apply else []), capture_output=True, text=True, check=True, timeout=15)
        return json.loads(result.stdout)

    preview = run()
    assert preview['decisions'][0]['action'] == 'entry_candidate' and not database.exists()
    opened = run(True)
    assert opened['decisions'][0]['action'] == 'opened'
    repeated = run(True)
    assert PaperLedger(database).report()['open_count'] == 1
    with SessionLease(str(database.resolve())+'.paper-rules'):
        duplicate = subprocess.run(command, capture_output=True, text=True, timeout=15)
        assert duplicate.returncode != 0 and 'Another paper rules' in duplicate.stderr
    time.sleep(1.05)
    write_json(snapshot, dict(shift(later(source)), validation_source=label))
    exit_preview = run()
    assert exit_preview['decisions'][0]['action'] == 'would_close'
    assert PaperLedger(database).report()['open_count'] == 1
    closed = run(True)
    assert closed['decisions'][0]['action'] == 'closed'
    run(True)
    ledger = PaperLedger(database).report()
    expected = Decimal('4.944')*Decimal('1.8')-Decimal('0.998688')
    assert ledger['closed_count'] == 1 and ledger['open_count'] == 0
    assert Decimal(ledger['closed_pnl_before_funding_usdt']) == expected
    assert ledger['net_pnl_usdt'] is None
    with closing(sqlite3.connect(database)) as db:
        integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
        counts = dict(db.execute('SELECT kind,count(*) FROM paper_events GROUP BY kind'))
    assert integrity == 'ok' and counts == dict(opened=1, closed=1)
    report = dict(classification=label, passed=True, no_network=True, real_orders=False,
        preview_did_not_create_ledger=True, duplicate_process_blocked=True, event_counts=counts,
        sqlite_integrity=integrity, expected_pnl_before_funding_usdt=str(expected),
        observed_pnl_before_funding_usdt=ledger['closed_pnl_before_funding_usdt'],
        net_pnl_usdt=None, close_reason=ledger['positions'][0]['close_reason'],
        note='Artificial timestamps shifted to wall clock to test live guards; not market observations.')
    write_json(root/'metrics.json', report, indent=2)
    print(json.dumps(report, indent=2))


if __name__ == '__main__': main()
