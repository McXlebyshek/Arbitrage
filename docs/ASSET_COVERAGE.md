# Покрытие капитализации и FDV

## Реестр от 11 ноября 2026

157 спотовых рынков (Binance 40, Bybit 52, OKX 65) и 29 контрактов OKX расширили реестр с 30 до 59 активов. Для спота CoinGecko указывает `coin_id` конкретной пары, идентификатор биржи и URL её торговой страницы. Все три поля сверены с активными рынками публичных каталогов. Валюта котировки также сверена по `target_coin_id`. Пропущены аномальные, устаревшие и неоднозначные записи. Для новых контрактов OKX каталог указывает тот же базовый индекс (`uly`, `instFamily`) и валюту номинала `ctValCcy`. На второй бирже подтверждён тот же CoinGecko ID спотовой пары.

| Биржа | Привязки CoinGecko | Публичный каталог |
| --- | --- | --- |
| Binance | [Тикеры Binance](https://api.coingecko.com/api/v3/exchanges/binance/tickers?depth=false) | [Spot exchangeInfo](https://api.binance.com/api/v3/exchangeInfo?symbolStatus=TRADING) |
| Bybit | [Тикеры Bybit Spot](https://api.coingecko.com/api/v3/exchanges/bybit_spot/tickers?depth=false) | [Spot instruments-info](https://api.bybit.com/v5/market/instruments-info?category=spot) |
| OKX | [Тикеры OKX](https://api.coingecko.com/api/v3/exchanges/okex/tickers?depth=false) | [Spot instruments](https://www.okx.com/api/v5/public/instruments?instType=SPOT) · [Swap instruments](https://www.okx.com/api/v5/public/instruments?instType=SWAP) |

Ключ записи включает биржу, тип и нативный идентификатор рынка. При чтении каталога проверяются активность, базовая валюта и валюта котировки. Для бессрочных контрактов дополнительно проверяются линейность, расчётная валюта и размер контракта. Значения капитализации и FDV доступны связке при совпадении подтверждённого актива обеих ног.

[Снимок привязок и оценок 59 активов](evidence/registry-2026-11-11.json)

## Покрытие бирж

В режиме «Лонг + шорт · спред» в меню UAInvest доступны 20 площадок. Общие 12 — Aster, Binance, Bitget, Bybit, EdgeX, Extended, Gate, HTX, KuCoin, MEXC, OKX и WhiteBIT. Только в этом меню UAInvest присутствуют Arcus, BinanceDEX, BybitFi, GateFi, Hyperliquid, Kraken, Nado и Robinlighter. На момент первой проверки все 12 локальных источников swap и девять spot ответили со статусом `ok`. EdgeX и Extended локально нормализованы как USDC, поэтому при фильтре USDT их локальные рынки не попадают в таблицу. [Снимок меню и локальных источников](evidence/coverage-2026-11-11.json)

## Базовое сравнение

11 ноября в 11:18:22 по Киеву Playwright зафиксировал ответ UAInvest и локальный снимок с разницей меток публикации 7,014 с. Обе стороны использовали общие 12 площадок, USDT, категорию Crypto, группировку, Open Spread по убыванию и оборот от 1 млн на каждой ноге. Все 50 публичных строк прошли проверку по двум полям `24usdt`. Локальные строки построены штатной фильтрацией браузера на сохранённом ответе `/api/markets`. [Выдача UAInvest](https://uainvest.com.ua/arbitrage?type=spread&exchanges=aster_binance_bitget_bybit_edgex_extended_gate_htx_kucoin_mexc_okx_whitebit&volume=1&quotes=USDT&category=crypto&group=1&sort_by=open_spread&sort_dir=desc) · [Снимок Playwright](evidence/spread-2026-11-11.json)

| Локальный отбор | Локальных групп | Совпали токены в топ-50 | Совпали направленные связки в топ-50 |
| --- | ---: | ---: | ---: |
| Условия базового сравнения | 258 | 21 | 13 |
| Дополнительно «Сверка с UAInvest», спред ≥ 0,5% или APR ≥ 50% | 46 | 35 | 23 |

Во всей базовой локальной выдаче присутствуют 36 из 50 публичных монет. 35 точных направленных связок прошли локальные фильтры до группировки. Для них медиана абсолютной разницы Open Spread составила 0,050 п.п., максимум 3,237 п.п. у AGT Gate → MEXC (2,246% и −0,991%). Метки публикации характеризуют снимки, а цены в публичном списке не имеют отдельных меток Bid/Ask.

### Строки UAInvest, которые не совпали с локальным топ-50

Локальный ранг относится к монете после группировки. Направление указано от лонга к шорту. Для отсутствующих ног проверены публичные каталоги бирж. [Ответы каталогов](evidence/missing-catalogs-2026-11-11.json)

| Монета | Связка UAInvest | Локальная группа | Проверка точной связки |
| --- | --- | --- | --- |
| INIT | Gate → KuCoin | 1, Bitget → KuCoin | связка прошла фильтры (+1.460%) |
| AGT | Gate → MEXC | 2, KuCoin → Gate | связка прошла фильтры (-0.991%) |
| BTW | Bitget → Gate | 18, Bitget → Bybit | связка прошла фильтры (+0.240%) |
| AERO | Binance → Bitget | 45, Bybit → Bitget | связка прошла фильтры (+0.148%) |
| SUI | KuCoin → Aster | 110, OKX → Gate | связка прошла фильтры (+0.024%) |
| Q | Gate → Binance | 53, Gate → Binance | связка прошла фильтры (+0.149%) |
| XOM | Bybit → Bitget | Отсутствует | Bybit `symbolType=stock` и Bitget `isRwa=YES` |
| AMD | EdgeX → KuCoin | Отсутствует | KuCoin `assetClass=STOCK`; EdgeX в локальном адаптере использует USDC |
| SKDD | Bitget → KuCoin | Отсутствует | Bitget `isRwa=YES` и KuCoin `assetClass=STOCK` |
| UAI | Gate → MEXC | 133, Bybit → Binance | связка прошла фильтры (-0.026%) |
| MERL | Bybit → MEXC | 117, Bybit → Binance | связка прошла фильтры (+0.033%) |
| SECZ | Bitget → Bybit | Отсутствует | Bitget `isRwa=YES` и Bybit `symbolType=stock` |
| MSTR | EdgeX → Gate | Отсутствует | Gate `contract_type=stocks`; EdgeX в локальном адаптере использует USDC |
| DOT | Bybit → MEXC | 25, WhiteBIT → Gate | связка прошла фильтры (+0.036%) |
| COMP | Bybit → Binance | 223, Bybit → Binance | связка прошла фильтры (-0.050%) |
| CORE | Bybit → OKX | 200, Bybit → OKX | связка прошла фильтры (+0.000%) |
| MET | Bybit → KuCoin | 156, Binance → KuCoin | связка прошла фильтры (+0.000%) |
| PONS | Gate → MEXC | 38, Bybit → Gate | связка прошла фильтры (-0.267%) |
| SOPH | Binance → OKX | 234, Binance → Bybit | связка прошла фильтры (-0.079%) |
| 哈基米 | Bybit → Binance | Отсутствует | Каталог Binance содержит `哈基米USDT`. Проверка базовой валюты допускает ASCII `[A-Z0-9]` |
| DELL | WhiteBIT → Bitget | Отсутствует | Bitget `isRwa=YES`. WhiteBIT `type=tradfiFutures` проходит локальный допуск |
| DASH | Gate → Bybit | 85, WhiteBIT → OKX | связка прошла фильтры (+0.000%) |
| BILL | KuCoin → Binance | 139, Bybit → Binance | расхождение меток 1.596 с |
| SOXX | Bitget → Bybit | Отсутствует | Bitget `isRwa=YES`. Bybit `symbolType=ETF` проходит локальный допуск |
| NVDA | Gate → Bybit | Отсутствует | Gate `contract_type=stocks` и Bybit `symbolType=stock` |
| VVV | KuCoin → OKX | 99, Binance → Bybit | связка прошла фильтры (+0.016%) |
| WDC | Gate → Bitget | Отсутствует | Gate `contract_type=stocks` и Bitget `isRwa=YES` |
| LYN | Binance → Bybit | 136, Binance → Bybit | связка прошла фильтры (+0.050%) |
| GOOGL | Bitget → EdgeX | Отсутствует | Bitget `isRwa=YES`; EdgeX в локальном адаптере использует USDC |
| PEPE | Gate → HTX | 51, WhiteBIT → Gate | связка прошла фильтры (-0.060%) |
| PRL | KuCoin → Binance | Отсутствует | расхождение меток 2.630 с |
| HANA | Gate → Bybit | 229, Gate → Binance | связка прошла фильтры (-0.064%) |
| RAM | Binance → Bitget | Отсутствует | Binance `contractType=TRADIFI_PERPETUAL` и Bitget `isRwa=YES` |
| NIGHT | Bybit → KuCoin | 16, WhiteBIT → OKX | связка прошла фильтры (-0.057%) |
| PURR | Bybit → KuCoin | Отсутствует | Bybit `symbolType=stock` и KuCoin `assetClass=STOCK` |
| O | KuCoin → OKX | 199, Binance → OKX | связка прошла фильтры (-0.107%) |
| BARD | Binance → OKX | 34, WhiteBIT → Bybit | связка прошла фильтры (-0.117%) |

## Капитализация от 1 млн USD

В 11:21:01 по Киеву отдельный прогон добавил `MCap min = 1` через меню UAInvest и `capMin=1` локально. Браузер UAInvest передал `market_cap_min=1`. Метки снимков различались на 3,875 с. Все 48 строк UAInvest имели оборот от 1 млн на обеих ногах и капитализацию от 1 млн. Локально прошло 29 групп, совпали SUI, DOT, BTW, а первые связки у всех трёх различались. До группировки две публичные направленные связки прошли локальные фильтры. С дополнительной галочкой «Сверка с UAInvest» локально осталось две группы, обе есть в UAInvest, направление совпало в одной. Публичный ответ указывает источник оценки и статус сопоставления. У INIT это CoinMarketCap и `probable`. Локальный фильтр использует CoinGecko и требует подтверждения обеих ног. [Снимок с порогом](evidence/mcap-2026-11-11.json)

| Монета | Связка UAInvest | Локальная группа | Проверка точной связки |
| --- | --- | --- | --- |
| INIT | Gate → Bybit | Отсутствует | нет привязки обеих ног |
| ARIA | Gate → Bitget | Отсутствует | нет привязки обеих ног |
| AGT | KuCoin → MEXC | Отсутствует | расхождение меток 4.152 с, нет привязки обеих ног |
| BEAM | WhiteBIT → Bybit | Отсутствует | нет привязки обеих ног |
| UP | OKX → Aster | Отсутствует | нет привязки обеих ног |
| BULLA | Binance → Gate | Отсутствует | нет привязки обеих ног |
| CYBER | WhiteBIT → Bybit | Отсутствует | нет привязки обеих ног |
| RSR | WhiteBIT → Bybit | Отсутствует | нет привязки обеих ног |
| BLUR | WhiteBIT → OKX | Отсутствует | нет привязки обеих ног |
| JCT | Gate → Bitget | Отсутствует | нет привязки обеих ног |
| CHIP | WhiteBIT → Bybit | Отсутствует | нет привязки обеих ног |
| STX | WhiteBIT → OKX | Отсутствует | нет привязки обеих ног |
| GRAM | OKX → Aster | Отсутствует | нет привязки Aster |
| Q | Gate → Binance | Отсутствует | нет привязки обеих ног |
| ONE | Gate → OKX | Отсутствует | нет привязки Gate |
| IRYS | KuCoin → Bybit | Отсутствует | нет привязки обеих ног |
| HUMA | OKX → Bybit | Отсутствует | нет привязки обеих ног |
| VVV | Gate → OKX | Отсутствует | нет привязки Gate |
| AERO | Bybit → Bitget | Отсутствует | нет привязки обеих ног |
| SUI | KuCoin → Aster | 14, Bybit → MEXC | нет привязки обеих ног |
| HANA | Gate → Bybit | Отсутствует | нет привязки обеих ног |
| LYN | Binance → Bybit | Отсутствует | нет привязки обеих ног |
| UAI | Gate → Binance | Отсутствует | нет привязки обеих ног |
| RENDER | Bybit → Binance | Отсутствует | нет привязки обеих ног |
| DASH | Gate → Bybit | Отсутствует | нет привязки обеих ног |
| DOT | Bybit → Binance | 9, Bybit → MEXC | связка прошла фильтры (+0.024%) |
| BTW | OKX → Gate | 1, Bitget → Bybit | связка прошла фильтры (+0.065%) |
| MERL | Bybit → MEXC | Отсутствует | нет привязки обеих ног |
| MET | Bybit → KuCoin | Отсутствует | нет привязки обеих ног |
| NIGHT | Bybit → KuCoin | Отсутствует | нет привязки обеих ног |
| BILL | OKX → Binance | Отсутствует | расхождение меток 1.520 с, нет привязки обеих ног |
| PEPE | Gate → HTX | Отсутствует | нет привязки обеих ног |
| COMP | Bybit → Binance | Отсутствует | нет привязки обеих ног |
| PRL | KuCoin → Binance | Отсутствует | расхождение меток 2.417 с, нет привязки обеих ног |
| SOPH | Bybit → OKX | Отсутствует | нет привязки обеих ног |
| CORE | Bybit → OKX | Отсутствует | нет привязки обеих ног |
| O | KuCoin → OKX | Отсутствует | нет привязки обеих ног |
| RIVER | OKX → Binance | Отсутствует | расхождение меток 23.160 с, нет привязки обеих ног |
| EVAA | Bitget → KuCoin | Отсутствует | нет привязки обеих ног |
| LSK | Bybit → Binance | Отсутствует | нет привязки обеих ног |
| PONS | Gate → MEXC | Отсутствует | нет привязки обеих ног |
| ERA | Binance → Bitget | Отсутствует | расхождение меток 2.247 с, нет привязки обеих ног |
| DGAI | Bybit → OKX | Отсутствует | нет привязки обеих ног |
| PROM | Bitget → Gate | Отсутствует | нет привязки обеих ног |
| ESPORTS | Bybit → Binance | Отсутствует | нет привязки обеих ног |
| BARD | Binance → OKX | Отсутствует | нет привязки обеих ног |
| 哈基米 | Bybit → Binance | Отсутствует | Каталог Binance содержит `哈基米USDT`. Проверка базовой валюты допускает ASCII `[A-Z0-9]` |
| PARTI | Gate → Bitget | Отсутствует | нет привязки обеих ног |

## Спот и отдельная сверка стаканов

Локальный режим «Спот + шорт · фандинг» с USDT, оборотом от 1 млн на каждой ноге, капитализацией и FDV от 1 млн показал 54 группы. В выдаче подтверждены, например, HYPE Bybit Spot → OKX Swap, ZEC OKX Spot → OKX Swap и VVV OKX Spot → OKX Swap. В HYPE обе ноги получили `hyperliquid`, капитализация составила 17 160,09 млн USD, FDV — 68 203,93 млн USD. [Снимок спотовой выдачи](evidence/spot-2026-11-11.json)

В 11:22:22–11:22:30 по Киеву для AGT Gate → MEXC сравнили Ask лонга и Bid шорта с публичной карточкой. Пять запросов дали три разных локальных снимка, два ответа пришли из трёхсекундного кэша. В трёх свежих наблюдениях биржевые метки локальных ног различались на 265–515 мс. Карточка прочитана через 101–135 мс после получения первой локальной ноги, её биржевые метки в DOM недоступны.

| Ask / Bid локально | Ask / Bid в карточке | Open Spread локально | Open Spread в карточке |
| --- | --- | ---: | ---: |
| 0,055917 / 0,05628 | 0,055917 / 0,05628 | 0,64708% | 0,647% |
| 0,055864 / 0,05619 | 0,055864 / 0,05619 | 0,58186% | 0,582% |
| 0,055792 / 0,05610 | 0,055770 / 0,05614 | 0,55053% | 0,661% |

На одинаковых ценах формула воспроизвела значение карточки до её точности отображения. Разницу около 0,111 п.п. в третьем наблюдении воспроизвели разные Ask и Bid при общей формуле. Это отдельный замер на той же связке. [Карточка AGT](https://uainvest.com.ua/arbitrage/agt-mexc-swap-gate-swap) · [Снимки стаканов](evidence/agt-books-2026-11-11.json)

## Проверка строк, видимых только локально

В 11:23:02 по Киеву адресные запросы к публичной выдаче без группировки вернули HTTP 200 и пустой список для трёх тикеров. Разница меток публикации составила 4,261 с. С порогом капитализации локально были BTC Bybit → Gate (Open Spread −0,00136%, APR 1,35%) и AKE Bybit → Gate (+0,14620%, APR 0%). Обе строки отпадают при включении локальной галочки «Сверка с UAInvest». Без порога капитализации локальная ALLO WhiteBIT → Bybit (+0,53028%, APR 0%) прошла бы и дополнительный отбор. По публичному ответу причина серверного исключения этих строк неизвестна. [Запросы и локальные значения](evidence/local-only-2026-11-11.json)

Проверка охватила короткие живые снимки. Сопоставление публичной спотовой таблицы, отдельный паритет FDV и длительный прогон остаются в очереди.
