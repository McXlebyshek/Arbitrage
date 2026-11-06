"""Opt-in rules for virtual positions only. Default CLI mode previews decisions."""
import argparse
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
import time

from .atomic_json import write_json
from .paper import PaperLedger, evidence, exit_value
from .paper_watch import check_status_path
from .recovery import SessionInUse, SessionLease
from .scanner import Registry, decimal


@dataclass(frozen=True)
class RulePolicy:
    min_entry_usdt: str = '1'
    take_profit_usdt: str = '5'
    stop_loss_usdt: str = '20'
    max_hold_seconds: int = 28800
    cooldown_seconds: int = 300
    max_positions: int = 1

    def __post_init__(self):
        for key in ('min_entry_usdt', 'take_profit_usdt', 'stop_loss_usdt'):
            value = getattr(self, key)
            try: number = Decimal(str(value))
            except (InvalidOperation, ValueError): raise ValueError('invalid_rule_'+key) from None
            if isinstance(value, bool) or not number.is_finite() or not 0 < number <= 1000000:
                raise ValueError('invalid_rule_'+key)
        for key, low, high in (('max_hold_seconds', 1, 2592000), ('cooldown_seconds', 0, 2592000), ('max_positions', 1, 10)):
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError('invalid_rule_'+key)

    def document(self):
        result = asdict(self)
        for key in ('min_entry_usdt', 'take_profit_usdt', 'stop_loss_usdt'):
            result[key] = str(decimal(result[key]))
        return result


def owned_policy(position):
    automation = position.get('automation') or {}
    if position.get('mode') != 'live_snapshot' or automation.get('owner') != 'paper_rules_v1':
        raise ValueError('paper_rule_position_not_owned')
    return RulePolicy(**automation['policy'])


def exit_reason(position, valuation, policy):
    pnl = decimal(valuation['pnl_before_funding_usdt'])
    if pnl <= -decimal(policy.stop_loss_usdt): return 'stop_loss_before_funding'
    if pnl >= decimal(policy.take_profit_usdt): return 'take_profit_before_funding'
    if valuation['timestamp_ms']-position['opened_at_ms'] >= policy.max_hold_seconds*1000:
        return 'max_hold'
    return None


ERRORS = (ValueError, KeyError, TypeError, ArithmeticError, OSError, sqlite3.Error)


def tick(database, snapshot, registry, policy, apply=False, now_ms=None):
    now = int(time.time()*1000) if now_ms is None else now_ms
    report = dict(classification='paper_rules_status', generated_at_ms=now, state='ready',
        mode='apply' if apply else 'preview', policy=policy.document(), decisions=[],
        execution_verified=False, real_orders=False)
    try:
        positions = PaperLedger(database).report()['positions']
        if any(row['mode'] != 'live_snapshot' for row in positions):
            raise ValueError('paper_rules_require_live_ledger')
        document = json.loads(Path(snapshot).read_text(encoding='utf-8-sig'))
        stamp, legs, digest, clock = evidence(document, False, now)
    except ERRORS as exc:
        return dict(report, state='blocked', error=str(exc))
    ledger = PaperLedger(database)
    # Only positions carrying our persisted ownership/policy may be closed.
    for position in positions:
        if position['state'] != 'open' or (position.get('automation') or {}).get('owner') != 'paper_rules_v1':
            continue
        item = dict(position_id=position['id'], pair_id=position['pair']['id'])
        try:
            stored = owned_policy(position)
            valuation = exit_value(position, legs, registry, stamp, position['policy'], clock)
            reason = exit_reason(position, valuation, stored)
            if not reason:
                report['decisions'].append(dict(item, action='hold'))
                continue
            if stamp <= position['opened_at_ms'] or any(valuation['depth_source_times'][side] <= position['entry_depth_source_times'][side] for side in ('long', 'short')):
                raise ValueError('paper_close_requires_later_depth')
            if apply:
                closed = ledger.value(document, position['id'], registry, close=True, now_ms=now, automatic=True)
                action = 'closed' if closed['state'] == 'closed' else 'blocked'
            else:
                action = 'would_close'
            report['decisions'].append(dict(item, action=action, reason=reason, basis='pnl_before_funding_usdt'))
        except ERRORS as exc:
            report['decisions'].append(dict(item, action='blocked', error=str(exc)))
    signals = document.get('signals') or {}
    candidates = [row for row in signals.get('rows', []) if row.get('state') == 'confirmed']
    # Stable ranking and at most one accepted entry per tick; ledger rechecks all inputs.
    try: candidates.sort(key=lambda row: (-decimal(row['conditional_net_usdt']), row['id']))
    except ERRORS as exc: return dict(report, state='blocked', error=str(exc))
    for row in candidates:
        item = dict(pair_id=row['id'])
        try:
            if decimal(row['conditional_net_usdt']) < decimal(policy.min_entry_usdt):
                continue
            if any(pos['pair']['id'] == row['id'] and pos['state'] == 'open' for pos in positions):
                continue
            if not apply:
                report['decisions'].append(dict(item, action='entry_candidate', revalidation_required=True))
                break
            position = ledger.open(document, row['id'], registry, now_ms=now, automation=policy.document())
            report['decisions'].append(dict(item, action='opened' if position['state'] == 'open' else 'already_used', position_id=position['id']))
            break
        except ERRORS as exc:
            report['decisions'].append(dict(item, action='blocked', error=str(exc)))
    if any(row['action'] == 'blocked' for row in report['decisions']): report['state'] = 'partial'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=Path('data/paper.sqlite3'))
    parser.add_argument('--snapshot', type=Path, default=Path('data/latest.json'))
    parser.add_argument('--status', type=Path, default=Path('data/paper-rules.json'))
    parser.add_argument('--registry', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--apply', action='store_true', help='Allow virtual paper entries/exits')
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    try:
        policy = RulePolicy(**(json.loads(args.config.read_text(encoding='utf-8-sig')) if args.config else {}))
        registry = Registry.load(args.registry)
        if args.database.resolve() == args.snapshot.resolve() or (args.database.exists() and args.snapshot.exists() and args.database.samefile(args.snapshot)):
            raise ValueError('paper_database_must_differ_from_snapshot')
        check_status_path(args.status, [args.database, args.snapshot, args.registry, args.config], 'paper_rules_status')
        with SessionLease(str(args.database.resolve())+'.paper-rules'):
            previous = None
            while True:
                report = tick(args.database, args.snapshot, registry, policy, args.apply)
                write_json(args.status, report)
                summary = {key: value for key, value in report.items() if key != 'generated_at_ms'}
                if summary != previous:
                    print(json.dumps(report, ensure_ascii=False), flush=True)
                    previous = summary
                if not args.watch: break
                time.sleep(2)
    except KeyboardInterrupt: pass
    except SessionInUse: parser.error('Another paper rules process is using this ledger')
    except ERRORS as exc: parser.error(str(exc))


if __name__ == '__main__': main()
