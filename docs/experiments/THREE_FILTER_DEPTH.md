# Сверка реестра и прямых стаканов

## Реестр

Подтверждены 45 USDT-рынков DOS (DAPPOS), ZEREBRO (Zerebro), IDOL (MEET48), COAI (ChainOpera AI) и ARIA (Aria.AI). Добавлены 28 бессрочных контрактов и 17 спотовых рынков. Реестр вырос с 89 до 93 активов и со 184 до 201 спотовой привязки. Для ARIA использован существующий идентификатор CoinGecko. Среди новых привязок есть обе ноги DOS Gate → Aster, ZEREBRO Gate → KuCoin, IDOL Bitget → Aster и COAI Gate → KuCoin, а также ARIA на Aster.

Пять активных рынков остались без привязки по итогам этой порции. Это DOS и ARIA на MEXC Futures, DOS и ZEREBRO на HTX Spot и COAI на Aster. В доказательствах указаны конкретные нативные идентификаторы и ссылки на источники идентификации подтверждённых рынков. [Публичные каталоги](../evidence/three-filter-depth-catalog-2026-11-11.json) · [Подтверждённые привязки](../evidence/three-filter-depth-registry-2026-11-11.json)

## Playwright на 12 общих площадках

11 ноября в 13:59:41 по Киеву сравнили выдачу Crypto, USDT и капитализацию от 1 млн USD с группировкой по монете и Open Spread по убыванию. Порог оборота отключён, в запросе UAInvest передано `volume=0`. У 40 строк из 50 оборот хотя бы одной ноги был ниже 1 млн. Метки публикации снимков разошлись на 281 мс. Ответил каждый из 21 локального источника, все 45 новых привязок появились в живом API, CoinGecko вернул 93 записи.

Из публичного топ-50 исключены BP и BTW с ногой EdgeX в USDC, ещё шесть строк с публичным символом, заканчивающимся на USDC, и GOOGL с подтверждённой классификацией акции. В сравнении осталось 41 из 50 строк. Для измерения вклада реестра обе локальные таблицы пересчитаны на тех же котировках, до и после активации 45 новых привязок.

| Локальный пересчёт | Группы локально | Общие токены в топ-50 | Совпавшие направления в топ-50 |
| --- | ---: | ---: | ---: |
| До расширения | 64 | 17 | 14 |
| После расширения | 68 | 20 | 14 |

Общими стали IDOL, ZEREBRO и COAI. Их точные публичные направления прошли фильтры. В локальной группировке у каждого выше стояло другое направление. DOS появился в локальном списке и отсутствовал в публичном топ-50 этого снимка. В отдельной проверке режима «Спот + шорт · фандинг» все пять активов появились с капитализацией и FDV. [Снимок](../evidence/three-filter-depth-snapshot-2026-11-11.json) · [Ответы каталогов](../evidence/three-filter-depth-audit-2026-11-11.json)

### Строки с расхождениями

