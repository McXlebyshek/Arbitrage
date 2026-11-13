import {
  activePair, app, chartOffset, chartOptions, chartTab, countdown, dialog, executablePending, executableSpreads,
  interval, liquidity, nominal, renderDetail, renderRoute, renderTable, route, savePreference,
  setDetailError, setExecutablePending, showDialog, shownRows, signalRules, state, toast, visiblePairs,
} from './app.mjs';
import {button, esc, icon, money, options, pct, price, signed} from './presentation.mjs';

function updateFeedLabel() {
  const label = document.querySelector('#feed-state');
  const timer = document.querySelector('#countdown');
  if (!label || !timer) return;
  const age = live.generatedAt ? Math.max(0, Math.floor((Date.now() - live.generatedAt) / 1000)) : null;
  label.textContent = isDemo ? '● Демоданные' : live.error ? 'Сервер недоступен' :
    age === null ? 'Загрузка API' : age * 1000 > (live.config?.quoteMaxAgeMs ?? Infinity) ? 'Снимок устарел' :
      live.state === 'partial' ? 'Часть источников недоступна' : '● Публичные API';
  timer.textContent = isDemo ? countdown + ' с' : age === null ? '—' : age + ' с назад';
  if (!isDemo && live.generatedAt && snapshotInterval) {
    const remaining = Math.ceil((live.generatedAt + snapshotInterval - Date.now()) / 1000);
    timer.title = remaining > 0 ? `До ожидаемого обновления около ${remaining} с` : 'Ожидаем новый снимок';
  }
}

let refreshQueued = false;
let marketEtag = null;
let marketMetadata = null;
let lastSnapshotNotice = 0;
let snapshotInterval = 15000;
let refreshing = false;

function refreshMarketsFallback() {
  if (!live.generatedAt || live.error || Date.now() - lastSnapshotNotice >= 2 * snapshotInterval) {
    refreshMarkets();
  }
}

async function expandSnapshot(snapshot) {
  if (!snapshot.metaVersion) return snapshot;
  if (marketMetadata?.version !== snapshot.metaVersion) {
    const query = new URLSearchParams({version: snapshot.metaVersion});
    const response = await fetch('/api/meta?' + query, {signal: AbortSignal.timeout(8000)});
    if (!response.ok) throw new Error('Каталог снимка недоступен');
    const metadata = await response.json();
    if (metadata.version !== snapshot.metaVersion) throw new Error('Получена другая версия каталога');
    marketMetadata = metadata;
  }
  const markets = Object.fromEntries(Object.entries(snapshot.markets).map(([coin, legs]) => [coin, legs.map(leg => {
    const fields = marketMetadata.markets[leg.id];
    if (!fields) throw new Error('Инструмент отсутствует в каталоге снимка');
    return {...fields, ...leg};
  })]));
  return {...snapshot, markets, config: snapshot.config ?? marketMetadata.config};
}

async function refreshMarkets(queueIfBusy = false) {
  if (refreshing) {
    if (queueIfBusy === true) refreshQueued = true;
    return;
  }
  refreshing = true;
  try {
    const response = await fetch('/api/markets?compact=1', {signal: AbortSignal.timeout(8000),
      headers: marketEtag ? {'If-None-Match': marketEtag} : {}});
    if (response.status === 304) live.error = false;
    else {
      if (!response.ok) throw new Error(response.statusText);
      const snapshot = await response.json();
      const etag = response.headers.get('ETag');
      const changed = snapshot.generatedAt !== live.generatedAt || (etag && etag !== marketEtag) || live.error;
      if (changed) {
        updateLive(await expandSnapshot(snapshot));
        evaluateSignals();
        if (activePair) {
          const current = live.fromId(activePair.id);
          if (current) {
            if (!app.querySelector('details[open]') && !app.querySelector(':focus-visible')) renderDetail(current);
          } else if (!dialog.open) renderRoute();
        } else if (route.section === 'arbitrage') {
          if (route.id) {
            renderRoute();
            if (activePair) loadPairDetails(activePair);
          } else renderTable();
        }
      }
      marketEtag = etag;
    }
    if (activePair && [activePair.long, activePair.short].some(leg => {
      const detail = live.details.get(leg.id);
      return !detail || Date.now() >= Math.min(detail.refreshAfter,
        detail.loadedAt + (live.config?.quoteMaxAgeMs ?? Infinity));
    })) {
      loadPairDetails(activePair);
    }
  } catch {
    live.error = true;
    if (!live.generatedAt) {
      if (route.id) renderRoute();
      else if (route.section === 'arbitrage') renderTable();
    }
  } finally {
    refreshing = false;
    updateFeedLabel();
    if (refreshQueued) {
      refreshQueued = false;
      refreshMarkets();
    }
  }
}

