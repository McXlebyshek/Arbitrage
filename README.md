# Арбитраж Монитор

Локальный скринер по мотивам UAInvest. Он сравнивает публичные котировки спота и линейных бессрочных контрактов в USDT и USDC.

## Структура

- `monitor.py` — запуск и координация цикла сбора.
- `market_monitor/http_api.py`, `books.py`, `snapshot.py` — HTTP и SSE, кэш стаканов и сборка снимка.
- `market_monitor/` — биржевые адаптеры, реестр инструментов, фандинг и расчёт издержек.
- `frontend/` — скринер и демонабор.
- `tests/` — проверки текущего проекта.
- `OLD/` — локальный архив предыдущего проекта. Для запуска достаточно текущих исходников.

## Запуск

Запустите `Start-UI.cmd` из корня проекта и дождитесь загрузки каталогов. Скрипт открывает [локальный скринер](http://127.0.0.1:8780/) и использует окружение `G:\Arbitrage\.venv`. Для первой установки нужен Python 3.12 или новее.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\Start-UI.cmd
```

Публичные WebSocket-котировки фьючерсов запускаются через `Start-UI.cmd --futures-ws`.
Поддерживаются Binance, Bybit, OKX, Gate, Bitget и пилот MEXC/KuCoin.
[Подписки и результаты живого прогона](docs/reports/UNIFIED_WS_LIVE.md)

Параметры сервера можно передавать через скрипт. Например, `Start-UI.cmd --port 8784 --venues bybit gate okx`. Остановить сбор можно через Ctrl+C. Наблюдения хранятся в `data/monitor.sqlite3` с окном хранения 48 часов. Параметр `--history` задаёт другой путь.

Деморежим открывается через `frontend/index.html` или `/?demo=1`. Он помечен в интерфейсе и использует отдельный демонстрационный набор.

Проверки текущего проекта из корня:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests
node --test tests/*.test.cjs
```

## Проверка интерфейса

После установки Playwright (`npm install --no-save playwright` и
`npx playwright install chromium`) запустите `node tests/e2e/verify.cjs`.
Сценарий использует синтетические данные и локальный сервер. Переменная
`ARBITRAGE_PYTHON` задаёт путь к Python с зависимостями проекта.

[Спецификация скринера](docs/SPEC.md) · [План работ](docs/ROADMAP.md) · [Журнал проверок](docs/CHANGELOG.md)

Архив текущих исходников создаётся из проверенного коммита командой
`git archive -o Arbitrage-source.zip HEAD`. В архив входят только файлы коммита,
за вычетом путей с атрибутом `export-ignore`.
