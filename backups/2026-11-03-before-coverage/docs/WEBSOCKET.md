# WebSocket: протоколы и границы

Проверено 2026-11-03. Native JSON через aiohttp; CCXT используется для списка контрактов.
Работают независимые задачи бирж, каждая загружает метаданные и подключается, не ожидая остальных.
Снимки пишутся отдельно от сетевых задач, по умолчанию раз в секунду в JSON и SQLite.

| Биржа | Канал | Контроль времени |
|---|---|---|
| Binance USD-M | bookTicker через /public/ws | T, иначе E |
| Bybit linear | orderbook.1, только snapshot | cts, иначе ts |
| OKX | bbo-tbt через порт 443 | ts строки данных |
| Bitget v3 | books1, только snapshot | ts строки данных |
| Gate USDT | futures.book_ticker | result.t |
| MEXC | sub.ticker | timestamp сделки; свежесть стакана не подтверждена |

При пустой стороне или нулевом размере лучшего уровня цена исключается.
Неожиданная delta в snapshot-канале вызывает переподключение, чтобы не смешивать протоколы.
Pong/subscribe ack не обновляют котировки. При отключении статус котировок сразу меняется на
disconnected; новая сессия очищает кэш до первого свежего снимка. Старые по исходному времени
обновления в рамках сессии игнорируются. Это не полная проверка порядковых номеров стакана:
выбранные BBO-каналы передают полный верхний уровень, локальный стакан из дельт не строится.

Heartbeat приложений — каждые 15 секунд, protocol ping/pong также обрабатывается aiohttp.
Нет market data 30 секунд → переподключение с паузой 1, 2, 4… максимум 30 секунд и jitter.
Текущие лимиты WS — до 20 базовых активов на биржу; подписки ALL пока не реализованы.
Метаданные загружаются при старте, их периодическое обновление и ротация истории впереди.
24h volume из REST не переносится в WS без собственного контроля возраста; null не означает нулевой объём.
Синхронизацию часов ОС не заменяем: отрицательный возраст более 1 секунды исключает котировку.

MEXC намеренно подключён в диагностическом режиме. По документации timestamp ticker относится
к сделке; он не доказывает время обновления best bid/ask. Следующий шаг для этой биржи —
полный стакан с REST-снимком и проверкой диапазонов begin/end. Инкременты sub.depth нельзя
принимать за полные snapshots.

Официальные источники:

- [Binance: маршрутизация WebSocket](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Important-WebSocket-Change-Notice)
- [Bybit: level 1 snapshots](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook)
- [Bybit: подключения](https://bybit-exchange.github.io/docs/v5/ws/connect)
- [OKX: order book channels](https://www.okx.com/docs-v5/en/#order-book-trading-market-data-ws-order-book-channel)
- [Bitget: books1](https://www.bitget.com/docs/uta/websocket/public/Order-Book-Channel)
- [Bitget: подключения и heartbeat](https://www.bitget.com/docs/uta/quick-start)
- [Gate: futures.book_ticker](https://www.gate.com/docs/developers/futures/ws/en/#best-ask-bid-subscription)
- [MEXC: single ticker](https://www.mexc.com/api-docs/futures/websocket-api/get-a-single-ticker)
- [MEXC: JSON endpoint](https://www.mexc.com/api-docs/futures/websocket-api/native-ws-endpoint)
- [MEXC: heartbeat](https://www.mexc.com/api-docs/futures/websocket-api/command-details-for-data-exchange)