function startEarlyQuotes() {
  if (!('EventSource' in window)) return;
  const stream = new EventSource('/api/markets/early');
  let expiryTimer, futuresTimer;
  let pendingBase = live.earlyPair?.baseAt;
  function renderEarlyChange() {
    refreshQuotePairs();
    if (route.section !== 'arbitrage') return;
    if (!route.id) renderTable();
    else if (!dialog.open && live.fromId(route.id)) renderDetail(live.fromId(route.id));
  }
  function refreshFuturesQuotes() {
    clearTimeout(futuresTimer);
    renderEarlyChange();
    const now = Date.now();
    const next = Math.min(...(live.futuresQuotes?.pairs ?? []).map(pair => pair.expiresAt).filter(at => at > now));
    if (Number.isFinite(next)) futuresTimer = setTimeout(refreshFuturesQuotes, Math.max(1, next - Date.now()));
  }
  stream.addEventListener('snapshot-ready', ({data}) => {
    const {generatedAt, intervalMs} = JSON.parse(data);
    if (!Number.isFinite(generatedAt) || !Number.isFinite(intervalMs) || intervalMs <= 0) return;
    lastSnapshotNotice = Date.now();
    snapshotInterval = intervalMs;
    refreshMarkets(true);
  });
  stream.addEventListener('futures-quotes', ({data}) => {
    const event = JSON.parse(data);
    if (!setFuturesQuotes(event)) return;
    refreshFuturesQuotes();
    if (event.baseAt > live.generatedAt) refreshMarkets(true);
  });
  stream.addEventListener('error', () => {
    lastSnapshotNotice = 0;
    if (!live.futuresQuotes) return;
    live.futuresQuotes = null;
    refreshFuturesQuotes();
  });
  stream.onmessage = ({data}) => {
    const event = JSON.parse(data);
    const completed = event.legs === null && event.baseAt > pendingBase;
    if (completed) pendingBase = undefined;
    if (setEarlyPair(event)) {
      clearTimeout(expiryTimer);
      renderEarlyChange();
      if (event.legs !== null) {
        pendingBase = event.baseAt;
        expiryTimer = setTimeout(renderEarlyChange, Math.max(0, event.expiresAt - Date.now()));
      }
    }
    if (completed && event.baseAt > live.generatedAt) refreshMarkets();
  };
}

const detailRequests = new Map();
async function getLegDetail(leg) {
  const cached = live.details.get(leg.id);
  if (cached && Date.now() < Math.min(cached.refreshAfter, cached.loadedAt + 45000)) return cached;
  if (!detailRequests.has(leg.id)) {
    detailRequests.set(leg.id, marketRequest(leg, 'detail').then(result => {
      const detail = {...result, loadedAt: Date.now()};
      live.details.set(leg.id, detail);
      live.baselineDeadline = live.nextDataChange = 0;
      for (const [id, item] of live.details) {
        if (Date.now() - item.loadedAt > 300000) live.details.delete(id);
      }
      return detail;
    }).finally(() => detailRequests.delete(leg.id)));
  }
  return detailRequests.get(leg.id);
}

async function loadPairDetails(pair) {
  const outcomes = await Promise.allSettled([getLegDetail(pair.long), getLegDetail(pair.short)]);
  rebuildPairs();
  if (activePair?.id === pair.id) {
    const current = live.fromId(pair.id);
    if (!current) return;
    const failed = outcomes.some(result => result.status === 'rejected');
    const errors = outcomes.flatMap(result => result.status === 'fulfilled' ? Object.keys(result.value.errors) : []);
    setDetailError(failed || errors.length ? 'Часть данных карточки недоступна у публичных источников.' : '');
    const focusId = document.activeElement?.id;
    renderDetail(current);
    if (focusId) document.getElementById(focusId)?.focus({preventScroll: true});
  }
}

