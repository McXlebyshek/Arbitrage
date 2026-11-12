# Сверка с UAInvest без порогов капитализации и оборота

## Условия

11 ноября 2026 года в 15:21:49 и 15:22:38 по Киеву Playwright снял сгруппированный топ по Open Spread по убыванию. Выбраны только Crypto и USDT. Запрос UAInvest содержал `volume=0`, параметр капитализации отсутствовал. На обеих сторонах выбраны Aster, Binance, Bitget, Bybit, EdgeX, Extended, Gate, HTX, KuCoin, MEXC, OKX и WhiteBIT. Все 21 локальный источник котировок ответил, как и все 12 каталогов бессрочных контрактов при проверке точных ног. Метки публикации списков разошлись на 6,723 и 6,920 с. Это метки снимков, время каждой котировки проверяется отдельно.

Из первого публичного топ-50 исключены 6 строк с ногой в USDC или USD1 и 12 строк с подтверждённым TradFi. Во втором исключены 5 строк по валюте и 11 по классу актива. Основание для исключения — нативный символ или биржевой каталог. Строки за пределами публичного топ-50 для замены исключённых не запрашивались. [Первый снимок](evidence/no-cap-first-2026-11-11.json) · [Повторный снимок](evidence/no-cap-repeat-2026-11-11.json) · [Каталоги](evidence/no-cap-audit-2026-11-11.json)

## Результаты

| Показатель | Первый снимок | Повтор |
| --- | ---: | ---: |
| Локальные группы | 596 | 590 |
| Локальные группы с неизвестной капитализацией | 559 | 556 |
| Сопоставимые строки из публичного топ-50 | 32 | 34 |
| Общие токены в локальном топ-50 | 26 | 26 |
| Те же лонг и шорт в локальном топ-50 | 19 | 20 |
| Публичные токены во всей локальной выдаче | 30 из 32 | 32 из 34 |
| Точные публичные направления прошли локальные фильтры до группировки | 25 из 32 | 24 из 34 |

В локальном топ-50 неизвестная капитализация у 30 и 31 группы соответственно. Например, PTB и CLAUDIA вошли в оба локальных топа. При подсчёте прямо по сырым спискам было 27 и 26 общих токенов, направления совпали 19 и 20 раз. Дополнительный пересчёт с локальным режимом «Сверка с UAInvest» дал 26 и 28 общих токенов и те же 19 и 20 направлений. Этот режим в основном сравнении выключен.

В обоих прогонах из всей локальной выдачи отсутствовали только 10000NEX и PYBOBO среди строк, прошедших проверку Crypto и USDT. У 10000NEX нет активного контракта Bitget. Для PYBOBO KuCoin → MEXC разница меток превысила 1,5 с в обоих снимках. MYX и RAVE найдены локально на других площадках. HTX не публикует эти рынки в проверенном каталоге.

### Строки первого снимка с расхождениями

