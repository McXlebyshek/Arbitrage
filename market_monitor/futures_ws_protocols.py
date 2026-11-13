"""Public futures BBO wire formats. Prices stay in each market's native quote unit."""

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any, NamedTuple

from .market import now_ms, numeric

JsonObject = dict[str, Any]


class BookUpdate(NamedTuple):
    symbol: str | None
    timestamp: int | None
    sequence: int | None
    bid: float | None
    ask: float | None
    reset: bool = False


@dataclass(frozen=True)
class BookProtocol:
    url: str
    commands: Callable[[list[str], str], Iterable[JsonObject]]
    updates: Callable[[JsonObject], Iterable[BookUpdate]]
    ping: Callable[[], JsonObject | str] | None = None
    ping_interval: int = 20
    decimal_sizes: bool = False
    repeated_snapshots: bool = False
    request_id: Callable[[int], int | str] | None = None
    subscription_ack: Callable[[JsonObject, str], bool] | None = None

    def subscribe(self, symbols: list[str]) -> Iterable[JsonObject]:
        return self.commands(symbols, 'subscribe')

    def unsubscribe(self, symbols: list[str]) -> Iterable[JsonObject]:
        return self.commands(symbols, 'unsubscribe')


def integer(value: Any) -> int | None:
    if type(value) is int:
        return value
    if isinstance(value, str) and len(value) <= 24 and value.isascii() and value.isdecimal():
        return int(value)
    return None


def sized_price(price: Any, size: Any) -> float | None:
    return numeric(price) if (numeric(size) or 0) > 0 else None


def best(levels: Any, choose: Callable[..., Any]) -> float | None:
    if not isinstance(levels, list):
        return None
    prices = (sized_price(level[0], level[1]) for level in levels
              if isinstance(level, list) and len(level) >= 2)
    return choose((price for price in prices if price is not None and price > 0), default=None)


def depth_update(symbol: Any, timestamp: Any, sequence: Any, bids: Any, asks: Any,
                 *, reset: bool = False) -> BookUpdate:
    return BookUpdate(symbol, integer(timestamp), integer(sequence), best(bids, max), best(asks, min), reset)


def mexc_updates(message: JsonObject) -> Iterator[BookUpdate]:
    data = message.get('data')
    if message.get('channel') == 'push.depth.full' and isinstance(data, dict):
        yield depth_update(message.get('symbol'), message.get('ts'), data.get('version'),
                           data.get('bids'), data.get('asks'))


def kucoin_updates(message: JsonObject) -> Iterator[BookUpdate]:
    data = message.get('data')
    if message.get('subject') == 'tickerV2' and isinstance(data, dict):
        ns = integer(data.get('ts'))
        yield BookUpdate(data.get('symbol'), ns // 1_000_000 if ns is not None else None,
                         integer(data.get('sequence')), sized_price(data.get('bestBidPrice'), data.get('bestBidSize')),
                         sized_price(data.get('bestAskPrice'), data.get('bestAskSize')))


def binance_updates(message: JsonObject) -> Iterator[BookUpdate]:
    data = message.get('data', message)
    if (isinstance(data, dict) and data.get('e') == 'bookTicker'
            and ('st' not in data or integer(data['st']) == 1)):
        yield BookUpdate(data.get('s'), integer(data.get('T')), integer(data.get('u')),
                         sized_price(data.get('b'), data.get('B')), sized_price(data.get('a'), data.get('A')))


def bybit_updates(message: JsonObject) -> Iterator[BookUpdate]:
    data = message.get('data')
    if message.get('type') != 'snapshot' or not isinstance(data, dict):
        return
    symbol = data.get('s')
    if isinstance(symbol, str) and message.get('topic') == f'orderbook.1.{symbol}':
        update_id = integer(data.get('u'))
        yield depth_update(symbol, message.get('cts'), update_id, data.get('b'), data.get('a'), reset=update_id == 1)