async function requestDepth(pair) {
  if (!pair) return;
  const results = await Promise.allSettled([pair.long, pair.short].map(async leg => {
    live.books.set(leg.id, await marketRequest(leg, 'book'));
  }));
  if (activePair?.id === pair.id) updateDepthSummary();
  else if (!route.id && route.section === 'arbitrage') renderTable();
  if (results.some(result => result.status === 'rejected')) toast('Часть стаканов недоступна. Попробуйте обновить позже.');
}

async function loadExecutableSpreads() {
  if (executablePending || isDemo) return;
  const visibleIds = new Set([...document.querySelectorAll('#market-table tbody tr[data-index]')].map(row => row.dataset.key));
  const pairs = visiblePairs().slice(0, shownRows).map(([pair]) => pair).filter(pair => visibleIds.has(pair.id))
    .filter(pair => pair.verified && pair.long.type === 'swap' && pair.short.type === 'swap').slice(0, 2);
  if (!pairs.length) {
    toast('Для оценки глубины нужны видимые связки из двух подтверждённых контрактов.');
    return;
  }
  setExecutablePending(true);
  try {
    const query = new URLSearchParams(pairs.map(pair => ['pair', pair.id]));
    const response = await fetch('/api/executable-spreads?' + query, {signal: AbortSignal.timeout(10000)});
    if (!response.ok) throw new Error(await response.text());
    executableSpreads.clear();
    const results = await response.json();
    for (const [id, estimate] of Object.entries(results.pairs)) executableSpreads.set(id, estimate);
    if (!route.id && route.section === 'arbitrage') renderTable();
    for (const deadline of new Set(Object.values(results.pairs).map(result => result.expiresAt))) {
      if (deadline > Date.now()) setTimeout(() => {
        if (!route.id && route.section === 'arbitrage') renderTable();
      }, deadline - Date.now());
    }
  } catch (error) {
    toast(error.message);
  } finally {
    setExecutablePending(false);
  }
}

function updateDepthSummary() {
  const summary = document.querySelector('#depth-summary');
  if (!summary) return;
  const {long, short} = activePair;
  if (!levelsFor(long, false).length && !levelsFor(short, true).length) {
    summary.textContent = 'Оценка доступна после запроса свежих стаканов';
    return;
  }
  summary.textContent = [long, short].map((leg, i) => {
    const levels = levelsFor(leg, i === 1);
    return `${leg.exchange} · L${liquidity} ${money(depthValue(levels, liquidity))} ${leg.quote} ·
      проск. на ${nominal} ${leg.quote} ${pct(slipValue(levels, nominal, i === 1), 3)}`;
  }).join(' / ');
}

function showEstimate(pair) {
  showDialog('Оценка издержек · ' + pair.coin, `<p>Обе ноги рассчитываются на одно базовое количество.
    Выход моделируется по противоположным сторонам тех же стаканов.
    Не учтены маржа, фандинг до закрытия и переводы.</p>
    <label class="field">Общий номинал обеих ног, ${pair.long.quote}
      <input name="budget" type="number" min="1" max="1000000" step="any" value="2000" required></label>
    <div class="form-grid">${[pair.long, pair.short].map((leg, i) => `<label class="field">
      Taker ${leg.exchange}, %<input name="${i === 0 ? 'longFee' : 'shortFee'}" type="number" min="0" max="1" step="any" required></label>`).join('')}</div>
    <div id="estimate-result" role="status"></div>`, cancel() + submit('Рассчитать'), 'estimate');
}

