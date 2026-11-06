"""Gate futures.obu snapshot and contiguous incremental book."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


def positive_id(row, key):
    value = row.get(key)
    if type(value) is not int or value <= 0:
        raise ValueError(f"invalid {key}")
    return value


def amount(raw):
    if not isinstance(raw, (str, int)) or isinstance(raw, bool):
        raise ValueError("invalid decimal type")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    if not value.is_finite() or value < 0:
        raise ValueError("invalid decimal")
    return value


@dataclass(frozen=True)
class Top:
    uid: int
    stamp: int
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal


class Book:
    def __init__(self):
        self.uid = None
        self.bids = {}
        self.asks = {}

    def apply(self, row):
        try:
            return self._apply(row)
        except (ValueError, TypeError):
            self.__init__()
            raise

    def _apply(self, row):
        uid = positive_id(row, "u")
        stamp = positive_id(row, "t")
        if type(row.get("full", False)) is not bool:
            raise ValueError("invalid full flag")
        if row.get("full"):
            if self.uid is not None and uid < self.uid:
                raise ValueError("OBU snapshot ID regressed")
            self.bids.clear()
            self.asks.clear()
        else:
            first = positive_id(row, "U")
            if self.uid is None or first != self.uid + 1 or uid < first:
                raise ValueError("sequence gap")
        for key, side in (("b", self.bids), ("a", self.asks)):
            levels = row.get(key, [])
            if not isinstance(levels, list):
                raise ValueError("invalid side")
            for level in levels:
                if not isinstance(level, list) or len(level) != 2:
                    raise ValueError("invalid level")
                price, size = map(amount, level)
                if not price:
                    raise ValueError("invalid price")
                if size:
                    side[price] = size
                else:
                    side.pop(price, None)
        if len(self.bids) + len(self.asks) > 20000:
            raise ValueError("OBU cache reached resnapshot limit")
        if not self.bids or not self.asks:
            raise ValueError("empty or crossed book")
        bid, ask = max(self.bids), min(self.asks)
        if bid >= ask:
            raise ValueError("empty or crossed book")
        self.uid = uid
        return Top(uid, stamp, bid, ask, self.bids[bid], self.asks[ask])
