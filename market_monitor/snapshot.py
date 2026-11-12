"""Build a market snapshot from the current catalogs and quotes."""

from collections import defaultdict

from .current_funding import funding_fields
from .market import compatible_assets, fresh, numeric
from .market_admission import valid_market
from .sources import CYCLE_FUNDING_VENUES, VENUES


class SnapshotBuilder:
    def __init__(self, sources, clocks, registry, detail_for):
        self.sources = sources
        self.clocks = clocks
        self.registry = registry
        self.detail_for = detail_for
        self.catalog_descriptors = {}
        self.snapshot_plans = {}
        self.network_descriptors = {}

    def market_descriptors(self, venue, kind, source):
        catalog = source.catalogs.get(venue)
        if catalog is None:
            self.catalog_descriptors.pop((venue, kind), None)
            return ()
        cached = self.catalog_descriptors.get((venue, kind))
        if source.catalog_at is not None and cached is not None and cached[0] is catalog:
            return cached[1]
        entries = []
        for market in catalog.values():
            binding = self.registry.match(venue, kind, market)
            group = binding.get('group') if binding else None
            if valid_market(market, kind, venue=venue, group=group):
                base, quote = market['base'], market['quote']
                group = group or base
                entries.append((market, binding, group, f'{venue}.{kind}.{group}.{quote}'))
        if source.catalog_at is not None:
            self.catalog_descriptors[venue, kind] = catalog, entries
        return entries

    def network_details(self, venue, base):
        source = self.sources.get((venue, 'spot'))
        if source is None:
            return [], [None, None]
        currency = source.currencies.get(base)
        if currency is None:
            return [], [None, None]
        key = venue, base
        cached = self.network_descriptors.get(key)
        if cached and cached[0] is currency:
            return cached[1:]
        networks = [dict(name=code, deposit=item.get('deposit'), withdraw=item.get('withdraw'))
                    for code, item in (currency.get('networks') or {}).items()
                    if item.get('deposit') is not None or item.get('withdraw') is not None]
        status = []
        for field in ('deposit', 'withdraw'):
            values = [network[field] for network in networks]
            status.append(True if True in values else False if values and all(v is False for v in values) else None)
        self.network_descriptors[key] = currency, networks, status
        return networks, status

    def plan(self, keys):
        inputs = []
        for venue, kind in keys:
            source = self.sources[venue, kind]
            inputs.append((source, self.market_descriptors(venue, kind, source)))
        cached = self.snapshot_plans.get(keys)
        if cached is not None and all(
            source is previous_source and entries is previous_entries
            for (source, entries), (previous_source, previous_entries) in zip(inputs, cached[0])
        ):
            return cached[1], cached[2]

        groups, instruments = defaultdict(list), {}
        for index, ((venue, kind), (source, entries)) in enumerate(zip(keys, inputs)):
            for market, binding, group, key in entries:
                if key in instruments:
                    # A route must identify exactly one native instrument.
                    instruments[key] = None
                    continue
                instruments[key] = (source, market)
                identity = dict(id=key, type=kind, exchange=VENUES[venue], quote=market['quote'],
                                asset=binding['asset'] if binding else None)
                if group != market['base']:
                    identity['nativeBase'] = market['base']
                if kind == 'swap' and binding and binding['price_units'] != 1:
                    identity['priceUnits'] = binding['price_units']
                groups[group].append((index, market, identity))

        selected, routes = {}, {}
        for group, entries in groups.items():
            unique = [entry for entry in entries if instruments[entry[2]['id']] is not None]
            usable = []
            for entry in unique:
                leg = entry[2]
                if any(
                    other is not leg and other['quote'] == leg['quote'] and
                    (other['type'] == 'swap' or leg['type'] == 'swap') and
                    (other['exchange'] != leg['exchange'] or other['type'] != leg['type']) and
                    compatible_assets(leg, other)
                    for _, _, other in unique
                ):
                    usable.append(entry)
                    routes[leg['id']] = instruments[leg['id']]
            if usable:
                selected[group] = usable

        self.snapshot_plans[keys] = inputs, selected, routes
        return selected, routes

    def build(self, now, *, source_keys=None):
        keys = tuple(self.sources if source_keys is None else source_keys)
        selected, routes = self.plan(keys)
        live_sources = []
        for venue, kind in keys:
            source = self.sources[venue, kind]
            batch = source.batches.get(venue, {})
            clock = self.clocks.get(venue)
            offset = clock.estimate(now) if clock is not None else None
            live_sources.append((source, venue, kind, batch.get('tickers', {}), batch.get('received_at_ms'), offset or 0))

        result = {}
        for group, entries in selected.items():
            legs = []
            for index, market, identity in entries:
                source, venue, kind, tickers, batch_received, offset = live_sources[index]
                symbol, key = market['symbol'], identity['id']
                base = market['base']
                ticker = tickers.get(symbol, {})
                received = ticker.get('quote_received_at_ms', batch_received)
                stamp = numeric(ticker.get('timestamp'))
                if stamp is not None:
                    stamp -= offset
                current = ticker.get('symbol') == symbol and ticker.get('native_id', market['id']) == market['id'] and fresh(received, now) and (
                    stamp is None or fresh(stamp, now))
                if not current:
                    ticker, stamp = {}, None
                bid, ask = numeric(ticker.get('bid')), numeric(ticker.get('ask'))
                if not (bid and ask and 0 < bid <= ask):
                    bid = ask = None
                raw = source.funding.get(symbol) or ticker
                raw_info = raw.get('info') or {}
                ticker_info = ticker.get('info') or {}
                turnover_ticker = source.volume.get(symbol, {})
                if (turnover_ticker.get('expiresAt', now + 1) <= now
                        or turnover_ticker.get('native_id', market['id']) != market['id']):
                    turnover_ticker = {}
                volume = numeric(ticker.get('quoteVolume'))
                if volume is None:
                    volume = numeric(turnover_ticker.get('quoteVolume'))
                _, network_status = self.network_details(venue, base)
                detail = self.detail_for(key, now) or {}
                fields = funding_fields(raw, market, venue, now, source.schedule) if kind == 'swap' else dict(rate=0, hours=None, next=None)
                if kind == 'swap':
                    details_funding = detail.get('funding', {})
                    if venue in CYCLE_FUNDING_VENUES and (details_funding.get('next') or 0) <= now:
                        details_funding = {}
                    for field, value in details_funding.items():
                        if fields[field] is None and value is not None and (field != 'next' or value > now):
                            fields[field] = value
                leg = dict(
                    id=key, exchange=identity['exchange'], type=kind, quote=identity['quote'], symbol=symbol,
                    bid=bid, ask=ask, last=numeric(ticker.get('last') or turnover_ticker.get('last')),
                    asset=identity['asset'],
                    sourceAt=stamp, receivedAt=received if current else None,
                    index=numeric(raw.get('indexPrice') or raw_info.get('indexPrice')
                                  or raw_info.get('index_price') or ticker_info.get('indexPrice')),
                    oi=numeric(ticker.get('openInterestValue') or ticker_info.get('openInterestValue')),
                    volume=volume / 1_000_000 if volume is not None and volume >= 0 else None,
                    **fields,
                )
                if network_status != [None, None]:
                    leg['status'] = network_status
                if kind == 'swap' and detail.get('totals'):
                    leg['totals'] = detail['totals']
                if 'nativeBase' in identity:
                    leg['nativeBase'] = identity['nativeBase']
                if 'priceUnits' in identity:
                    leg['priceUnits'] = identity['priceUnits']
                legs.append(leg)
            result[group] = legs
        return result, routes