async function calculateEstimate(pair, data) {
  const result = document.querySelector('#estimate-result');
  const submitButton = dialog.querySelector('[type="submit"]');
  const params = new URLSearchParams(data);
  params.set('long', pair.long.id);
  params.set('short', pair.short.id);
  submitButton.disabled = true;
  result.textContent = 'Проверяем синхронность стаканов и глубину.';
  try {
    const response = await fetch('/api/estimate?' + params, {signal: AbortSignal.timeout(10000)});
    if (!response.ok) throw new Error(await response.text());
    const value = await response.json();
    result.innerHTML = `<dl class="status-description"><dt>Количество на ногу</dt><dd>${price(value.quantity)} ${pair.coin}</dd>
      <dt>Номинал входа</dt><dd>${money(value.used)} ${pair.long.quote}</dd>
      <dt>VWAP входа · лонг / шорт</dt><dd>${value.entry.map(price).join(' / ')}</dd>
      <dt>VWAP выхода · лонг / шорт</dt><dd>${value.exit.map(price).join(' / ')}</dd>
      <dt>Четыре taker-комиссии</dt><dd>${money(value.fees)} ${pair.long.quote}</dd>
      <dt>Условный результат до фандинга</dt><dd>${money(value.net)} ${pair.long.quote}</dd>
      <dt>Текущая разница ставок за 8 ч</dt><dd>${signed(pair.funding, 4)}</dd></dl>`;
  } catch (error) {
    result.textContent = error.message;
  } finally {
    submitButton.disabled = false;
  }
}

function expireLiveData() {
  const now = Date.now();
  let expired = false;
  for (const [key, book] of live.books) {
    if (now - (book.sourceAt ?? book.receivedAt) > (live.config?.bookMaxAgeMs ?? Infinity)) {
      live.books.delete(key);
      expired = true;
    }
  }
  if (expired) {
    if (activePair) updateDepthSummary();
    else if (route.section === 'arbitrage' && !route.id) renderTable();
  }
  if (now >= live.nextDataChange) {
    refreshQuotePairs(now);
    if (activePair && live.fromId(activePair.id)) renderDetail(live.fromId(activePair.id));
    else if (route.section === 'arbitrage' && !route.id) renderTable();
  }
}

function showPresets() {
  const rows = presets.map((preset, i) => `<div class="form-actions">
    ${button(esc(preset.name), 'apply-preset', 'control', `data-index="${i}"`)}
    ${button('Удалить', 'delete-preset', 'plain', `data-index="${i}"`)}</div>`).join('');
  showDialog('Пресеты фильтров', `${rows || '<p>Сохранённых пресетов пока нет.</p>'}
    <label class="field">Название текущего набора<input name="name" required maxlength="48"></label>`,
  cancel() + submit('Сохранить текущий'), 'presets');
}

function showSignals(pair) {
  if (isDemo) return showDialog('Сигналы', '<p>Локальные сигналы доступны при подключении публичных API.</p>');
  const target = pair?.id ? pair : null;
  const choices = target ? {[target.id]: target.coin + ' · ' + target.long.exchange + ' → ' + target.short.exchange} :
    Object.fromEntries(visiblePairs().slice(0, 80).map(([item]) => [
      item.id, item.coin + ' · ' + item.long.exchange + ' → ' + item.short.exchange + ' · ' + item.long.quote,
    ]));
  const rules = signalRules.map((rule, i) => `<div class="form-actions"><span>${esc(rule.label)} ·
    ${rule.metric === 'entry' ? 'Вход' : 'Фандинг 8 ч'} ≥ ${pct(rule.threshold, 3)}</span>
    ${button('Удалить', 'remove-signal', 'plain', `data-index="${i}"`)}</div>`).join('');
  const records = signalLog.slice(0, 10).map(item =>
    `<li>${new Date(item.at).toLocaleString('ru')} · ${esc(item.label)} · ${signed(item.value, 3)}</li>`).join('');
  showDialog('Локальные сигналы', `<p>Сигнал фиксируется при пересечении порога на двух свежих
    сопоставимых снимках подтверждённой связки, пока открыта страница.</p>${rules}
    ${records ? `<details><summary>Журнал</summary><ul>${records}</ul></details>` : ''}
    ${Object.keys(choices).length ? `<label class="field">Связка<select name="pair">${options(choices)}</select></label>
      <label class="field">Метрика<select name="metric">${options({entry: 'Спред входа', funding: 'Спред фандинга 8 ч'})}</select></label>
      <label class="field">Порог, %<input name="threshold" type="number" step="any" value="0.5" required></label>` :
      '<p>Откройте карточку или выберите фильтры с доступными связками.</p>'}`,
  cancel() + (Object.keys(choices).length ? submit('Сохранить правило') : ''), 'signals');
}

