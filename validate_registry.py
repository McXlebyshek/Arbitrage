"""Read public contract metadata and audit explicit bindings; never edits a registry."""
import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import time

from arbitrage.atomic_json import write_json
from arbitrage.collector import EXCHANGES, public_client_config
from arbitrage.models import is_target
from arbitrage.scanner import Registry, positive


def audit(registry, catalog):
    checked, errors = [], []
    for (exchange, native), binding in registry.bindings.items():
        matches = [row for row in catalog.get(exchange, []) if row['id'] == native]
        reason = None
        if len(matches) != 1: reason = 'missing_or_ambiguous_contract'
        else:
            row = matches[0]
            if not is_target(row): reason = 'not_active_linear_usdt_perpetual'
            elif row['base'] != binding['base']: reason = 'base_identity_changed'
            else:
                try:
                    if positive(row.get('contractSize')) != binding['contract_size']: reason = 'contract_size_changed'
                except (ValueError, TypeError): reason = 'invalid_contract_size'
        item = dict(exchange=exchange, native_id=native, asset=binding['asset'], base=binding['base'])
        if reason: errors.append(dict(item, reason=reason))
        else: checked.append(item)
    return dict(checked=checked, errors=errors, passed=not errors and bool(checked))


async def collect(root):
    import ccxt.async_support as ccxt
    async def venue(exchange):
        client = getattr(ccxt, exchange)(public_client_config(exchange))
        fields = ('id','symbol','base','quote','settle','swap','linear','active','contractSize','precision','limits','info')
        failures = []
        try:
            async with asyncio.timeout(90):
                for attempt in range(2):
                    try:
                        markets = await client.load_markets(reload=True)
                        return exchange, [{key:row.get(key) for key in fields} for row in markets.values()
                            if row.get('swap') and row.get('linear') and row.get('quote') == 'USDT'], failures
                    except Exception as exc:
                        failures.append(type(exc).__name__+': '+str(exc))
                        if attempt == 0: await asyncio.sleep(2)
        except TimeoutError:
            failures.append('metadata_deadline_exceeded')
        finally: await client.close()
        return exchange, [], failures
    replies = await asyncio.gather(*(venue(exchange) for exchange in EXCHANGES))
    catalog = {exchange:rows for exchange,rows,_ in replies}
    write_json(root/'catalog.json', catalog, indent=2)
    return catalog, {exchange:errors for exchange,_,errors in replies if errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--registry', type=Path)
    parser.add_argument('--catalog', type=Path, help='Audit an existing saved catalog without network')
    args = parser.parse_args()
    registry = Registry.load(args.registry)
    args.output.mkdir(parents=True, exist_ok=False)
    if args.catalog:
        catalog, failures = json.loads(args.catalog.read_text(encoding='utf-8')), {}
    else: catalog, failures = asyncio.run(collect(args.output))
    report = dict(classification='PUBLIC_CONTRACT_METADATA_AUDIT',generated_at_ms=int(time.time()*1000),
        registry_version=registry.version, catalog_source='saved' if args.catalog else 'public_api',
        network_failures=failures, **audit(registry,catalog))
    report['checked_by_exchange'] = dict(Counter(row['exchange'] for row in report['checked']))
    write_json(args.output/'metrics.json', report, indent=2)
    print(json.dumps({key:report[key] for key in ('passed','checked_by_exchange','network_failures','errors')},indent=2))
    if not report['passed']: raise SystemExit(1)


if __name__ == '__main__': main()
