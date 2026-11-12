"""Encode the browser's compact snapshot with versioned instrument metadata."""

from dataclasses import dataclass
from operator import itemgetter
from uuid import uuid4

from orjson import dumps


STATIC_FIELDS = ('id', 'exchange', 'type', 'quote', 'symbol', 'asset', 'nativeBase', 'priceUnits')
DYNAMIC_FIELDS = ('bid', 'ask', 'last', 'sourceAt', 'receivedAt', 'index', 'oi', 'volume',
                  'rate', 'hours', 'next', 'cap', 'floor', 'totals', 'status', 'networks')
MARKET_FIELDS = frozenset((*STATIC_FIELDS, *DYNAMIC_FIELDS))
STATIC_CORE = itemgetter(*STATIC_FIELDS[:6])
DYNAMIC_CORE = itemgetter(*DYNAMIC_FIELDS[:11])


@dataclass(frozen=True)
class MarketMetadata:
    version: str
    groups: list
    records: list
    body: bytes


def encode_compact(snapshot, previous):
    groups = []
    records = []
    updates = []
    for coin, legs in snapshot['markets'].items():
        groups.append((coin, len(legs)))
        for leg in legs:
            if not MARKET_FIELDS.issuperset(leg):
                # Keep the full representation available for unfamiliar fields.
                return None, None
            try:
                record = (*STATIC_CORE(leg), leg.get('nativeBase'), leg.get('priceUnits'))
                update = (*DYNAMIC_CORE(leg), leg.get('cap'), leg.get('floor'),
                          leg.get('totals'), leg.get('status'), leg.get('networks'))
            except KeyError:
                record = tuple(leg.get(key) for key in STATIC_FIELDS)
                update = tuple(leg.get(key) for key in DYNAMIC_FIELDS)
            records.append(record)
            updates.append(update)
    if previous is not None and previous.groups == groups and previous.records == records:
        metadata = previous
    else:
        version = uuid4().hex
        metadata = MarketMetadata(version, groups, records, dumps(dict(
            version=version, groups=groups, records=records, staticFields=STATIC_FIELDS, dynamicFields=DYNAMIC_FIELDS,
        )))
    compact = {key: value for key, value in snapshot.items() if key != 'markets'}
    return dumps(dict(compact, metaVersion=metadata.version, marketRows=updates)), metadata
