"""Artificial offline CLI funding reconciliation, not market profitability."""
import argparse
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from arbitrage.funding_history import persist


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('data/paper-funding-check'))
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).parent/'tests'))
    from test_paper import document, later
    from test_economics import NOW, HOUR
    snapshot, registry = document()
    args.output.mkdir(parents=True, exist_ok=False)
    ledger, market, entry, exit_path, registry_path, prices_path = [args.output/name for name in
        ('paper.sqlite3', 'ARTIFICIAL-market.sqlite3', 'entry.json', 'exit.json', 'registry.json', 'ARTIFICIAL-prices.json')]
    entry.write_text(json.dumps(snapshot), encoding='utf-8')
    exit_path.write_text(json.dumps(later(snapshot, 9*HOUR)), encoding='utf-8')
    registry_path.write_text(json.dumps(dict(version='ARTIFICIAL-FUNDING-CHECK', bindings=list(registry.bindings.values())), default=str), encoding='utf-8')
    for exchange, hours, rates in (('alpha', [-16,-8,0,8], ['.001']*4),
            ('beta', [-8,-4,0,4,8], ['.001','.001','.001','.002','.001'])):
        persist(market, exchange, dict(id='BTC_USDT', asset='bitcoin'),
            dict(events=[dict(timestamp=NOW+hour*HOUR, rate=rate) for hour,rate in zip(hours,rates)], requests=0, error=None), NOW+9*HOUR)
    prices = [dict(exchange=exchange, native_id='BTC_USDT', timestamp_ms=NOW+hour*HOUR,
        mark_price_native=price, source='ARTIFICIAL TEST PRICE, NOT EXCHANGE DATA')
        for exchange,hour,price in [('alpha',8,'100'), ('beta',4,'102'), ('beta',8,'104')]]
    prices_path.write_text(json.dumps(prices), encoding='utf-8')

    def run(action, *extra):
        result = subprocess.run([sys.executable, '-m', 'arbitrage.paper', action, '--replay',
            '--database', str(ledger), '--registry', str(registry_path), *extra], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    opened = run('open', '--pair', 'test', '--snapshot', str(entry))
    run('close', '--position', opened['id'], '--snapshot', str(exit_path))
    before = sha256(market.read_bytes()).hexdigest()
    incomplete = run('funding', '--position', opened['id'], '--market-database', str(market))
    complete = run('funding', '--position', opened['id'], '--market-database', str(market), '--funding-prices', str(prices_path))
    repeated = run('funding', '--position', opened['id'], '--market-database', str(market), '--funding-prices', str(prices_path))
    funding = Decimal('4.944')*(-Decimal('100')*Decimal('.001')+Decimal('102')*Decimal('.002')+Decimal('104')*Decimal('.001'))
    net = Decimal('7.900512')+funding
    with sqlite3.connect(ledger) as db:
        count = db.execute("SELECT count(*) FROM paper_events WHERE kind='funding_reconciled'").fetchone()[0]
    if (incomplete['valuation']['net_pnl_usdt'] is not None or Decimal(complete['valuation']['funding_usdt']) != funding
            or Decimal(complete['valuation']['net_pnl_usdt']) != net or complete != repeated or count != 2
            or sha256(market.read_bytes()).hexdigest() != before):
        raise AssertionError('Funding CLI reconciliation mismatch')
    report = dict(classification='ARTIFICIAL_OFFLINE_CHECK_NOT_MARKET_RESULT', events=3,
        missing_prices_net_pnl_usdt=incomplete['valuation']['net_pnl_usdt'], expected_funding_usdt=str(funding),
        observed_funding_usdt=complete['valuation']['funding_usdt'], expected_net_pnl_usdt=str(net),
        observed_net_pnl_usdt=complete['valuation']['net_pnl_usdt'], idempotent=True,
        source_database_unchanged=True, price_evidence_verified=False, passed=True)
    (args.output/'metrics.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