| Публичный ранг и монета | Публичные лонг → шорт | Локальный ранг и направление | Причина |
| --- | --- | --- | --- |
| 1. CASHCAT | EdgeX → HTX | 245, Gate → Aster | EdgeX USDC |
| 2. MYX | Bybit → HTX | 57, Bybit → Binance | HTX без этого рынка в USDT в каталоге, локальный ранг ниже 50 |
| 3. ASX | HTX → Gate | — | Gate contract_type=stocks |
| 4. 10000NEX | Bybit → Bitget | — | Bitget ранее вернул `40309`, контракт по-прежнему отсутствует в каталоге |
| 5. H100 | Bitget → OKX | — | Bitget isRwa=YES, OKX instCategory=4 |
| 6. RAVE | KuCoin → HTX | 184, Bybit → Binance | HTX без этого рынка в USDT в каталоге, локальный ранг ниже 50 |
| 7. BP | EdgeX → Gate | — | EdgeX USDC |
| 9. BOME | Binance → HTX | 436, Binance → OKX | Binance USDC |
| 13. UP | MEXC → Aster | 6, OKX → Aster | точная пара прошла фильтры (1,421%), спред выбранной пары равен |
| 14. PYBOBO | KuCoin → MEXC | — | разница биржевых меток 1,707 с |
| 15. DANGOTE | Gate → Bybit | — | Gate contract_type=stocks, Bybit symbolType=stock |
| 17. YPF | Bybit → Gate | — | Bybit symbolType=stock, Gate contract_type=stocks |
| 19. B200 | Gate → Bitget | — | Gate contract_type=indices, Bitget isRwa=YES |
| 21. BULLA | Aster → Gate | 17, Binance → Gate | точная пара прошла фильтры (0,678%), у выбранной пары спред выше |
| 23. MOONSHOT | EdgeX → KuCoin | — | KuCoin assetClass=STOCK |
| 25. PEARL | Aster → EdgeX | — | EdgeX USDC |
| 29. PHAROS | MEXC → WhiteBIT | 20, Binance → WhiteBIT | точная пара прошла фильтры (0,656%), спред выбранной пары равен |
| 30. GRAM | EdgeX → Aster | 40, OKX → Aster | EdgeX USDC |
| 31. ERA | Extended → Bybit | 25, Gate → Bybit | Extended без этого рынка в USDT в каталоге |
| 32. ABNB | Bitget → Gate | — | Bitget isRwa=YES, Gate contract_type=stocks |
| 37. POET | Bitget → KuCoin | — | Bitget isRwa=YES, KuCoin assetClass=STOCK |
| 38. ARIA | Binance → Bitget | 12, Aster → KuCoin | точная пара прошла фильтры (0,623%), у выбранной пары спред выше |
| 39. ESPORTS | MEXC → KuCoin | 24, Binance → KuCoin | разница биржевых меток 1,518 с |
| 40. PREDICT | Bybit → Aster | 80, MEXC → Aster | Bybit помечает рынок как премаркет, локальный ранг ниже 50 |
| 41. FTNT | Gate → Bitget | — | Gate contract_type=stocks, Bitget isRwa=YES |
| 44. SHEINHKD | KuCoin → Bitget | — | KuCoin assetClass=STOCK, Bitget isRwa=YES |
| 46. WLFI | WhiteBIT → MEXC | 53, WhiteBIT → Gate | MEXC USD1 |
| 47. ARKK | MEXC → Bybit | — | MEXC conceptPlate=TradFi/ETF, Bybit symbolType=ETF |
| 48. RGTI | Bitget → KuCoin | — | Bitget isRwa=YES, KuCoin assetClass=STOCK |
| 49. BMT | Gate → Bybit | 97, Gate → Bybit | точная пара прошла фильтры (0,236%), локальный ранг ниже 50 |
| 50. AGT | Gate → MEXC | 18, Gate → Aster | точная пара прошла фильтры (0,613%), у выбранной пары спред выше |

## Сверка Bid и Ask

В отдельном Playwright для CLAUDIA Gate → MEXC локальный Gate Ask 0,005256 и MEXC Bid 0,005400 совпали с карточкой до и после запросов. Они дают 2,7027%, в карточке показано 2,703%. Метки двух локальных стаканов различались на 688 мс. Публичный список также содержит поле `price`, но по этим двум полям формула не воспроизводит опубликованный спред, поэтому в этом прогоне использованы явные Bid и Ask карточки. Публичных биржевых меток в карточке нет. [Стаканы CLAUDIA](evidence/no-cap-books-2026-11-11.json) · [Карточка UAInvest](https://uainvest.com.ua/arbitrage/claudia-mexc-swap-gate-swap)

Локальный лидер AI OKX → Aster дал 60,652% и 60,407% в двух снимках. В дополнительной проверке стаканы подтвердили уровни OKX Ask 0,01392 и Aster Bid 0,02598, но их метки разошлись на 1,658 с. OKX прямо называет базовый актив Gensyn и использует AIGENSYN/USDT в индексе. Метаданные Aster указывают тикер AI и индекс около 0,0261, идентичность базового актива Aster осталась неподтверждённой. Локальная выдача помечает такую пару как предварительную. Причина отсутствия AI в публичном топе не установлена. [Каталоги, индексы и стаканы AI](evidence/no-cap-ai-2026-11-11.json) · [Анонс OKX](https://www.okx.com/id/help/okx-to-list-perpetual-futures-for-ai-crypto)

Рыночный код в этой порции не менялся. Выполнены два прогона Playwright, проверка каталогов и адресные запросы стаканов. Длительная стабильность и идентичность AI на Aster остаются непроверенными.
