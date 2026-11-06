"""Artificial live-clock pinning and CLI watcher check; no exchange requests."""
import argparse
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from arbitrage.economic_data import EconomicData
from arbitrage.paper import PaperLedger, encode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('data/paper-watch-check'))
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).parent/'tests'))
    from test_paper import document, later
    from test_economics import NOW
    from test_economic_data import arguments
    entry, registry = document()
    stamp = int(time.time()*1000)
    delta = stamp-NOW

    def shift(value):
        if isinstance(value, dict): return {key:shift(item) for key,item in value.items()}
        if isinstance(value, list): return [shift(item) for item in value]
        return value+delta if isinstance(value, int) and value > 1000000000000 else value

    entry = shift(entry)
    current = shift(later(document()[0], 1))
    args.output.mkdir(parents=True, exist_ok=False)
    ledger_path, snapshot_path, registry_path, status_path = [args.output/name for name in
        ('ARTIFICIAL-live-paper.sqlite3','ARTIFICIAL-latest.json','registry.json','watch-status.json')]
    registry_path.write_text(json.dumps(dict(version='ARTIFICIAL-PAPER-WATCH',bindings=list(registry.bindings.values())), default=str), encoding='utf-8')
    ledger = PaperLedger(ledger_path)
    ledger.open(entry, 'test', registry)
    settings = arguments(registry)
    settings.paper_database = ledger_path
    engine = EconomicData(settings, [])
    asyncio.run(engine.refresh_paper_pins())
    engine.state = 'ready'
    engine.legs = {(row['exchange'],row['native_id']):row for row in current['economics']['inputs']}
    economics = engine.snapshot({'rows':[]}, stamp+1)
    snapshot = dict(classification='ARTIFICIAL_TEST_NOT_MARKET_DATA',generated_at_ms=stamp+1,economics=economics,signals=None)
    snapshot_path.write_text(encode(snapshot), encoding='utf-8')

    def run():
        subprocess.run([sys.executable,'-m','arbitrage.paper_watch','--database',str(ledger_path),
            '--snapshot',str(snapshot_path),'--registry',str(registry_path),'--status',str(status_path)],
            capture_output=True,text=True,check=True)
        return json.loads(status_path.read_text(encoding='utf-8'))

    first = run()
    repeated = run()
    snapshot['economics']['state'] = 'stopped'
    snapshot_path.write_text(encode(snapshot), encoding='utf-8')
    stopped = run()
    with closing(sqlite3.connect(ledger_path)) as db:
        marks = db.execute("SELECT count(*) FROM paper_events WHERE kind='marked'").fetchone()[0]
    result = dict(classification='ARTIFICIAL_OFFLINE_CHECK_NOT_MARKET_RESULT',pinned_legs=len(economics['inputs']),
        new_signal_rows=len(economics['rows']),first=first['positions'][0]['state'],
        repeated=repeated['positions'][0]['state'],stopped=stopped['positions'][0]['state'],
        open_positions=ledger.report()['open_count'],mark_events=marks,passed=True)
    if (result['pinned_legs'] != 2 or result['new_signal_rows'] != 0 or result['first'] != 'marked'
            or result['repeated'] != 'already_current' or result['stopped'] != 'blocked'
            or result['open_positions'] != 1 or marks != 1):
        raise AssertionError('Controlled paper watcher mismatch')
    (args.output/'metrics.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__ == '__main__':
    main()