function evaluateSignals() {
  for (const rule of signalRules) {
    const current = live.fromId(rule.pair);
    const pair = current?.baseline ?? current;
    const value = pair?.[rule.metric];
    if (pair?.early) continue;
    if (!pair?.verified || pair.quality !== 'timed' || !Number.isFinite(value)) {
      signalStates.delete(rule.key);
      continue;
    }
    const below = value < rule.threshold;
    const previous = signalStates.get(rule.key);
    if (below || !previous) {
      signalStates.set(rule.key, {armed: below, observations: 0, last: previous?.last ?? 0});
    } else if (previous.armed && ++previous.observations >= 2 && Date.now() - previous.last >= 60000) {
      const at = Date.now();
      signalLog = [{at, label: rule.label, value}, ...signalLog].slice(0, 100);
      savePreference('signal-log', signalLog);
      signalStates.set(rule.key, {armed: false, observations: 0, last: at});
      toast(`Сигнал · ${rule.label} · ${signed(value, 3)}`);
    }
  }
}

let historyRequest = 0;
async function showLiveHistory(pair, selected, days) {
  const request = ++historyRequest;
  const tabs = [['', 'Сравнение'], ['0', pair.long.exchange], ['1', pair.short.exchange]].map(([key, label]) =>
    button(label, 'history', '', `data-pair="${pair.id}" data-leg="${key}" data-days="${days}" aria-selected="${key === selected}"`)).join('');
  const periods = [[1, '24 ч'], [3, '3 дня'], [7, '7 дней']].map(([value, label]) =>
    button(label, 'history', '', `data-pair="${pair.id}" data-leg="${selected}" data-days="${value}" aria-selected="${value === days}"`)).join('');
  const heading = 'История фандинга · ' + pair.coin;
  const present = body => showDialog(heading, `<div class="chart-tabs">${tabs}</div>
    <div class="chart-tabs">${periods}</div>${body}`, button('Закрыть', 'close', 'control'), 'funding-history');
  present('<p role="status">Загружаем расчётные события бирж.</p>');
  const results = await Promise.allSettled([getLegDetail(pair.long), getLegDetail(pair.short)]);
  if (request !== historyRequest || !dialog.open || !dialog.querySelector('[data-form="funding-history"]')) return;
  const [left, right] = results.map(result => result.status === 'fulfilled' ? result.value : {});
  const events = [new Map(left.events ?? []), new Map(right.events ?? [])];
  const start = Date.now() - days * 86400000;
  const dates = [...new Set((selected === '' ? events.flatMap(map => [...map.keys()]) :
    [...events[Number(selected)].keys()]).filter(stamp => stamp > start))].sort((a, b) => a - b);
  const comparison = selected === '';
  const totals = [left.totals?.[days] ?? (pair.long.type === 'spot' ? 0 : null), right.totals?.[days] ?? null];
  const complete = totals.every(Number.isFinite);
  let running = [0, 0];
  const rows = dates.map(stamp => {
    const rates = events.map(map => map.get(stamp));
    running = running.map((total, i) => total + (rates[i] ?? 0));
    const index = Number(selected);
    return `<tr><td>${new Date(stamp).toLocaleString('ru')}</td>${comparison ?
      `<td>${signed(rates[0], 4)}</td><td>${signed(rates[1], 4)}</td><td>${signed(complete ? running[1] - running[0] : null, 4)}</td>` :
      `<td>${signed(rates[index], 4)}</td><td>${signed(Number.isFinite(totals[index]) ? running[index] : null, 4)}</td>`}</tr>`;
  }).reverse().join('');
  present(`<p>Опубликованные ставки за ${days === 1 ? '24 ч' : days + ' дн.'} ${complete ? 'Разница ' + signed(totals[1] - totals[0], 4) : 'Покрытие одной или обеих ног неполное.'}</p>
    ${rows ? `<table class="mini-table"><thead><tr><th>Время</th>${comparison ?
      `<th>${pair.long.exchange}</th><th>${pair.short.exchange}</th><th>Разница</th>` :
      '<th>Ставка</th><th>Накопление</th>'}</tr></thead><tbody>${rows}</tbody></table>` : '<p>События за этот период отсутствуют.</p>'}`);
  if (!route.id && route.section === 'arbitrage') {
    rebuildPairs();
    renderTable();
  }
}

