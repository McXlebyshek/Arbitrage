"""Compact wire format shared by published snapshots and versioned metadata."""

import orjson

from .thresholds import PUBLIC_CONFIG

STATIC_FIELDS = ('exchange', 'type', 'quote', 'symbol', 'asset', 'size', 'networks', 'nativeBase', 'priceUnits')


def encode_compact(snapshot, previous_metadata, current_version, next_version):
    metadata = {}
    groups = {}
    for coin, legs in snapshot['markets'].items():
        dynamic_legs = groups[coin] = []
        for leg in legs:
            static = {key: leg[key] for key in STATIC_FIELDS if key in leg}
            if leg.get('type') == 'spot' and 'totals' in leg:
                static['totals'] = leg['totals']
            metadata[leg['id']] = static
            dynamic_legs.append({key: value for key, value in leg.items() if key not in static})
    changed = metadata != previous_metadata
    version = next_version if changed else current_version
    body = orjson.dumps(dict(snapshot, markets=groups, metaVersion=version))
    metadata_body = orjson.dumps(dict(version=version, config=PUBLIC_CONFIG, markets=metadata)) if changed else None
    return body, version, metadata if changed else previous_metadata, metadata_body
