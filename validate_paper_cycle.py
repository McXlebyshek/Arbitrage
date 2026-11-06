"""Controlled offline CLI paper cycle. Artificial prices; never a market result."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('data/paper-core-check'))
    args = parser.parse_args()
    # Uses the explicitly artificial economics fixture distributed with the tests.
    sys.path.insert(0, str(Path(__file__).parent/'tests'))
    from test_paper import document, later
    snapshot, registry = document()
    args.output.mkdir(parents=True, exist_ok=False)
    entry, exit_path, registry_path, ledger = [args.output/name for name in ('entry.json', 'exit.json', 'registry.json', 'paper.sqlite3')]
    entry.write_text(json.dumps(snapshot), encoding='utf-8')
    exit_path.write_text(json.dumps(later(snapshot)), encoding='utf-8')
    registry_path.write_text(json.dumps(dict(version='ARTIFICIAL-PAPER-CHECK',
        bindings=list(registry.bindings.values())), default=str), encoding='utf-8')

    def run(action, *extra):
        result = subprocess.run([sys.executable, '-m', 'arbitrage.paper', action, '--replay',
            '--database', str(ledger), '--registry', str(registry_path), *extra], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    opened = run('open', '--pair', 'test', '--snapshot', str(entry))
    closed = run('close', '--position', opened['id'], '--snapshot', str(exit_path))
    status = run('status')
    actual = Decimal(closed['valuation']['pnl_before_funding_usdt'])
    expected = Decimal('4.944')*(Decimal('102')-100+Decimal('100.9')-Decimal('101.1'))-Decimal('0.998688')
    if actual != expected or status['open_count'] != 0 or status['closed_count'] != 1 or status['net_pnl_usdt'] is not None:
        raise AssertionError('Controlled CLI accounting mismatch')
    report = dict(classification='ARTIFICIAL_OFFLINE_CHECK_NOT_MARKET_RESULT',
        base_quantity=opened['base_quantity'], entry_fees_usdt=opened['entry_fees_usdt'],
        exit_fees_usdt=closed['valuation']['exit_fees_usdt'], expected_pnl_before_funding_usdt=str(expected),
        observed_pnl_before_funding_usdt=str(actual), net_pnl_usdt=None, funding_status='not_accounted',
        open_count=status['open_count'], closed_count=status['closed_count'], passed=True)
    (args.output/'metrics.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