function chartSamples(pair) {
  const funding = chartTab === 'funding';
  const step = funding ? 8 * 3600000 : parseInt(interval) * (interval.endsWith('h') ? 3600000 : 60000);
  const width = funding ? 7 * 86400000 : Math.max(3600000, 30 * step);
  const end = Date.now() + chartOffset * width * 0.75;
  const start = end - width;
  const raw = [pair.long, pair.short].map(leg => live.details.get(leg.id)?.samples ?? []);
  const points = new Map();
  if (funding) {
    [pair.long, pair.short].forEach((leg, i) => {
      for (const [stamp, rate] of live.details.get(leg.id)?.events ?? []) {
        if (stamp < start || stamp > end) continue;
        if (!points.has(stamp)) points.set(stamp, [stamp, [null, null]]);
        points.get(stamp)[1][i] = rate;
      }
    });
  } else if (chartTab === 'spread') {
    const right = new Map(raw[1].map(row => [row[0], row]));
    for (const [stamp, sourceAt, receivedAt, bid, ask] of raw[0]) {
      const short = right.get(stamp);
      if (stamp < start || stamp > end || !short) continue;
      const left = {sourceAt, receivedAt, bid, ask};
      const other = {sourceAt: short[1], receivedAt: short[2], bid: short[3], ask: short[4]};
      const quality = spreadQuality(left, other, Math.max(receivedAt, other.receivedAt));
      if (!['timed', 'receipt'].includes(quality)) continue;
      const key = Math.floor(stamp / step) * step;
      points.set(key, [stamp, [openSpread(ask, other.bid), closeSpread(bid, other.ask)]]);
    }
  } else {
    const column = {price: 5, index: 6, oi: 7}[chartTab];
    raw.forEach((rows, i) => {
      for (const row of rows) {
        if (row[0] < start || row[0] > end || !Number.isFinite(row[column])) continue;
        const key = Math.floor(row[0] / step) * step;
        if (!points.has(key)) points.set(key, [Math.max(start, key), [null, null]]);
        points.get(key)[1][i] = row[column];
      }
    });
  }
  return {start, end, step, points: [...points.values()].sort((a, b) => a[0] - b[0])};
}

function indexComposition(pair) {
  return `<section class="chart-frame contract-data"><h2>Состав индекса</h2>
    <div class="venue-grid">${[pair.long, pair.short].map(leg => {
      const detail = live.details.get(leg.id);
      const index = detail?.indexComposition;
      const available = index && (index.fromCatalog || Date.now() - index.observedAt <= 300000);
      return `<section><h3>${esc(leg.exchange)}${available ? ' · ' + esc(index.name) : ''}</h3>
        ${available ? `<p class="muted">${index.fromCatalog ? 'Источники из каталога' : 'Снимок биржи'}${index.observedAt ?
          ' · ' + new Date(index.observedAt).toLocaleString('ru') : ''}</p>
          <table class="mini-table"><thead><tr><th>Источник</th><th>Пара</th><th>Вес</th></tr></thead>
          <tbody>${index.components.map(component => `<tr><td>${esc(component.exchange)}</td>
            <td>${esc(component.symbol ?? '—')}</td><td>${component.weight === null ? '—' : price(component.weight * 100) + '%'}</td></tr>`).join('')}</tbody></table>` :
          `<p class="muted">${detail?.indexUnavailable || index ? 'Свежий состав индекса временно недоступен.' :
            leg.type === 'spot' ? 'Спотовый рынок.' : detail ? 'Данные о составе индекса недоступны.' : 'Ожидаем данные карточки.'}</p>`}
      </section>`;
    }).join('')}</div></section>`;
}

