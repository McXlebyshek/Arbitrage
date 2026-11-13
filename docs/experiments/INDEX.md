# Индекс экспериментов и сверок

Краткие выводы, методика и ограничения хранятся в отчётах ниже. Исходные JSON/JSONL записи находятся в [`docs/evidence`](../evidence/) и передаются через Git LFS. Их контрольные SHA-256 и размеры перечислены в [`EVIDENCE_SHA256SUMS.txt`](EVIDENCE_SHA256SUMS.txt).

## Охват рынков, активы и оценки

- [Охват активов](ASSET_COVERAGE.md)
- [Дополнительная проверка охвата](ASSET_COVERAGE_FOLLOWUP.md)
- [Классификация рынков](CLASSIFICATION_COMPARISON.md)
- [Фандинг и сравнение выборок](FUNDING_COMPARISON.md)
- [Матрица покрытия рынков](MARKET_COVERAGE.md)
- [Фильтры и идентичность рынков](THREE_FILTER_COMPARISON.md)
- [Дополнительные проверки фильтров](THREE_FILTER_FOLLOWUP.md)
- [Идентичность контрактов](THREE_FILTER_IDENTITIES.md)
- [Масштабированные символы](THREE_FILTER_SCALED.md)

## Спреды, ликвидность и сопоставление выдачи

- [Проверка AGT и JELLYJELLY](AGT_JELLYJELLY_COMPARISON.md)
- [AI и PYBOBO](AI_PYBOBO_COMPARISON.md)
- [Сравнение спреда и стаканов](SPREAD_COMPARISON.md)
- [Выдача без порога капитализации](NO_CAP_COMPARISON.md)
- [Повторная сверка оборота](VOLUME_REPEAT_COMPARISON.md)
- [Стаканы и глубина в сценариях фильтрации](THREE_FILTER_DEPTH.md)
- [Идентичность ZEST и оценка глубины](ZEST_DEPTH_COMPARISON.md)

## WebSocket, задержки и переподключения

- [Пяти минутный shadow-прогон REST и WebSocket](REST_WS_SHADOW_2026-11-13.md)
- [Переход Binance между источниками котировок](BINANCE_DEPTH_CROSSOVER.md)
- [Ранние стаканы Binance](BINANCE_DEPTH_EARLY.md)
- [Циклы приоритетных запросов Binance](BINANCE_DEPTH_PRIORITY_CYCLE.md)
- [Подключение ранней публикации Binance](BINANCE_EARLY_INTEGRATION.md)
- [Публикация ранних котировок](BINANCE_EARLY_PUBLICATION.md)
- [Короткий прогон ранних котировок](BINANCE_EARLY_SOAK.md)
- [Ранние котировки MEXC и KuCoin](MEXC_KUCOIN_EARLY_COMPARISON.md)
- [WebSocket-сверка MEXC и KuCoin](MEXC_KUCOIN_WS_COMPARISON.md)
- [Пилот переподключения MEXC и KuCoin](MEXC_KUCOIN_WS_PILOT.md)
- [Широкая проверка MEXC и KuCoin](MEXC_KUCOIN_WS_WIDE.md)
- [Влияние временных меток](QUOTE_TIMING_COMPARISON.md)