def okx_updates(message: JsonObject) -> Iterator[BookUpdate]:
    arg, data = message.get('arg'), message.get('data')
    if (not isinstance(arg, dict) or arg.get('channel') != 'bbo-tbt'
            or message.get('action', 'snapshot') != 'snapshot' or not isinstance(data, list)):
        return
    for row in data:
        if isinstance(row, dict):
            yield depth_update(arg.get('instId'), row.get('ts'), row.get('seqId'), row.get('bids'), row.get('asks'))


def gate_updates(message: JsonObject) -> Iterator[BookUpdate]:
    row = message.get('result')
    if message.get('channel') == 'futures.book_ticker' and message.get('event') == 'update' and isinstance(row, dict):
        yield BookUpdate(row.get('s'), integer(row.get('t')), integer(row.get('u')),
                         sized_price(row.get('b'), row.get('B')), sized_price(row.get('a'), row.get('A')))


def bitget_updates(message: JsonObject) -> Iterator[BookUpdate]:
    arg, data = message.get('arg'), message.get('data')
    if (not isinstance(arg, dict) or arg.get('instType') != 'usdt-futures' or arg.get('topic') != 'books1'
            or message.get('action') != 'snapshot' or not isinstance(data, list)):
        return
    for row in data:
        if isinstance(row, dict):
            yield depth_update(arg.get('symbol'), row.get('ts'), row.get('seq'), row.get('b'), row.get('a'),
                               reset=integer(row.get('pseq')) == 0)


PROTOCOLS = {
    'mexc': BookProtocol(
        'wss://contract.mexc.com/edge',
        lambda symbols, op: (
            dict(method='sub.depth.full', param=dict(symbol=symbol, limit=5)) if op == 'subscribe'
            else dict(method='unsub.depth.full', param=dict(symbol=symbol))
            for symbol in symbols
        ),
        mexc_updates, ping=lambda: dict(method='ping'), ping_interval=14, repeated_snapshots=True,
    ),
    'kucoinfutures': BookProtocol(
        '',
        lambda symbols, op: (dict(type=op, topic=f'/contractMarket/tickerV2:{symbol}',
                                  privateChannel=False, response=True) for symbol in symbols),
        kucoin_updates, ping=lambda: dict(id=str(now_ms()), type='ping'), request_id=str,
    ),
    'binanceusdm': BookProtocol(
        'wss://fstream.binance.com/public/ws',
        lambda symbols, op: (dict(method=op.upper(), params=[f'{symbol.lower()}@bookTicker' for symbol in symbols]),),
        binance_updates, request_id=int,
        subscription_ack=lambda message, op: 'result' in message and message['result'] is None,
    ),
    'bybit': BookProtocol(
        'wss://stream.bybit.com/v5/public/linear',
        lambda symbols, op: (dict(op=op, args=[f'orderbook.1.{symbol}' for symbol in symbols]),),
        bybit_updates, ping=lambda: dict(op='ping'), repeated_snapshots=True,
    ),
    'okx': BookProtocol(
        'wss://ws.okx.com/ws/v5/public',
        lambda symbols, op: (dict(op=op, args=[dict(channel='bbo-tbt', instId=symbol) for symbol in symbols]),),
        okx_updates, ping=lambda: 'ping', repeated_snapshots=True,
    ),
    'gate': BookProtocol(
        'wss://fx-ws.gateio.ws/v4/ws/usdt',
        lambda symbols, op: (dict(time=now_ms() // 1000, channel='futures.book_ticker', event=op, payload=symbols),),
        gate_updates, decimal_sizes=True, request_id=int,
        subscription_ack=lambda message, op: message.get('channel') == 'futures.book_ticker'
        and message.get('event') == op and isinstance(message.get('result'), dict)
        and message['result'].get('status') == 'success',
    ),
    'bitget': BookProtocol(
        'wss://ws.bitget.com/v3/ws/public',
        lambda symbols, op: (dict(op=op, args=[dict(instType='usdt-futures', topic='books1', symbol=symbol)
                                              for symbol in symbols]),),
        bitget_updates, ping=lambda: 'ping', ping_interval=30, repeated_snapshots=True,
    ),
}