function contractLimits(pair) {
  return `<section class="contract-data"><h2>Лимиты контрактов</h2><div class="venue-grid">
    ${[pair.long, pair.short].map(leg => {
      const detail = live.details.get(leg.id);
      const limits = detail?.contractLimits;
      const unit = limits?.inContracts ? 'контр.' : pair.coin;
      const fields = {
        orderMin: ['Минимум ордера', unit],
        marketMin: ['Минимум рыночного ордера', unit], marketMax: ['Максимум рыночного ордера', unit],
        limitMin: ['Минимум лимитного ордера', unit], limitMax: ['Максимум лимитного ордера', unit],
        notionalMin: ['Минимальный номинал ордера', leg.quote],
        riskBase: ['Базовый риск-лимит позиции', unit], riskBaseLong: ['Базовый риск-лимит лонга', unit],
        riskBaseShort: ['Базовый риск-лимит шорта', unit],
        positionFixedUsd: ['Фиксированный порог лимита позиции', 'USD'],
        positionSharePct: ['Доля OI для лимита позиции', '%'],
        platformPositionUsd: ['Общий лимит OI площадки', 'USD'], platformPositionCoins: ['Общий лимит OI площадки', pair.coin],
      };
      return `<section><h3>${esc(leg.exchange)} · ${leg === pair.long ? 'лонг' : 'шорт'}</h3>
        ${limits ? `<dl class="status-description">${Object.entries(fields).filter(([key]) => Number.isFinite(limits[key]))
          .map(([key, [label, suffix]]) => `<dt>${label}</dt><dd>${price(limits[key])} ${esc(suffix)}</dd>`).join('')}</dl>
          ${limits.inContracts && limits.contractSize > 0 ? `<p class="muted">1 контракт = ${price(limits.contractSize)} ${esc(pair.coin)}</p>` : ''}
          ${limits.positionFixedUsd && limits.positionSharePct ? '<p class="muted">OKX берёт больший из фиксированного порога и указанной доли OI в USD.</p>' : ''}` :
          `<p class="muted">${leg.type === 'spot' ? 'Спотовый рынок.' : detail ? 'Публичные лимиты недоступны.' : 'Ожидаем данные карточки.'}</p>`}
      </section>`;
    }).join('')}</div><p class="muted">Публичные правила рынка. Доступный объём по счёту зависит от позиции и настроек маржи.</p></section>`;
}

