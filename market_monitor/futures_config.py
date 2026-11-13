"""Pinned public futures symbols used to keep important WS subscriptions warm."""

PINNED_FUTURES_SYMBOLS = {
    'BTC': {
        'mexc': 'BTC_USDT',
        'kucoinfutures': 'XBTUSDTM',
    },
    'INIT': {
        'mexc': 'INIT_USDT',
        'kucoinfutures': 'INITUSDTM',
    },
    'ARIA': {
        'mexc': 'ARIA_USDT',
        'kucoinfutures': 'ARIAUSDTM',
    },
    'JCT': {
        'mexc': 'JCT_USDT',
        'kucoinfutures': 'JCTUSDTM',
    },
    'AGT': {
        'mexc': 'AGT_USDT',
        'kucoinfutures': 'AGTUSDTM',
    },
}
