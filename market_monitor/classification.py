"""Market admission based on linear contract metadata and public TradFi markers."""

from collections.abc import Callable, Mapping
import logging
import re
from typing import Any

from .market import is_target, numeric


LOG = logging.getLogger(__name__)
TICKER = re.compile(r'[A-Z0-9]{1,30}')
Fields = Mapping[str, Any]

TRADFI_SYMBOL_TYPES = (1, 'metal', 'stock', 'commodity')
TRADFI_CONTRACT_TYPES = ('stocks', 'indices', 'commodities', 'forex', 'metals')
TRADFI_UNDERLYINGS = ('COMMODITY', 'EQUITY', 'INDEX', 'PREMARKET')
BYBIT_TYPES = ('ETF', 'forex')
KUCOIN_ASSETS = ('STOCK', 'METAL', 'COMMODITY')
OKX_CATEGORIES = ('3', '4')

VENUE_MARKERS: dict[str, Callable[[Fields], bool]] = {
    'bybit': lambda info: info.get('symbolType') in BYBIT_TYPES,
    'kucoinfutures': lambda info: info.get('assetClass') in KUCOIN_ASSETS,
    'whitebit': lambda info: info.get('type') == 'tradfiFutures',
    'okx': lambda info: info.get('instCategory') in OKX_CATEGORIES,
    'htx': lambda info: bool(info.get('tradfi_labels')),
    'mexc': lambda info: 'mc-trade-zone-tradfi' in (info.get('conceptPlate') or []),
}

# Observed catalogue classes, including values used for supported crypto markets.
OBSERVED_CLASSES = {
    'aster': {'symbolType': (0, *TRADFI_SYMBOL_TYPES)},
    'binanceusdm': {'underlyingType': ('COIN', 'KR_EQUITY', 'HK_EQUITY', 'CN_EQUITY', 'FX', *TRADFI_UNDERLYINGS)},
    'bitget': {'symbolType': ('perpetual', *TRADFI_SYMBOL_TYPES), 'isRwa': ('NO', 'YES')},
    'bybit': {'symbolType': ('', 'innovation', *TRADFI_SYMBOL_TYPES, *BYBIT_TYPES)},
    'gate': {'contract_type': ('', *TRADFI_CONTRACT_TYPES)},
    'kucoinfutures': {'assetClass': ('CRYPTO', *KUCOIN_ASSETS)},
    'okx': {'instCategory': ('1', *OKX_CATEGORIES)},
    'whitebit': {'type': ('futures', 'tradfiFutures')},
}
MAX_CLASS_NOTICES = 64
_class_notices: set[tuple[str, str, str]] = set()


def report_unknown_classes(venue: str, info: Fields) -> None:
    for field, observed in OBSERVED_CLASSES.get(venue, {}).items():
        value = info.get(field)
        if value is None or value in observed:
            continue
        # Only bounded, printable representations of the selector are logged.
        label = repr(value[:60]) if isinstance(value, str) else (
            repr(value) if type(value) is int and value.bit_length() <= 64 else type(value).__name__
        )
        key = venue, field, label
        if key not in _class_notices and len(_class_notices) < MAX_CLASS_NOTICES:
            _class_notices.add(key)
            LOG.warning('Unrecognized market class on %s: %s=%s', venue, field, label)


def shared_special_market(info: Fields) -> bool:
    return (
        info.get('isRwa') == 'YES' or info.get('is_pre_market') is True or info.get('isPreListing') is True
        or info.get('symbolType') in TRADFI_SYMBOL_TYPES
        or info.get('contract_type') in TRADFI_CONTRACT_TYPES
        or info.get('underlyingType') in TRADFI_UNDERLYINGS
        or info.get('contractType') == 'TRADIFI_PERPETUAL'
    )


def valid_market(market: Fields, kind: str, *, venue: str | None = None, group: str | None = None) -> bool:
    ticker = group if group is not None else market.get('base')
    quote = market.get('quote')
    if not isinstance(ticker, str) or not TICKER.fullmatch(ticker) or quote not in ('USDT', 'USDC'):
        return False
    if kind == 'spot':
        return market.get('spot') is True and market.get('active') is True
    if not is_target(market, quote):
        return False
    size = numeric(market.get('contractSize'))
    if size is None or size <= 0:
        return False
    info = market.get('info') or {}
    if venue in OBSERVED_CLASSES:
        report_unknown_classes(venue, info)
    predicate = VENUE_MARKERS.get(venue)
    return not (shared_special_market(info) or (predicate is not None and predicate(info)))
