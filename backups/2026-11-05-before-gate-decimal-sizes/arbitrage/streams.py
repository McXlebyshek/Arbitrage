"""Native public WS protocols; incremental MEXC depth is handled by MexcBook."""
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


BINANCE_STREAMS = ('bookTicker', 'bookTicker-combined', 'depth5@100ms', 'depth5@500ms')
BINANCE_COMBINED_URL = 'wss://fstream.binance.com/public/stream'


def subscriptions(exchange, symbols, binance_stream='bookTicker'):
    if exchange == "binanceusdm":
        if binance_stream not in BINANCE_STREAMS: raise ValueError('unsupported_binance_stream')
        topic='bookTicker' if binance_stream=='bookTicker-combined' else binance_stream
        return [{"method": "SUBSCRIBE", "params": [f"{s.lower()}@{topic}" for s in symbols], "id": 1}]
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
    return [{"method": "sub.depth", "param": {"symbol": s}, "gzip": False} for s in symbols]


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


def partial_top(levels, bids=False):
    if not isinstance(levels,list) or len(levels)>5: raise ValueError('invalid_partial_depth_levels')
    previous=None
    for level in levels:
        if not isinstance(level,list) or len(level)<2: raise ValueError('invalid_partial_depth_level')
        price,size=number(level[0]),number(level[1])
        if price is None or size is None or price<=0 or size<=0: return None
        if previous is not None and (price>=previous if bids else price<=previous):
            raise ValueError('unordered_partial_depth')
        previous=price
    return levels[0][0] if levels else None


def parse(exchange, message, markets, received_at_ms, binance_stream='bookTicker'):
    """Return complete normalized snapshots; control messages never refresh quotes."""
    data = message.get("data", {})
    symbol, bid, ask, stamp, volume = None, None, None, None, None
    source = "ws_bbo"
    if exchange=='binanceusdm' and binance_stream!='bookTicker':
        if binance_stream not in BINANCE_STREAMS: raise ValueError('unsupported_binance_stream')
        if 'stream' not in message:
            if 'e' in message: raise ValueError('partial_depth_requires_topic_envelope')
            return []
        row=message.get('data')
        if not isinstance(row,dict): raise ValueError('invalid_partial_depth_payload')
        symbol=row.get('s')
        topic='bookTicker' if binance_stream=='bookTicker-combined' else binance_stream
        if not isinstance(symbol,str) or message['stream']!=f'{symbol.lower()}@{topic}':
            raise ValueError('unexpected_binance_depth_topic')
        if row.get('st') not in (None,1):
            raise ValueError('unexpected_binance_depth_type')
        if symbol not in markets: raise ValueError('unsubscribed_partial_depth_symbol')
        if type(row.get('u')) is not int or row['u']<0:raise ValueError('invalid_binance_update_id')
        if binance_stream=='bookTicker-combined':
            if row.get('e')!='bookTicker':raise ValueError('unexpected_binance_book_type')
            return parse(exchange,row,markets,received_at_ms)
        if row.get('e')!='depthUpdate':raise ValueError('unexpected_binance_depth_type')
        bid,ask=partial_top(row.get('b'),True),partial_top(row.get('a'))
        stamp=row.get('T') or row.get('E')
    elif exchange == "binanceusdm" and message.get("e") == "bookTicker":
        if message.get('st') not in (None,1):raise ValueError('unexpected_binance_book_type')
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
