"""Catalog rules for the crypto-only screener."""

import logging
import re

from .market import is_target, numeric


LOG = logging.getLogger(__name__)
TICKER = re.compile(r'[A-Z0-9]{1,30}')
COMMON_SYMBOL_TYPES = (1, 'metal', 'stock', 'commodity')
VENUE_RULES = {
    'bybit': lambda info: info.get('symbolType') in ('ETF', 'forex'),
    'kucoinfutures': lambda info: info.get('assetClass') in ('STOCK', 'METAL', 'COMMODITY'),
    'whitebit': lambda info: info.get('type') == 'tradfiFutures',
    'okx': lambda info: info.get('instCategory') in ('3', '4'),
    'htx': lambda info: bool(info.get('tradfi_labels')),
    'mexc': lambda info: 'mc-trade-zone-tradfi' in (info.get('conceptPlate') or []),
}
unknown_types = set()


def valid_market(market: dict, kind: str, *, venue: str | None = None, group: str | None = None) -> bool:
    ticker = group if group is not None else market.get('base')
    quote = market.get('quote')
    if not isinstance(ticker, str) or not TICKER.fullmatch(ticker) or quote not in ('USDT', 'USDC'):
        return False
    if kind == 'spot':
        return market.get('spot') is True and market.get('active') is True
    size = numeric(market.get('contractSize'))
    if not is_target(market, quote) or size is None or size <= 0:
        return False
    info = market.get('info') or {}
    symbol_type = info.get('symbolType')
    excluded = (
        info.get('isRwa') == 'YES' or info.get('is_pre_market') is True or info.get('isPreListing') is True
        or symbol_type in COMMON_SYMBOL_TYPES
        or info.get('contract_type') in ('stocks', 'indices', 'commodities', 'forex', 'metals')
        or info.get('underlyingType') in ('COMMODITY', 'EQUITY', 'INDEX', 'PREMARKET')
        or info.get('contractType') == 'TRADIFI_PERPETUAL'
    )
    rule = VENUE_RULES.get(venue)
    if excluded or (rule is not None and rule(info)):
        return False
    if symbol_type not in (None, '', 0, 'crypto', 'Crypto', 'CRYPTO'):
        key = venue, repr(symbol_type)[:80]
        if key not in unknown_types and len(unknown_types) < 40:
            unknown_types.add(key)
            LOG.info('%s catalog: unclassified symbolType %s', *key)
    return True
