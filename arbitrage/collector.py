import asyncio
import time
from dataclasses import asdict

from .models import is_target, normalize

EXCHANGES = ("binanceusdm", "bybit", "okx", "bitget", "gate", "mexc")


def public_client_config(exchange):
    options = {"defaultType": "swap", "defaultSubType": "linear"}
    if exchange == "bitget":
        # This monitor needs swaps only. Loading unrelated UTA spot instruments
        # can fail when Bitget returns an instrument without its base asset.
        options["fetchMarkets"] = {"types": ["swap"]}
    config = {"enableRateLimit": True, "timeout": 15000, "options": options}
    if exchange == 'gate':
        options['fetchMarkets'] = {'types': ['swap']}
        options['swap'] = {'fetchMarkets': {'settlementCurrencies': ['usdt']}}
        # Linear USDT contract metadata supplies everything this monitor uses.
        # Currency/deposit networks and unrelated market catalogs are not needed.
        config['has'] = {'fetchCurrencies': False}
    return config


async def collect(exchange_id, bases, timeout_seconds=90):
    """One exchange failure cannot abort the other adapters. Never uses credentials."""
    import ccxt.async_support as ccxt

    client = getattr(ccxt, exchange_id)(public_client_config(exchange_id))
    rows, instruments = [], []
    status = {"exchange": exchange_id, "state": "error", "errors": [],
              "trading_availability": "not_checked", "transport": "REST"}
    try:
        async with asyncio.timeout(timeout_seconds):
            markets = await client.load_markets()
            instruments = [m for m in markets.values() if is_target(m)]
            selected = [m for m in instruments if not bases or m["base"] in bases]
            status["selected_markets"] = len(selected)
            if not selected:
                status["state"] = "no_markets"
            else:
                tickers = await client.fetch_tickers([m["symbol"] for m in selected])
                for market in selected:
                    ticker = tickers.get(market["symbol"], {})
                    quote = normalize(exchange_id, market, ticker, int(time.time() * 1000))
                    # Bounded probe: deep books for all markets belong to a later stage.
                    if (quote.bid is None or quote.ask is None) and bases:
                        try:
                            book = await client.fetch_order_book(market["symbol"], limit=20)
                            ticker = dict(ticker, bid=book["bids"][0][0] if book["bids"] else None,
                                          ask=book["asks"][0][0] if book["asks"] else None,
                                          timestamp=book.get("timestamp"))
                            quote = normalize(exchange_id, market, ticker,
                                              int(time.time() * 1000), "orderbook")
                        except Exception as exc:
                            status["errors"].append(f"{market['symbol']}: {type(exc).__name__}: {exc}")
                    rows.append(quote)
                status["state"] = "partial" if status["errors"] else "ok"
    except Exception as exc:
        status["errors"].append(f"{type(exc).__name__}: {exc}")
        status["state"] = "partial" if rows else "error"
    finally:
        await client.close()
    now = int(time.time() * 1000)
    status.update(checked_at_ms=now, markets=len(instruments), quotes=len(rows),
                  usable_quotes=sum(q.quality(now) == "ok" for q in rows))
    fields = ("id", "symbol", "base", "quote", "settle", "contractSize", "precision", "limits", "active", "swap", "linear")
    return {"status": status, "instruments": [{k: m.get(k) for k in fields} for m in instruments],
            "quotes": [dict(asdict(q), quality=q.quality(now)) for q in rows]}
