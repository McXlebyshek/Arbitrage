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
| MEXC | sub.depth + REST-снимок 1000 уровней | cts, иначе системное время сообщения; проверяются begin/end/version |

При пустой стороне или нулевом размере лучшего уровня цена исключается.
Неожиданная delta в snapshot-канале вызывает переподключение, чтобы не смешивать протоколы.
Pong/subscribe ack не обновляют котировки. При отключении статус котировок сразу меняется на
disconnected; новая сессия очищает кэш до первого свежего снимка. Старые по исходному времени
обновления в рамках сессии игнорируются. Это не полная проверка порядковых номеров стакана:
выбранные BBO-каналы передают полный верхний уровень, локальный стакан из дельт не строится.

Heartbeat приложений — каждые 15 секунд, protocol ping/pong также обрабатывается aiohttp.
Нет market data 30 секунд → переподключение с паузой 1, 2, 4… максимум 30 секунд и jitter.
С версии 0.4 работают группы: до 20 рынков на соединение, MEXC — до 5.
По умолчанию до 8 соединений на биржу; общий темп новых соединений и переподключений
одной биржи ограничен интервалом не менее 1,1 секунды. Это бюджет текущего процесса;
другие приложения на том же IP в него не входят.
Метаданные обновляются каждые 900 секунд, изменившиеся группы перезапускаются.
При ошибках старые метаданные используются не дольше двух интервалов обновления.
Подписки ALL ограничиваются выбранной ёмкостью; пропуски учитываются явно.
Подробнее: [COVERAGE.md](COVERAGE.md). Ротация истории пока не реализована.
24h volume из REST не переносится в WS без собственного контроля возраста; null не означает нулевой объём.
Синхронизацию часов ОС не заменяем: отрицательный возраст более 1 секунды исключает котировку.

MEXC с версии 0.4 использует локальный стакан. Сначала поступает WS-обновление,
затем загружается REST-снимок; уже буферизуемый WS-поток должен продолжить его последовательность.
Старые версии пропускаются без обновления времени котировки; новый диапазон должен содержать
localVersion+1. При пропуске котировка удаляется до ожидания нового REST-снимка.
Сам REST-снимок не даёт статуса свежего WS-стакана: нужно последующее непрерывное обновление.
Размеры в приращениях абсолютные, ноль удаляет уровень. Лучшие цены должны оставаться
в области, покрытой исходным снимком; иначе выполняется повторная синхронизация.
Общий интервал REST-снимков всех групп MEXC — не менее 0,3 секунды.
По умолчанию лимит 20 локальных стаканов, максимум настройки 40. Это не VWAP и не проверка размера сделки.

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
- [MEXC: последовательность стакана и восстановление](https://www.mexc.com/api-docs/futures/websocket-api/incremental-order-book-maintenance-mechanism)
