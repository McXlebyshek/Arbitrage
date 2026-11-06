"""Artificial offline CLI funding worker lifecycle; never operates user positions."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('data/funding-watch-check'))
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    seed=args.output/'ARTIFICIAL-seed'
    subprocess.run([sys.executable,str(Path(__file__).with_name('validate_paper_funding.py')),'--output',str(seed)],capture_output=True,text=True,check=True)
    ledger=seed/'paper.sqlite3'
    # Deliberately convert only this synthetic fixture to live-format to test the live-only worker.
    with closing(sqlite3.connect(ledger)) as db,db:
        db.execute("UPDATE paper_meta SET mode='live_snapshot'")
        db.execute("UPDATE paper_positions SET payload=json_set(payload,'$.mode','live_snapshot')")
    status=args.output/'worker-status.json'
    def run():
        subprocess.run([sys.executable,'-m','arbitrage.paper_funding_watch','--database',str(ledger),
            '--market-database',str(seed/'ARTIFICIAL-market.sqlite3'),'--registry',str(seed/'registry.json'),
            '--funding-prices',str(seed/'ARTIFICIAL-prices.json'),'--status',str(status)],capture_output=True,text=True,check=True)
        return json.loads(status.read_text(encoding='utf-8'))
    first=run()
    with closing(sqlite3.connect(ledger)) as db:
        before=db.execute("SELECT count(*) FROM paper_events WHERE kind='funding_reconciled'").fetchone()[0]
    repeated=run()
    with closing(sqlite3.connect(ledger)) as db:
        after=db.execute("SELECT count(*) FROM paper_events WHERE kind='funding_reconciled'").fetchone()[0]
    (seed/'ARTIFICIAL-prices.json').write_text('[]',encoding='utf-8')
    partial=run()
    with closing(sqlite3.connect(ledger)) as db:
        position=json.loads(db.execute('SELECT payload FROM paper_positions LIMIT 1').fetchone()[0])
    result=dict(classification='ARTIFICIAL_OFFLINE_FUNDING_WORKER_CHECK',first=first['state'],repeated=repeated['state'],
        requests=first['requests']+repeated['requests']+partial['requests'],network_enabled=first['network_enabled'],
        duplicate_audit_events=after-before,missing_prices_state=partial['positions'][0]['state'],
        net_after_missing_prices=position['valuation']['net_pnl_usdt'],passed=True)
    if (result['first']!='ready' or result['repeated']!='ready' or result['requests']!=0
            or result['network_enabled'] or result['duplicate_audit_events']!=0
            or result['missing_prices_state']!='rates_covered_marks_missing' or result['net_after_missing_prices'] is not None):
        raise AssertionError('Funding worker lifecycle mismatch')
    (args.output/'metrics.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__': main()
