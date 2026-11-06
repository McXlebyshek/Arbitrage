# Явный реестр 0.33

20 активов, 120 контрактов на Binance USD-M, Bybit, OKX, Bitget, Gate и MEXC.
Новые активы: BNB, BCH, TRX, ATOM, NEAR, UNI, AAVE, SUI, ARB, OP. Первоначальные
60 записей сохранены без изменений. Версия: `2026-11-05-twenty-assets-v3`.

Соответствия задаются вручную в `arbitrage/instruments.json`, не создаются
автоматически по совпадению тикера. Публичные каталоги на дату проверки подтверждают
ожидаемые native id, base, active linear swap, quote/settle USDT и contractSize.
Это не проверка депозитных сетей, доступности торговли или полного состава
ценовых индексов. Различия индексов и будущие изменения спецификаций остаются риском модели.

## Единицы новых контрактов

Во всех новых записях `price_units=1`: цена выражена за одну каноническую монету.
Размер контракта задаёт количество монет, а не делитель цены.

| Base → канонический актив | Binance / Bybit / Bitget | OKX | Gate | MEXC |
|---|---|---|---|---|
| BNB → bnb | 1 | 0.01 | 0.001 | 0.01 |
| BCH → bitcoin-cash | 1 | 0.1 | 0.01 | 0.01 |
| TRX → tron | 1 | 1000 | 100 | 10 |
| ATOM → cosmos | 1 | 1 | 1 | 0.1 |
| NEAR → near-protocol | 1 | 10 | 1 | 1 |
| UNI → uniswap | 1 | 1 | 1 | 0.1 |
| AAVE → aave | 1 | 0.1 | 0.01 | 0.01 |
| SUI → sui | 1 | 1 | 1 | 1 |
| ARB → arbitrum | 1 | 10 | 1 | 1 |
| OP → optimism | 1 | 1 | 1 | 1 |

Native id: `BASEUSDT` у Binance/Bybit/Bitget, `BASE-USDT-SWAP` у OKX,
`BASE_USDT` у Gate/MEXC. В файле сохранены конкретные ID, а не правило генерации.
Смена base/размера/линейности/валюты при дальнейшем сборе блокирует инструмент.

Источники именования активов — официальные страницы площадки:
[BNB](https://www.okx.com/price/bnb-bnb), [Bitcoin Cash](https://www.okx.com/price/bitcoin-cash-bch),
[TRON](https://www.okx.com/price/tron-trx), [Cosmos](https://www.okx.com/price/cosmos-atom),
[NEAR](https://www.okx.com/price/near-protocol-near), [Uniswap](https://www.okx.com/price/uniswap-uni),
[Aave](https://www.okx.com/price/aave-aave), [Sui](https://www.okx.com/price/sui-sui),
[Arbitrum](https://www.okx.com/price/arbitrum-arb), [Optimism](https://www.okx.com/price/optimism-op).
Они дополняют проверку точных спецификаций, а не заменяют её.
Снимок нормализованных спецификаций и исходных info всех 120 контрактов:
[REGISTRY_V033_CATALOG.json](reports/REGISTRY_V033_CATALOG.json).

## Повторная проверка

```powershell
Set-Location G:\Arbitrage
# Новая папка; публичные каталоги без ключей, тикеров и ордеров
.\.venv\Scripts\python.exe validate_registry.py --output data/registry-audit-001
# Воспроизведение проверки сохранённых метаданных без сети
.\.venv\Scripts\python.exe validate_registry.py --output data/registry-offline-001 --catalog docs/reports/REGISTRY_V033_CATALOG.json
```

Стенд не изменяет реестр. Он сохраняет каталог и отчёт, отмечает отсутствующие/
неоднозначные ID, изменения base, активности/типа/валюты и размера контракта.
Две попытки загрузки и общий deadline 90с на площадку. Отсутствие данных даёт
неуспех проверки, а не удаление записей. Проверка сохранённого каталога не утверждает
текущую доступность API или актуальность метаданных. Канонический asset и price_units
не выводятся из API автоматически и требуют отдельного ручного рассмотрения.

## Вместимость

При шести пригодных площадках на каждый актив возможно 30 направлений,
теоретически до 600 в сумме. Фактическое число ниже при старых/недоступных котировках
или расхождении времени. 20 стаканов MEXC помещаются в текущий лимит 20;
увеличение реестра дальше потребует пересмотра отбора и нагрузки. Широкий сбор BBO
не означает расчёт глубины для всех направлений: экономика по-прежнему выбирает
ограниченное число кандидатов (по умолчанию 3).

Чтобы применить новый набор, перезапустите монитор, UI и используемые paper-процессы.
Их уже запущенные экземпляры сохраняют прежний реестр в памяти. Расширение не
перезапускает процессы автоматически. Старые paper-позиции совместимы, поскольку
их исходные bindings сохранены.

Измерения: [REGISTRY_EXPANSION_CHECK.md](reports/REGISTRY_EXPANSION_CHECK.md).
