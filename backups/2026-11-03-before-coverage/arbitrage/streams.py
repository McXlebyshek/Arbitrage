"""Native public WS protocols. Only complete BBO snapshots are accepted."""
import gzip
import json
import time

from .models import normalize, number

URLS = {
    "binanceusdm": "wss://fstream.binance.com/public/ws",
    "bybit": "wss://stream.bybit.com/v5/public/linear",
    "okx": "wss://ws.okx.com/ws/v5/public",
    "bitget": "wss://ws.bitget.com/v3/ws/public",
    "gate": "wss://fx-ws.gateio.ws/v4/ws/usdt",
    "mexc": "wss://contract.mexc.com/edge",
}


def subscriptions(exchange, symbols):
    if exchange == "binanceusdm":
        return [{"method": "SUBSCRIBE", "params": [f"{s.lower()}@bookTicker" for s in symbols], "id": 1}]
    if exchange == "bybit":
        return [{"op": "subscribe", "args": [f"orderbook.1.{s}" for s in symbols]}]
    if exchange == "okx":
        return [{"op": "subscribe", "args": [{"channel": "bbo-tbt", "instId": s} for s in symbols]}]
    if exchange == "bitget":
        return [{"op": "subscribe", "args": [
            {"instType": "usdt-futures", "topic": "books1", "symbol": s} for s in symbols]}]
    if exchange == "gate":
        return [{"time": int(time.time()), "channel": "futures.book_ticker",
                 "event": "subscribe", "payload": list(symbols)}]
    return [{"method": "sub.ticker", "param": {"symbol": s}, "gzip": False} for s in symbols]


def heartbeat(exchange):
    if exchange in ("okx", "bitget"):
        return "ping"
    if exchange == "bybit":
        return {"op": "ping"}
    if exchange == "mexc":
        return {"method": "ping"}
    if exchange == "gate":
        return {"time": int(time.time()), "channel": "futures.ping"}
    return None  # aiohttp responds to Binance protocol ping frames automatically.


def decode(raw):
    if isinstance(raw, bytes):
        raw = gzip.decompress(raw).decode() if raw[:2] == b"\x1f\x8b" else raw.decode()
    if raw in ("pong", "ping"):
        return None
    message = json.loads(raw)
    if not isinstance(message, dict):
        raise ValueError("Expected a WS JSON object")
    if (message.get("event") == "error" or message.get("success") is False
            or message.get("error") or message.get("channel") == "rs.error"
            or (message.get("event") != "notice" and message.get("code") not in (None, 0, "0"))):
        raise ValueError(f"WS rejected message: {message}")
    return message


def top(levels):
    """An empty or zero-size best level must invalidate the price."""
    if not levels or len(levels[0]) < 2:
        return None
    size = number(levels[0][1])
    return levels[0][0] if size is not None and size > 0 else None


def parse(exchange, message, markets, received_at_ms):
    """Return complete normalized snapshots; control messages never refresh quotes."""
    data = message.get("data", {})
    symbol, bid, ask, stamp, volume = None, None, None, None, None
    source = "ws_bbo"
    if exchange == "binanceusdm" and message.get("e") == "bookTicker":
        symbol = message.get("s")
        bid, ask = message.get("b"), message.get("a")
        stamp = message.get("T") or message.get("E")
        if number(message.get("B")) == 0:
            bid = None
        if number(message.get("A")) == 0:
            ask = None
    elif exchange == "bybit" and message.get("topic", "").startswith("orderbook.1."):
        if message.get("type") != "snapshot":
            raise ValueError("Unexpected delta on snapshot-only Bybit level 1")
        symbol = data.get("s")
        bid, ask = top(data.get("b")), top(data.get("a"))
        stamp = message.get("cts") or message.get("ts")
    elif exchange in ("okx", "bitget") and isinstance(data, list):
        argument = message.get("arg", {})
        channel = argument.get("channel") if exchange == "okx" else argument.get("topic")
        if channel != ("bbo-tbt" if exchange == "okx" else "books1"):
            return []
        if message.get("action") not in (None, "snapshot"):
            raise ValueError("Unexpected incremental update on snapshot-only channel")
        symbol = argument.get("instId") if exchange == "okx" else argument.get("symbol")
        quotes = []
        for row in data:
            bids, asks = ("bids", "asks") if exchange == "okx" else ("b", "a")
            ticker = {"bid": top(row.get(bids)), "ask": top(row.get(asks)),
                      "timestamp": row.get("ts")}
            if symbol in markets:
                quotes.append(normalize(exchange, markets[symbol], ticker, received_at_ms, source))
        return quotes
    elif exchange == "gate" and message.get("channel") == "futures.book_ticker" and message.get("event") == "update":
        row = message["result"]
        symbol, bid, ask, stamp = row.get("s"), row.get("b"), row.get("a"), row.get("t")
        if number(row.get("B")) == 0:
            bid = None
        if number(row.get("A")) == 0:
            ask = None
    elif exchange == "mexc" and message.get("channel") == "push.ticker":
        symbol = message.get("symbol") or data.get("symbol")
        bid, ask = data.get("bid1"), data.get("ask1")
        stamp, volume = data.get("timestamp"), data.get("amount24")
        # MEXC documents timestamp as trade time, not book generation time.
        source = "ws_ticker_unverified_book_time"
    if symbol not in markets:
        return []
    return [normalize(exchange, markets[symbol], {"bid": bid, "ask": ask, "timestamp": stamp,
                      "quoteVolume": volume}, received_at_ms, source)]
