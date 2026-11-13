"""Public index composition and documented contract limits."""

from .market import fresh, now_ms, numeric

INDEX_MAX_AGE = 300_000
INDEX_VENUES = ('binanceusdm', 'bybit', 'okx', 'mexc')


def contract_limits(venue, market):
    info = market.get('info') or {}
    if venue == 'binanceusdm':
        limits = market.get('limits') or {}
        market_order = limits.get('market') or {}
        limit_order = limits.get('amount') or {}
        fields = dict(marketMin=market_order.get('min'), marketMax=market_order.get('max'),
                      limitMin=limit_order.get('min'), limitMax=limit_order.get('max'),
                      notionalMin=(limits.get('cost') or {}).get('min'))
    elif venue == 'bybit':
        lot = info.get('lotSizeFilter') or {}
        fields = dict(orderMin=lot.get('minOrderQty'), marketMax=lot.get('maxMktOrderQty'),
                      limitMax=lot.get('maxOrderQty'), notionalMin=lot.get('minNotionalValue'))
    elif venue == 'okx':
        fields = dict(orderMin=info.get('minSz'), marketMax=info.get('maxMktSz'), limitMax=info.get('maxLmtSz'),
                      positionFixedUsd=info.get('posLmtAmt'), positionSharePct=info.get('posLmtPct'),
                      platformPositionUsd=info.get('maxPlatOILmt'), platformPositionCoins=info.get('maxPlatOICoinLmt'))
    elif venue == 'mexc':
        fields = dict(orderMin=info.get('minVol'), marketMax=info.get('maxVol'), limitMax=info.get('limitMaxVol'))
        if info.get('riskLimitType') == 'BY_VOLUME':
            if info.get('riskLongShortSwitch') == 0:
                fields['riskBase'] = info.get('riskBaseVol')
            elif info.get('riskLongShortSwitch') == 1:
                fields.update(riskBaseLong=info.get('riskBaseVolLong'), riskBaseShort=info.get('riskBaseVolShort'))
    else:
        return None
    values = {key: n for key, value in fields.items() if (n := numeric(value)) is not None and n > 0}
    if not values:
        return None
    return dict(values, inContracts=venue in ('okx', 'mexc'), contractSize=numeric(market.get('contractSize')))


def parse_index(name, returned_name, stamp, rows, fields, now):
    stamp = numeric(stamp)
    if returned_name != name or not fresh(stamp, now, INDEX_MAX_AGE) or not isinstance(rows, list) or not rows:
        raise ValueError('Index components unavailable or expired')
    components = []
    exchange_key, symbol_key, weight_key = fields
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid index component')
        exchange, symbol = row.get(exchange_key), row.get(symbol_key)
        if not isinstance(exchange, str) or not exchange or not isinstance(symbol, str) or not symbol:
            raise ValueError('Invalid index component')
        weight = numeric(row.get(weight_key))
        components.append(dict(exchange=exchange, symbol=symbol,
                               weight=weight if weight is not None and 0 <= weight <= 1 else None))
    return dict(name=name, observedAt=int(stamp), fromCatalog=False, components=components)


async def contract_index(venue, client, market, catalog_at):
    name = market['id']
    if venue == 'mexc':
        origins = (market.get('info') or {}).get('indexOrigin')
        if isinstance(origins, list):
            components = [dict(exchange=item, symbol=None, weight=None) for item in origins
                          if isinstance(item, str) and item]
            if components:
                return dict(name=name, observedAt=catalog_at, fromCatalog=True, components=components)
        return None
    if venue == 'bybit':
        data = (await client.public_get_v5_market_index_price_components({'indexName': name}))['result']
        return parse_index(name, data['indexName'], data['updateTime'], data['components'],
                           ('exchange', 'spotPair', 'weight'), now_ms())
    if venue == 'okx':
        name = (market.get('info') or {}).get('uly')
        if not name:
            return None
        data = (await client.public_get_market_index_components({'index': name}))['data']
        return parse_index(name, data['index'], data['ts'], data['components'], ('exch', 'symbol', 'wgt'), now_ms())
    if venue == 'binanceusdm':
        data = await client.fapiPublicGetConstituents({'symbol': name})
        return parse_index(name, data['symbol'], data['time'], data['constituents'],
                           ('exchange', 'symbol', 'weight'), now_ms())
    return None
