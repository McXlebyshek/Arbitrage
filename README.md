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

Для WebSocket-котировок биржевая метка изменения хранится отдельно от локального времени подтверждения. Актуальность зависит от живости соединения и недавнего подтверждения конкретного символа, а расхождение биржевых меток показывается как предупреждение. Подписки фьючерсных потоков обновляются на живом соединении; отбор сохраняет запас символов возле границы лимита, чтобы не переключать их из-за небольших изменений рейтинга. Для тихих символов Binance и Gate выполняется ограниченная REST-сверка. События SSE `futures-quotes` передают только ID инструментов и динамические котировки; описания инструментов берутся из `/api/meta`. Пороги свежести котировок, допустимого рассогласования времени и соотношения цен публикуются в `config` снимка и `/api/meta`; интерфейс читает их из ответа сервера.

Данные истории сохраняются в `data/monitor.sqlite3`; окно хранения — 48 часов. Запись выполняется в фоне через очередь на одно задание; если SQLite-писатель занят, минутный снимок пропускается, а длительность последней записи доступна в `/api/health` как `historyWriteMs`. Старые точки прореживаются порциями до одного часа за обслуживание. Путь к базе можно изменить параметром `--history`. Деморежим доступен через `frontend/index.html` или `/?demo=1`.

Frontend запускается как ES-модуль `frontend/app.mjs`. Настройки, состояние фильтров, таблица, HTML-представление, диалоги и live-обновления подключаются явными импортами; `loader.js`, `bootstrap.mjs` и `globalThis.monitorModules` удалены. `app.mjs` координирует маршрут, состояние экрана и обработчики, а вспомогательные разделы находятся в `dialogs.mjs`, `presentation.mjs`, `table.mjs` и `live-ui.mjs`.

## Проверки

Установите инструменты разработки в `.venv`:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests
node --test tests/*.test.cjs
npm run lint
.venv\Scripts\ruff.exe check .
.venv\Scripts\pyright.exe
npm run test:e2e
```

Playwright-сценарий `tests/e2e/table.spec.cjs` дополнительно измеряет таблицу на 20 000 связок: long tasks при загрузке, байты compact API-ответа, число строк DOM и длительность повторного рендера. Время публикуется в выводе теста без жёсткого порога, поскольку зависит от браузера и машины.

Для строкового покрытия ключевых модулей без дополнительной зависимости используйте `trace`:

```powershell
.venv\Scripts\python.exe -m trace --module --count --summary --missing --coverdir "$env:TEMP\arbitrage-coverage" unittest tests.test_monitor.MonitorTests tests.test_market_adapters tests.test_economics tests.test_public_response_parsers tests.test_ws_adapters.PublicBookAdapterTests tests.test_source_errors
```

## Документы

- [Спецификация и поведение монитора](docs/SPEC.md)
- [Этапы и ближайшие задачи](docs/ROADMAP.md)
- [Журнал проверок и изменений](docs/CHANGELOG.md)
- [Индекс экспериментов и сверок](docs/experiments/INDEX.md)
- [Исследовательские материалы](docs/research/)

Предыдущий проект и его архив не входят в рабочие импорты или текущие команды запуска.
