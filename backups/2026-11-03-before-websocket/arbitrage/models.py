from dataclasses import dataclass
import math


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def is_target(market):
    return (market.get("swap") is True and market.get("linear") is True
            and market.get("quote") == "USDT" and market.get("settle") == "USDT"
            and market.get("active") is True)


@dataclass(frozen=True)
class Quote:
    exchange: str
    symbol: str
    exchange_symbol: str
    base: str
    quote: str
    contract_size: float | None
    bid: float | None
    ask: float | None
    volume_24h_usdt: float | None
    source_timestamp_ms: int | None
    received_at_ms: int
    source: str

    def quality(self, now_ms, max_age_ms=3000):
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= 0:
            return "missing_bid_ask"
        if self.bid > self.ask:
            return "crossed_book"
        if self.source_timestamp_ms is None:
            return "unknown_source_time"
        age = now_ms - self.source_timestamp_ms
        if age < -1000:
            return "future_source_time"
        if age > max_age_ms or now_ms - self.received_at_ms > max_age_ms:
            return "stale"
        return "ok"


def normalize(exchange, market, ticker, received_at_ms, source="ticker"):
    timestamp = number(ticker.get("timestamp"))
    return Quote(
        exchange, market["symbol"], market["id"], market["base"], market["quote"],
        number(market.get("contractSize")), number(ticker.get("bid")),
        number(ticker.get("ask")), number(ticker.get("quoteVolume")),
        int(timestamp) if timestamp is not None and timestamp > 0 else None,
        received_at_ms, source,
    )