| Публичный ранг и токен | Публичные лонг → шорт | Локальный ранг и направление | Подтверждённая причина |
| --- | --- | --- | --- |
| 1. CASHCAT | Gate → HTX | — | HTX помечает контракт неактивным |
| 2. MYX | KuCoin → HTX | — | HTX не публикует MYX в проверенном каталоге |
| 3. 10000NEX | Bybit → Bitget | — | Bitget вернул `40309` в прошлой проверке, контракт по-прежнему отсутствует в каталоге |
| 4. RAVE | Bybit → HTX | — | HTX не публикует RAVE в проверенном каталоге |
| 5. BP | EdgeX → Gate | — | у EdgeX только USDC, у Gate премаркет |
| 7. BOME | Binance → HTX | — | публичная нога `BOMEUSDC` в USDC |
| 9. PYBOBO | KuCoin → MEXC | — | расхождение биржевых меток 4,511 с |
| 12. ERA | Extended → Bybit | — | в каталоге Extended нет ERA |
| 16. VELO | Gate → MEXC | — | нет привязки Gate и MEXC |
| 20. PREDICT | Bybit → Aster | — | Bybit `isPreListing=true` |
| 22. IDOL | Gate → Aster | 24, Bitget → Aster | точная пара прошла фильтры (0,080%), спред выбранной пары выше |
| 24. AGT | Gate → Extended | — | в каталоге Extended нет AGT, CoinGecko вернул только FDV |
| 25. HAJIMI | Bybit → EdgeX | 4, Bybit → Gate | в каталоге EdgeX нет рынка по HAJIMI и 哈基米 |
| 26. ARIA | Gate → Bitget | 20, Bybit → Bitget | точная пара прошла фильтры (0,203%), спред выбранной пары выше |
| 27. BTW | EdgeX → Gate | 21, Bitget → Bybit | у EdgeX только USDC |
| 28. GRAM | Bitget → Aster | 26, OKX → Aster | публичная нога `GRAMUSDC` в USDC |
| 30. O | Gate → MEXC | — | нет привязки Gate и MEXC |
| 31. SKYAI | HTX → KuCoin | — | нет привязки HTX и KuCoin |
| 32. GTC | KuCoin → MEXC | — | нет привязки KuCoin и MEXC, расхождение биржевых меток 4,040 с |
| 33. PENGU | Bitget → Gate | — | публичная нога `PENGUUSDC` в USDC |
| 34. OG | MEXC → Gate | — | нет привязки MEXC и Gate |
| 35. POPCAT | Bitget → Binance | — | публичная нога `POPCATUSDC` в USDC |
| 36. ZEREBRO | Gate → KuCoin | 27, Gate → Binance | точная пара прошла фильтры (0,156%), спред выбранной пары выше |
| 37. DRIFT | KuCoin → Gate | — | нет привязки KuCoin и Gate |
| 38. MAVIA | Bybit → MEXC | — | нет привязки Bybit и MEXC |
| 39. SUI | KuCoin → Bitget | 49, Bybit → MEXC | публичная нога `SUIUSDC` в USDC |
| 40. Q | Gate → MEXC | 35, Gate → Binance | точная пара прошла фильтры (0,100%), спред выбранной пары равен |
| 41. 1000RATS | KuCoin → MEXC | — | нет привязки KuCoin и MEXC, расхождение биржевых меток 4,099 с |
| 42. 1000BONK | Bybit → MEXC | — | публичная нога `1000BONKUSDC` в USDC |
| 43. IRYS | KuCoin → Bybit | — | нет привязки KuCoin и Bybit |
| 44. PROM | KuCoin → MEXC | — | нет привязки KuCoin и MEXC, расхождение биржевых меток 4,292 с |
| 45. BARD | MEXC → Bybit | — | нет привязки MEXC и Bybit |
| 46. BMT | KuCoin → Bybit | — | нет привязки KuCoin и Bybit |
| 47. GOOGL | Bitget → HTX | — | Bitget `isRwa=YES`, HTX `Stocks` |
| 49. ZIL | OKX → Gate | — | нет привязки OKX и Gate |
| 50. COAI | Gate → KuCoin | 25, Gate → Binance | точная пара прошла фильтры (0,063%), спред выбранной пары выше |

## Bid и Ask

Для ZEREBRO Gate → KuCoin первое обращение к локальному стакану KuCoin вернуло HTTP 503. В повторном замере Gate Ask 0,024317 и KuCoin Bid 0,024331 совпали с публичной карточкой до и после запросов и дали 0,058%. Биржевые метки двух локальных стаканов разошлись на 243 мс. UAInvest в карточке не показывает биржевые метки. [Первый запрос](../evidence/three-filter-depth-books-2026-11-11.json) · [Повтор и спотовая выдача](../evidence/three-filter-depth-zerebro-spot-2026-11-11.json) · [Карточка ZEREBRO](https://uainvest.com.ua/arbitrage/zerebro-kucoin-swap-gate-swap)

Для PYBOBO KuCoin → MEXC в восьми последовательных пробах массовые котировки уложились в допуск 1,5 с один раз. Прямые стаканы уложились шесть раз, с разницей меток 7–1452 мс. В одной пробе стакан KuCoin просрочен, в другой оба стакана свежие, но их метки разошлись на 2878 мс. Перепроверка была отдельной от живого сборщика. [Восемь проб](../evidence/three-filter-depth-pybobo-2026-11-11.json)

## Проверки

Прошли 33 теста Python unittest, 8 тестов Node.js, `compileall`, `pip check` и целевые проверки Playwright. Длительный прогон и проверка прямых стаканов в цикле сборщика остаются в очереди.
