import gzip
import json
import unittest

from arbitrage.streams import decode, parse, subscriptions
from arbitrage.live import LiveExchange


def market(symbol):
    return dict(symbol="BTC/USDT:USDT", id=symbol, base="BTC", quote="USDT", contractSize=1)


class ProtocolTests(unittest.TestCase):
    def quote(self, exchange, symbol, message):
        return parse(exchange, message, {symbol: market(symbol)}, 10100)[0]

    def test_binance_native_snapshot(self):
        quote = self.quote("binanceusdm", "BTCUSDT", dict(e="bookTicker", s="BTCUSDT", b="100", a="101", B="2", A="3", T=10000))
        self.assertEqual((quote.bid, quote.ask, quote.source_timestamp_ms), (100, 101, 10000))
        self.assertEqual(quote.quality(10100), "ok")

    def test_bybit_snapshot_and_empty_side(self):
        message = dict(topic="orderbook.1.BTCUSDT", type="snapshot", ts=10000,
                       data=dict(s="BTCUSDT", b=[[100, 1]], a=[]))
        quote = self.quote("bybit", "BTCUSDT", message)
        self.assertEqual(quote.quality(10100), "missing_bid_ask")
        message["type"] = "delta"
        with self.assertRaises(ValueError):
            self.quote("bybit", "BTCUSDT", message)

    def test_okx_complete_snapshots(self):
        message = dict(arg=dict(channel="bbo-tbt", instId="BTC-USDT-SWAP"),
                       data=[dict(bids=[[100, 1]], asks=[[101, 1]], ts="10000")])
        self.assertEqual(self.quote("okx", "BTC-USDT-SWAP", message).quality(10100), "ok")
        message["data"][0]["bids"] = []
        self.assertIsNone(self.quote("okx", "BTC-USDT-SWAP", message).bid)

    def test_bitget_v3_books1(self):
        message = dict(arg=dict(topic="books1", symbol="BTCUSDT"), action="snapshot",
                       data=[dict(b=[[100, 2]], a=[[101, 1]], ts="10000")])
        self.assertEqual(self.quote("bitget", "BTCUSDT", message).quality(10100), "ok")
        message["action"] = "update"
        with self.assertRaises(ValueError):
            self.quote("bitget", "BTCUSDT", message)

    def test_gate_bbo_timestamp(self):
        message = dict(channel="futures.book_ticker", event="update",
                       result=dict(s="BTC_USDT", b="100", a="101", B="1", A="1", t=10000))
        self.assertEqual(self.quote("gate", "BTC_USDT", message).quality(10100), "ok")
        message["result"]["B"] = "0"
        self.assertIsNone(self.quote("gate", "BTC_USDT", message).bid)

    def test_mexc_trade_time_is_not_book_time(self):
        message = dict(channel="push.ticker", symbol="BTC_USDT",
                       data=dict(bid1=100, ask1=101, timestamp=10000, amount24=2000000))
        quote = self.quote("mexc", "BTC_USDT", message)
        self.assertEqual(quote.quality(10100), "unverified_book_time")
        self.assertEqual(quote.volume_24h_usdt, 2000000)

    def test_control_messages_do_not_produce_quotes(self):
        for exchange in ("binanceusdm", "bybit", "okx", "bitget", "gate", "mexc"):
            self.assertEqual(parse(exchange, {"event": "subscribe"}, {}, 10100), [])
        self.assertIsNone(decode("pong"))
        self.assertEqual(decode('{"event":"notice","code":"64010"}')["event"], "notice")
        for payload in [{"success": False}, {"event": "error"}, {"code": 30001}, {"channel": "rs.error"}]:
            with self.assertRaises(ValueError):
                decode(json.dumps(payload))

    def test_binary_gzip(self):
        payload = {"channel": "pong", "data": 10000}
        self.assertEqual(decode(gzip.compress(json.dumps(payload).encode())), payload)

    def test_disconnection_immediately_invalidates_quotes(self):
        worker = LiveExchange("gate", {"BTC"})
        quote = self.quote("gate", "BTC_USDT", dict(channel="futures.book_ticker", event="update",
                           result=dict(s="BTC_USDT", b="100", a="101", t=10000)))
        worker.quotes[quote.symbol] = quote
        worker.state = "streaming"
        self.assertEqual(worker.snapshot(10100)["status"]["usable_quotes"], 1)
        worker.state = "reconnecting"
        snapshot = worker.snapshot(10100)
        self.assertEqual(snapshot["status"]["usable_quotes"], 0)
        self.assertEqual(snapshot["quotes"][0]["quality"], "disconnected")

    def test_subscriptions_are_public(self):
        self.assertEqual(subscriptions("bybit", ["BTCUSDT"])[0]["args"], ["orderbook.1.BTCUSDT"])
        self.assertEqual(subscriptions("bitget", ["BTCUSDT"])[0]["args"][0]["topic"], "books1")


if __name__ == "__main__":
    unittest.main()