function renderLiveChart(panel) {
  const pair = activePair;
  if (chartTab === 'info') {
    panel.innerHTML = `<div class="chart-frame chart-info"><h2>О связке</h2>
      <p>${pair.coin} · ${pair.long.exchange} (${pair.long.type === 'spot' ? 'спот' : 'лонг'}) / ${pair.short.exchange} (шорт).
      Валюта котировки ${pair.long.quote}.</p>
      <p>${liveQuality[pair.quality]}. ${pair.verified ? 'Контракты сверены по реестру.' :
        'Совпадение тикера требует проверки идентичности.'}</p>
      ${pair.valuationAt ? `<p>CoinGecko · ${new Date(pair.valuationAt).toLocaleString('ru')}<br>
        Капитализация, млн $ · ${money(pair.cap)}<br>FDV, млн $ · ${money(pair.fdv)}</p>` : ''}
      <p>Вход делит разницу между Bid шорта и Ask лонга на среднее этих цен. Выход сравнивает Bid лонга с Ask шорта.
        APR экстраполирует текущие ставки. Графики используют наблюдения локального монитора.</p>
      ${contractLimits(pair)}</div>`;
    return;
  }
  const {points, start, end, step} = chartSamples(pair);
  const values = points.flatMap(([, entries]) => entries.filter(Number.isFinite));
  const isSpread = chartTab === 'spread';
  const height = Math.max(160, Math.min(420, Number(chartOptions.height) || 244));
  const lower = Math.min(...values), upper = Math.max(...values);
  const padding = Math.max((upper - lower) * 0.08, Math.abs(upper) * 0.0001, 1e-12);
  const x = stamp => 12 + (stamp - start) / (end - start) * 550;
  const y = value => height - 24 - (value - lower + padding) / (upper - lower + 2 * padding) * (height - 55);
  const isFunding = chartTab === 'funding';
  const format = value => isSpread || isFunding ? signed(value, 3) : price(value);
  const labels = isSpread ? ['Вход', 'Выход'] : [pair.long.exchange, pair.short.exchange];
  const colors = ['#2abd81', '#ee6878'];
  const lines = colors.map((color, i) => {
    let previous = null;
    const commands = points.map(([stamp, row]) => {
      const value = row[i];
      if (!Number.isFinite(value)) {
        previous = null;
        return '';
      }
      const command = previous !== null && stamp - previous <= step * 1.5 ? 'L' : 'M';
      previous = stamp;
      return `${command}${x(stamp)},${y(value)}`;
    }).join(' ');
    const dots = points.filter(([, row]) => Number.isFinite(row[i])).map(([stamp, row]) =>
      `<circle cx="${x(stamp)}" cy="${y(row[i])}" r="${isFunding ? 2.7 : 1.6}" fill="${color}">${chartOptions.tooltip ?
        `<title>${new Date(stamp).toLocaleString('ru')} · ${format(row[i])}</title>` : ''}</circle>`).join('');
    return `<path d="${commands}" fill="none" stroke="${color}" stroke-width="1.5"/>${dots}`;
  }).join('');
  const axes = values.length ? Array.from({length: 4}, (_, i) => {
    const value = lower + (upper - lower) * i / 3;
    return `<path d="M12 ${y(value)}H562" stroke="var(--grid)"/>
      <text x="570" y="${y(value) + 4}" fill="var(--muted)">${format(value)}</text>`;
  }).join('') : '';
  panel.innerHTML = `<div class="chart-frame live-chart" id="chart-frame">
    <div class="live-chart-head"><span>${labels.map((label, i) => `<b style="color:${colors[i]}">${label}</b>`).join(' / ')}</span>
      ${isFunding ? '<small>События за 7 дней</small>' : `<select class="control" id="interval" aria-label="Интервал графика">${options(intervals, interval)}</select>`}</div>
    ${values.length ? `<svg class="chart-svg" viewBox="0 0 680 ${height}" role="img" aria-label="${chartTabs[chartTab]} · публичные данные">
      <g font-size="11">${axes}
        <text x="12" y="${height - 3}" fill="var(--muted)">${new Date(start).toLocaleString('ru')}</text>
        <text x="562" y="${height - 3}" text-anchor="end" fill="var(--muted)">${new Date(end).toLocaleString('ru')}</text></g>
      ${lines}${chartOptions.labels ? colors.map((color, i) => {
        const point = points.findLast(([, row]) => Number.isFinite(row[i]));
        return point ? `<text x="${x(point[0]) - 5}" y="${y(point[1][i]) - 8}" text-anchor="end" fill="${color}" font-size="11">${format(point[1][i])}</text>` : '';
      }).join('') : ''}
    </svg>` : `<p class="muted chart-info">${isFunding ? 'За выбранный период ставки не получены.' :
      'За выбранный период наблюдений нет. История накапливается во время работы монитора.'}</p>`}
    <div class="chart-tools">
      ${button('←', 'chart-left', '', 'aria-label="График влево"')}
      ${button('→', 'chart-right', '', 'aria-label="График вправо"')}
      ${button('⇥', 'chart-end', '', 'aria-label="К последним данным"')}
      ${button('↻', 'chart-reset', '', 'aria-label="Сбросить график"')}
      ${button(icon('settings'), 'chart-settings', '', 'aria-label="Настройки графика"')}
      ${button('⛶', 'fullscreen', '', 'aria-label="На весь экран"')}
    </div>
  </div>
  ${isSpread ? `<p class="chart-history-link">${button('История фандинга', 'history', 'control', `data-pair="${pair.id}"`)}</p>` : ''}
  ${chartTab === 'index' ? indexComposition(pair) : ''}`;
}

export {
  calculateEstimate, expireLiveData, loadExecutableSpreads, loadPairDetails, refreshMarkets,
  refreshMarketsFallback, renderLiveChart, requestDepth, showEstimate, showLiveHistory,
  showPresets, showSignals, startEarlyQuotes, updateDepthSummary, updateFeedLabel,
};
