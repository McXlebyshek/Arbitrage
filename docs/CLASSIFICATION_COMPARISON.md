# TradFi и Unicode-тикеры

## Проверка каталогов 11 ноября 2026 года

Криптовалютный скринер исключает линейные USDT-контракты с подтверждёнными признаками TradFi. Каталоги сравнили старым и новым правилами на одних данных.

| Биржа | Добавленные признаки | Исключено дополнительно |
| --- | --- | ---: |
| Bybit | `symbolType=ETF` или `forex`, включая SOXX и SOXL | 63 |
| WhiteBIT | `type=tradfiFutures`, включая DELL, NATGAS и XAU | 93 |
| Gate | `contract_type=metals`, включая XAU и XAG | 12 |
| KuCoin | `assetClass=COMMODITY` или `METAL`, включая NATGAS и XAG | 9 |

Для SOXX Bybit называет базовым инструментом iShares Semiconductor ETF. DELL WhiteBIT и DELL Bitget исключены соответственно по `tradfiFutures` и `isRwa=YES`. У NATGAS обе наблюдавшиеся ноги WhiteBIT и KuCoin получили подтверждённые признаки TradFi. [Каталоги](evidence/classification-catalog-2026-11-11.json) · [Сравнение допуска](evidence/classification-counts-2026-11-11.json) · [Анонс Bybit SOXX](https://announcements.bybit.com/en/article/new-listing-soxxusdt-perpetual-contract-with-up-to-20x-leverage--artffef63767592/) · [Классификация Gate](https://www.gate.com/docs/developers/apiv4/en/futures/)

## Подтверждение HAJIMI

Binance и Bybit называют общий базовый актив в анонсах конкретных контрактов. CoinGecko указывает API ID `hakimi` и ссылки на эти два бессрочных контракта, а также на точные рынки Bitget, Gate и KuCoin. Все пять нативных идентификаторов и размеры контрактов сверены с текущими каталогами. [Binance](https://www.binance.com/en-NZ/support/announcement/detail/ca3196e008da448da107d397bae0b4b0) · [Bybit](https://announcements.bybit.com/en/article/new-listing-hajimiusdt-perpetual-contract-in-innovation-zone-with-up-to-10x-leverage--arte7ee8f9bc7dc/) · [CoinGecko](https://www.coingecko.com/en/coins/hajimi)

| Биржа | Нативный ID | Размер контракта в HAJIMI |
| --- | --- | ---: |
| Binance | `哈基米USDT` | 1 |
| Bybit | `HAJIMIUSDT` | 1 |
| Bitget | `哈基米USDT` | 1 |
| Gate | `哈基米_USDT` | 100 |
| KuCoin | `HAJIMIUSDTM` | 100 |

Группа и маршруты используют `HAJIMI`. Для запросов к бирже сохраняется её исходный символ. При участии ноги с изменённым тикером требуются две подтверждённые привязки к одному активу. Запросы поиска по обоим написаниям выбирают одну группу. В живом снимке MEXC использовал написание HAJIMI и оставался без привязки.

## Целевой прогон Playwright

11 ноября в 11:48:20 по Киеву при USDT, Crypto, группировке и обороте от 1 млн на обеих ногах UAInvest показал `哈基米` Bybit → Binance со спредом 1,07425%. Разница меток публикации с локальным снимком составила 150 мс. В том же направлении локальные Ask 0,01577 и Bid 0,01591 дали 0,88384%. Для Bybit в пакетном снимке доступно время получения. Биржевые метки Ask/Bid публичного списка недоступны, поэтому причина расхождения цен не подтверждена.

Два прямых стакана ответили HTTP 200. Биржевые метки локальных стаканов различались на 751 мс. Ask Bybit 0,01577 и Bid Binance 0,01592 дали 0,94667%. Локальные капитализация и FDV составили 15,731454 млн USD. SOXX, DELL и NATGAS дали пустую локальную выдачу. Все 21 источник были в состоянии `ok`. [Целевой снимок и стаканы](evidence/classification-first-2026-11-11.json)

При одновременных порогах капитализации и FDV от 1 млн переключили поиск с `HAJIMI` на `哈基米` на одном локальном снимке. Оба запроса показали одну группу с Bybit → KuCoin. Карточка открылась с подтверждённой парой, у которой капитализация и FDV составили 15,731454 млн USD. [Проверка поиска и карточки](evidence/classification-search-2026-11-11.json)

## Базовый прогон на 12 общих площадках

В 11:52:51 по Киеву на тех же базовых фильтрах получили 50 строк UAInvest и 242 локальные группы. Разница меток публикации составила 1,077 с. Все 50 публичных строк имели оборот от 1 млн на каждой ноге. В первых 50 совпали 19 монет и 10 направленных связок. Во всей локальной выдаче присутствовали 35 публичных монет, а 30 точных направленных связок прошли фильтры до группировки.

HAJIMI заняла второе место в обоих списках. Публичная Bybit → Binance прошла локальные фильтры. Локальный расчёт на Ask 0,01588 и Bid 0,01606 дал 1,12711334%, что совпало с опубликованным спредом до точности ответа. При группировке локально выбрана Bybit → KuCoin с 1,25156%. [Базовый снимок и диагностика](evidence/classification-baseline-2026-11-11.json)

### Причины отсутствия точной связки

| Монета | Связка UAInvest | Подтверждённое условие |
| --- | --- | --- |
| AERO | Extended → Bitget | В каталоге локального Extended отсутствовал AERO в USDT и USDC |
| XOM | Bybit → Bitget | Bybit `stock`, Bitget `isRwa=YES` |
| WDC | Gate → Bitget | Gate `stocks`, Bitget `isRwa=YES` |
| BMNR | Gate → Bybit | Gate `stocks`, Bybit `stock` |
| SKDD | Bitget → KuCoin | Bitget `isRwa=YES`, KuCoin `STOCK` |
| SOXX | Bitget → Bybit | Bitget `isRwa=YES`, Bybit `ETF` |
| DELL | WhiteBIT → Bitget | WhiteBIT `tradfiFutures`, Bitget `isRwa=YES` |
| RAM | Binance → Bitget | Binance `TRADIFI_PERPETUAL`, Bitget `isRwa=YES` |
| MU | HTX → EdgeX | HTX `tradfi_labels=Stocks`, в локальном каталоге EdgeX отсутствовал MU |
| SECZ | Bitget → Bybit | Bitget `isRwa=YES`, Bybit `stock` |
| MRNA | Bitget → Bybit | Bitget `isRwa=YES`, Bybit `stock` |
| GOOGL | Bitget → EdgeX | Bitget `isRwa=YES`, в локальном каталоге EdgeX отсутствовал GOOGL |
| INTW | Bybit → Bitget | Bybit `ETF`, Bitget `isRwa=YES` |
| CSOPSKHYNIX2L | KuCoin → Binance | KuCoin `STOCK`, Binance `TRADIFI_PERPETUAL` |
| AGT | KuCoin → MEXC | Биржевые метки различались на 3,447 с |
| SOPH | Binance → OKX | Биржевые метки различались на 3,064 с |
| RIVER | OKX → Binance | Биржевые метки различались на 3,178 с |
| PRL | KuCoin → Binance | Биржевые метки различались на 5,248 с |
| EVAA | Binance → KuCoin | Биржевые метки различались на 3,256 с |
| EDEN | KuCoin → Binance | Биржевые метки различались на 20,745 с |

[Ответы каталогов по пропущенным ногам](evidence/classification-missing-2026-11-11.json)

В публичном ответе с фильтром Crypto присутствовали подтверждённые рынки TradFi. Охват проекта ограничен крипторынком. В следующих сверках подтверждённые TradFi исключаются из публичной выборки перед подсчётом совпадений. Длительная стабильность и причина расхождения цен в первом целевом снимке остаются непроверенными.

При повторной адресной проверке публичный запрос с `category=crypto` вернул SOXX Bitget → Bybit и DELL WhiteBIT → Bitget вместе с другими направлениями DELL. По каталогам SOXX отслеживает фонд акций, а DELL — акции Dell Technologies. [Ответы UAInvest](evidence/crypto-scope-uainvest-2026-11-11.json)
