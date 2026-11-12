# Arbitrage Monitor

Локальный монитор публичных котировок спота и линейных бессрочных контрактов в USDT и USDC. Проект работает в режиме monitor-only: он не открывает позиции и не отправляет торговые ордера.

## Запуск

Нужен Python 3.12 или новее. Из корня проекта выполните:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\Start-UI.cmd
```

Скрипт откроет локальный скринер на `http://127.0.0.1:8780/` и запустит сбор публичных данных. Остановить процесс можно через Ctrl+C. WebSocket-пилот MEXC и KuCoin включается параметром `--futures-ws`, например `Start-UI.cmd --futures-ws`. Другие параметры передаются так: `Start-UI.cmd --port 8784 --venues bybit gate okx`.

Данные истории сохраняются в `data/monitor.sqlite3`; окно хранения — 48 часов. Путь можно изменить параметром `--history`. Деморежим доступен через `frontend/index.html` или `/?demo=1`.

## Проверки

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests
node --test tests/*.test.cjs
npm run lint
npm run test:e2e
```

## Документы

- [Спецификация и поведение монитора](docs/SPEC.md)
- [Этапы и ближайшие задачи](docs/ROADMAP.md)
- [Журнал проверок и изменений](docs/CHANGELOG.md)
- [Сверки покрытия бирж](docs/MARKET_COVERAGE.md)
- [Исследовательские материалы](docs/research/)

Предыдущий проект и его архив не входят в рабочие импорты или текущие команды запуска.
