"""Versioned MEXC depth with fail-closed sequence and snapshot-boundary checks."""
from decimal import Decimal, InvalidOperation

from .models import normalize


class BookGap(ValueError):
    pass


def amount(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise BookGap("Invalid depth number") from None
    if not result.is_finite():
        raise BookGap("Nonfinite depth number")
    return result


class MexcBook:
    def __init__(self):
        self.version = None
        self.bids, self.asks = {}, {}
        self.bid_boundary = self.ask_boundary = None

    @staticmethod
    def levels(rows):
        result = {}
        for row in rows:
            if len(row) < 2:
                raise BookGap("Incomplete depth level")
            price, size = amount(row[0]), amount(row[1])
            if price <= 0 or size < 0:
                raise BookGap("Invalid depth level")
            if size:
                result[price] = size
        return result

    def initialize(self, data):
        self.bids = self.levels(data["bids"])
        self.asks = self.levels(data["asks"])
        if not self.bids or not self.asks:
            raise BookGap("Empty REST depth snapshot")
        self.version = int(data["version"])
        self.bid_boundary = min(self.bids)
        self.ask_boundary = max(self.asks)

    def apply(self, message, market, received_at_ms):
        if self.version is None:
            raise BookGap("Snapshot required")
        data = message["data"]
        try:
            end = int(data.get("end", data.get("version")))
            begin = int(data.get("begin", end))
        except (TypeError, ValueError):
            raise BookGap("Missing or invalid depth version") from None
        if begin > end or int(data.get("version", end)) != end:
            raise BookGap("Invalid version range")
        if end <= self.version:
            return None  # Duplicate/old commits must not refresh the quote timestamp.
        if not begin <= self.version + 1 <= end:
            raise BookGap(f"Depth gap: local={self.version}, incoming={begin}..{end}")
        for side, rows in ((self.bids, data.get("bids", [])), (self.asks, data.get("asks", []))):
            for row in rows:
                price, size = amount(row[0]), amount(row[1])
                if price <= 0 or size < 0:
                    raise BookGap("Invalid depth update")
                if size == 0:
                    side.pop(price, None)
                else:
                    side[price] = size
        self.version = end
        if not self.bids or not self.asks:
            raise BookGap("Depth side exhausted")
        bid, ask = max(self.bids), min(self.asks)
        if bid < self.bid_boundary or ask > self.ask_boundary:
            raise BookGap("Best level outside initialized snapshot coverage")
        if bid >= ask:
            raise BookGap("Crossed or locked local depth")
        if len(self.bids) + len(self.asks) > 20000:
            raise BookGap("Depth cache reached resnapshot limit")
        ticker = {"bid": float(bid), "ask": float(ask),
                  "timestamp": data.get("cts") or data.get("ts") or message.get("ts")}
        return normalize("mexc", market, ticker, received_at_ms, "ws_depth_verified")
