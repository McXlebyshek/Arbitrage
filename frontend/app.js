const app = document.querySelector('#app');
const dialog = document.querySelector('#dialog');
const {readPreference, savePreference, listSettings: decodeListSettings, updateRows, visibleRange} = globalThis.monitorModules;
const icons = {
  search: '<circle cx="10.5" cy="10.5" r="5.5"/><path d="m15 15 4 4"/>',
  link: '<path d="m10 8 2-2a4 4 0 0 1 6 6l-2 2m-2 2-2 2a4 4 0 0 1-6-6l2-2m1 5 6-6"/>',
  filters: '<path d="M4 6h16M4 12h16M4 18h16M9 3v6m6 0v6m-6 0v6"/>',
  heart: '<path d="M12 20 4 12a4.5 4.5 0 0 1 8-4 4.5 4.5 0 0 1 8 4z"/>',
  grid: '<rect x="4" y="4" width="6" height="6" rx="1"/><rect x="14" y="4" width="6" height="6" rx="1"/><rect x="4" y="14" width="6" height="6" rx="1"/><rect x="14" y="14" width="6" height="6" rx="1"/>',
  moon: '<path d="M20 15A8.5 8.5 0 0 1 9 4a8.5 8.5 0 1 0 11 11Z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2"/>',
  clock: '<circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 2"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>',
  send: '<path d="m3 10 18-7-5 18-5-7-8-4Zm8 4 10-11"/>',
  arrow: '<path d="M19 12H5m6-6-6 6 6 6"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  chart: '<path d="M4 4v16h16M7 15l4-5 4 3 5-7"/>',
  bars: '<path d="M5 20v-7m5 7V9m5 11V5m5 15V2"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="m9 4 1-2h4l1 2 2 1 2 .2 2 3-1 2v3l1 2-2 3-2 .2-2 1-1 2h-4l-1-2-2-1-2-.2-2-3 1-2v-3l-1-2 2-3L7 5Z"/>',
};
function icon(name) {
  return `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name]}</svg>`;
}
const esc = value => String(value).replace(/[&<>"']/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[character]));
const dataSource = isDemo ? demo : live;
const pct = (n, decimals = 2) => Number.isFinite(n) ? `${n.toFixed(decimals)}%` : '—';
const price = n => Number.isFinite(n) ? Number(n.toPrecision(8)).toLocaleString('ru', {maximumFractionDigits: 14}) : '—';
const money = n => Number.isFinite(n) ? n.toLocaleString('ru', {maximumFractionDigits: 2}) : '—';
const signed = (n, decimals = 2) => (n > 0 ? '+' : '') + pct(n, decimals);
const tones = n => n > 0.05 ? 'positive' : n < -0.15 ? 'negative' : 'neutral';
const defaults = {mode: 'funding', sort: isDemo ? 'entry' : 'funding', direction: 'desc', group: true, categories: ['crypto']};
const modes = {
  funding: 'Лонг + шорт · фандинг',
  spread: 'Лонг + шорт · спред',
  spot: 'Спот + шорт · фандинг',
  spotSpread: 'Спот + шорт · спред',
  negative: 'Спот + шорт · отриц. спред',
};
const sections = {
  arbitrage: 'Арбитраж', funding: 'Фандинг', staking: 'Стейкинг',
  loans: 'Криптозаймы', launchpools: 'Лаунчпулы', premarket: 'Премаркет',
};
const tables = {balances: 'Балансы бирж', rates: 'Курсы валют', comparison: 'Сравнение бирж'};
const exchanges = Object.keys(dataSource.exchanges).sort();
const categories = {crypto: 'Криптовалюты', stock: 'Акции', commodity: 'Сырьё'};
const quotes = isDemo ? ['USDT', 'USDC', 'USD1', 'USD', 'USDG'] : ['USDT', 'USDC'];
const numericFilters = {
  volume: 'Объём каждой ноги от, млн', spread: 'Спред входа, %', apr: 'Экстраполированный APR от, %',
  aprAfterFees: 'APR после комиссий от, %',
  funding: 'Спред фандинга за 8 ч от, %',
  capMin: 'Капитализация от, млн $', capMax: 'Капитализация до, млн $',
  fdvMin: 'Полная оценка (FDV) от, млн $', fdvMax: 'Полная оценка (FDV) до, млн $',
  delta: 'Фандинг за 24 ч от, %', delta3: 'Фандинг за 3 дня от, %', delta7: 'Фандинг за 7 дней от, %',
};
const columnLabels = {fund: 'Фандинг', delta: 'Фанд. 24 ч', apr: 'Экстраполированный APR', aprAfterFees: 'APR после комиссий',
  spread: 'Спред 8 ч', entry: 'Спред входа', executable: 'Спред по глубине', status: 'Статус', liq: 'Ликвидность', slip: 'Проскальзывание'};
let theme = readPreference('theme', 'dark') === 'light' ? 'light' : 'dark';
let favorites = new Set(readPreference('favorites', []));
let layout = readPreference('layout', 'regular') === 'compact' ? 'compact' : 'regular';
let liquidity = 1;
let nominal = 2000;
let chartOptions = {...{height: 244, labels: true, areas: true, tooltip: true}, ...readPreference('chart', {})};
let wide = readPreference('wide', false);
let chartTab = 'spread';
let interval = '1m';
let chartOffset = 0;
let countdown = 15;
let toastTimeout;
let shownRows = 80;
let hiddenColumns = new Set(readPreference('hidden-columns', []));
let presets = readPreference('presets', []);
let signalRules = readPreference('signal-rules', []);
let signalLog = readPreference('signal-log', []);
const signalStates = new Map();
let refreshing = false;
let detailError = '';
const executableSpreads = new Map();
let executablePending = false;

function executableCell(id) {
  const value = executableSpreads.get(id);
  if (value?.error) return `<td class="metric" title="${esc(value.error)}">—</td>`;
  return `<td class="metric">${value?.expiresAt > Date.now() ? pct(value.spread, 3) : '—'}</td>`;
}

function currentRoute() {
  const [path, query] = location.hash.slice(1).split('?');
  const [, section = 'arbitrage', id] = (path || '/arbitrage').split('/');
  return {section, id, params: new URLSearchParams(query)};
}
function listSettings(params) {
  return decodeListSettings(params, {numericFilters, modes, defaults, categories, exchanges, quotes});
}
let route = currentRoute();
let state = listSettings(route.params);

function listQuery() {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(state)) {
    if (key === 'query') {
      if (value) params.set('coin', value);
    } else if (key === 'exchanges') {
      if (value.length !== exchanges.length) params.set(key, value.join(','));
    } else if (Array.isArray(value)) {
      if (value.join(',') !== (defaults[key] ?? []).join(',')) params.set(key, value.join(','));
    } else if (key === 'group') {
      if (!value) params.set(key, '0');
    } else if (value && value !== defaults[key] && value !== 'gte') {
      params.set(key, value === true ? '1' : value);
    }
  }
  return params.size ? `?${params}` : '';
}
function routeHref(id = '') {
  return `#/arbitrage${id ? '/' + id : ''}${listQuery()}`;
}
function applyList(patch, tableOnly = false) {
  shownRows = 80;
  Object.assign(state, patch);
  history.replaceState(null, '', routeHref());
  if (tableOnly) renderTable();
  else renderScreener();
}

function menu(label, content, classes = '') {
  return `<details class="menu ${classes}"><summary>${label}${icon('chevron')}</summary><div class="menu-panel">${content}</div></details>`;
}
function button(label, action, classes = '', extra = '') {
  return `<button type="button" class="${classes}" data-action="${action}" ${extra}>${label}</button>`;
}
function options(items, selected) {
  return Object.entries(items).map(([value, label]) =>
    `<option value="${esc(value)}"${String(selected) === value ? ' selected' : ''}>${esc(label)}</option>`).join('');
}
function toast(message) {
  const element = document.querySelector('#toast');
  element.textContent = message;
  element.classList.add('visible');
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => element.classList.remove('visible'), 2700);
}
function showDialog(title, content, actions = '', formName = '') {
  dialog.innerHTML = `<div class="dialog-head"><h2 id="dialog-title"></h2>
    ${button(icon('close'), 'close', 'icon-button', 'aria-label="Закрыть"')}</div>
    <form data-form="${formName}"><div class="dialog-body">${content}</div>
    ${actions ? `<div class="dialog-actions">${actions}</div>` : ''}</form>`;
  dialog.querySelector('#dialog-title').textContent = title;
  if (!dialog.open) dialog.showModal();
}
const cancel = () => button('Отмена', 'close', 'control');
const submit = label => `<button type="submit" class="control primary">${label}</button>`;
const badge = (exchange) => {
  const [mark, color] = dataSource.exchanges[exchange];
  return `<span class="exchange-mark" style="--mark:${color}" aria-hidden="true">${mark}</span>`;
};
const arrow = short => `<span class="direction ${short ? 'short' : 'long'}" title="${short ? 'Шорт' : 'Лонг'}">${short ? '↓' : '↑'}</span>`;

function renderHeader() {
  document.documentElement.dataset.theme = theme;
  document.querySelector('#navigation').innerHTML = Object.entries(sections).map(([key, label]) =>
    `<a href="#/${key}" ${route.section === key ? 'aria-current="page"' : ''}>${label}${key === 'launchpools' ? '<span class="nav-count">3</span>' : ''}</a>`).join('') +
    menu('Таблицы', Object.entries(tables).map(([key, label]) => `<a href="#/${key}">${label}</a>`).join(''), 'nav-menu');
  document.querySelector('#data-source').textContent = isDemo ? 'Демонстрационный интерфейс' : 'Публичные API бирж';
  document.querySelector('#header-actions').innerHTML = menu(isDemo ? 'Демо-профиль' : 'Локальный профиль',
    button('О профиле', 'profile', 'menu-item') + button('Справка', 'help', 'menu-item'), 'align-right profile-menu') +
    button(icon(theme === 'dark' ? 'moon' : 'sun'), 'theme', 'icon-button theme-button',
      `aria-label="${theme === 'dark' ? 'Светлая тема' : 'Тёмная тема'}"`);
}

function createMatcher() {
  const spotMode = ['spot', 'spotSpread', 'negative'].includes(state.mode);
  const needsFunding = !isDemo && ['funding', 'spot'].includes(state.mode);
  const query = state.query.trim().toUpperCase();
  const selectedExchanges = new Set(state.exchanges);
  const coinMatches = new Map();
  const minima = ['volume', 'apr', 'aprAfterFees', 'funding', 'capMin', 'fdvMin', 'delta', 'delta3', 'delta7']
    .filter(key => state[key]).map(key => [key === 'capMin' ? 'cap' : key === 'fdvMin' ? 'fdv' : key, Number(state[key])]);
  const spread = state.spread ? Number(state.spread) : null;
  const capMax = state.capMax ? Number(state.capMax) : null;
  const fdvMax = state.fdvMax ? Number(state.fdvMax) : null;
  return pair => {
    const {coin, long, short, entry, cap, fdv, category} = pair;
    const spot = long.type === 'spot';
    if (spot !== spotMode) return false;
    if (state.mode === 'negative' && !(entry < 0)) return false;
    if (state.mode === 'spread' && !(isDemo ? entry > 0 : Number.isFinite(entry))) return false;
    if (state.mode === 'spotSpread' && !(entry > 0)) return false;
    if (needsFunding && !Number.isFinite(pair.funding)) return false;
    if (!coinMatches.has(coin)) coinMatches.set(coin, coin.includes(query) || (!isDemo &&
      live.markets[coin]?.legs.some(leg => leg.nativeBase?.toUpperCase().includes(query))));
    const matchesCoin = coinMatches.get(coin);
    if (!matchesCoin || (state.expand && state.expand !== coin)) return false;
    if (!selectedExchanges.has(long.exchange) || !selectedExchanges.has(short.exchange)) return false;
    if (state.favorites && !favorites.has(coin)) return false;
    if (state.categories.length && !state.categories.includes(category)) return false;
    if (state.quotes.length && (!state.quotes.includes(long.quote) || !state.quotes.includes(short.quote))) return false;
    if (spread !== null && (!Number.isFinite(entry) ||
        (state.operator === 'lte' ? entry > spread : entry < spread))) return false;
    for (const [key, minimum] of minima) {
      const value = key === 'volume' ? (Number.isFinite(long.volume) && Number.isFinite(short.volume)
        ? Math.min(long.volume, short.volume) : null) : key === 'aprAfterFees' ? aprAfterFees(pair, state) : pair[key];
      if (!Number.isFinite(value) || value < minimum) return false;
    }
    return (capMax === null || (Number.isFinite(cap) && cap <= capMax)) &&
      (fdvMax === null || (Number.isFinite(fdv) && fdv <= fdvMax));
  };
}
function visiblePairs() {
  return selectPairs(dataSource.pairs, createMatcher(), state.sort, state.direction, state.group && !state.expand,
    state.sort === 'aprAfterFees' ? pair => aprAfterFees(pair, state) : undefined);
}
function status(leg, id, index) {
  return `<div class="status-line">${['Ввод', 'Вывод'].map((label, position) => {
    const value = leg.status[position];
    const css = value === undefined ? 'unknown' : value ? 'available' : 'unavailable';
    return button(label[0] + (position === 0 ? 'в' : 'ы'), 'status', `plain ${css}`,
      `data-pair="${id}" data-leg="${index}" title="${label} · ${leg.exchange}" aria-label="${label} · ${leg.exchange}"`);
  }).join('')}</div>`;
}
function exchangeLine(leg, short, id) {
  return `<div class="market-line">${arrow(short)}
    ${button(badge(leg.exchange) + leg.exchange, 'market', 'plain market-name', `data-pair="${id}" data-leg="${Number(short)}"`)}
    ${leg.delist ? '<small class="delist" title="Предупреждение о делистинге">Делистинг</small>' : ''}
    ${leg.quote !== 'USDT' ? `<small class="quote">${leg.quote}</small>` : ''}
    <span class="price">${isDemo ? leg.price : price(short ? leg.bid : leg.ask)}</span></div>`;
}
function fundingLine(leg) {
  return leg.type === 'spot' ? '<div class="fund-line muted">Спот</div>' :
    `<div class="fund-line"><span><b>${pct(leg.rate, 4)}</b> <small>${leg.hours ? leg.hours + ' ч' : ''}</small></span>${fundingClock(leg)}</div>`;
}
function depthCell(pair, slip) {
  if (isDemo) return '<span class="muted">—<br>—</span>';
  return [pair.long, pair.short].map((leg, index) => {
    const levels = levelsFor(leg, index === 1);
    const value = slip ? pct(slipValue(levels, nominal, index === 1), 3) : money(depthValue(levels, liquidity));
    return `<span class="depth-value">${value}</span>`;
  }).join('<br>');
}
function row([pair, more]) {
  const {id, coin, long, short, delta, apr, funding, entry} = pair;
  return `<tr>
    <td class="coin-cell"><a href="${routeHref(id)}">${coin}</a>${favorites.has(coin) ? '<span class="star" title="В избранном">★</span>' : ''}
      ${more ? button('+' + more, 'expand', 'plain expand', `data-coin="${coin}" aria-label="Другие связки ${coin}"`) : ''}</td>
    <td><div class="legs">${exchangeLine(long, false, id)}${exchangeLine(short, true, id)}</div></td>
    <td><div class="legs">${fundingLine(long)}${fundingLine(short)}</div></td>
    <td class="metric">${button(signed(delta), 'history', `metric-chip ${tones(delta)}`, `data-pair="${id}" title="История фандинга ${coin}"`)}</td>
    <td class="metric apr">${pct(apr)}</td>
    <td class="metric apr">${pct(aprAfterFees(pair, state))}</td>
    <td class="metric"><span class="metric-chip ${tones(funding)}">${pct(funding)}</span></td>
    <td class="metric"><span class="metric-chip ${tones(entry)}" title="${isDemo ? '' : liveQuality[pair.quality] + (pair.timestampSuspect ? ' · биржевые метки расходятся' : '') + (pair.verified ? ' · реестр' : ' · совпадение тикеров')}">${!isDemo && Number.isFinite(entry) && (!pair.verified || pair.quality === 'receipt' || pair.timestampSuspect) ? '≈ ' : ''}${pct(entry)}</span></td>
    ${executableCell(id)}
    <td>${status(long, id, 0)}${status(short, id, 1)}</td>
    <td class="metric muted dash">${depthCell(pair, false)}${isDemo ? '' : button('Стакан', 'depth', 'plain depth-request', `data-pair="${id}"`)}</td>
    <td class="metric muted dash">${depthCell(pair, true)}</td>
  </tr>`;
}
function sortHeader(key, label, title = '') {
  const active = key === state.sort;
  return `<th scope="col"${active ? ` aria-sort="${state.direction === 'desc' ? 'descending' : 'ascending'}"` : ''}>
    ${button(`${label} <span class="sort-arrow ${active ? 'sorted' : ''}">${active ? state.direction === 'asc' ? '▴' : '▾' : '▿'}</span>`, 'sort', 'plain sort', `data-key="${key}" title="${title || label}"`)}</th>`;
}
let tableFrame = 0;
function renderTable() {
  if (!document.querySelector('#market-table > table')) {
    if (tableFrame) cancelAnimationFrame(tableFrame);
    tableFrame = 0;
    updateTable();
    return;
  }
  if (!tableFrame) tableFrame = requestAnimationFrame(() => {
    tableFrame = 0;
    updateTable();
  });
}

function updateTable() {
  const rows = visiblePairs();
  const table = document.querySelector('#market-table');
  if (!table) return;
  const columns = Object.keys(columnLabels);
  const total = Math.min(shownRows, rows.length);
  const measured = table.querySelector('tbody tr[data-index]')?.getBoundingClientRect().height;
  const rowHeight = measured || (layout === 'compact' ? 49 : 64);
  const [first, last] = visibleRange(total, table.getBoundingClientRect().top, innerHeight, rowHeight);
  const focused = document.activeElement?.closest('tbody tr[data-index]');
  const focusedIndex = focused && table.contains(focused) ? rows.findIndex(([pair]) => pair.id === focused.dataset.key) : -1;
  const indexes = Array.from({length: last - first}, (_, index) => first + index);
  if (focusedIndex >= 0 && focusedIndex < total && (focusedIndex < first || focusedIndex >= last)) indexes.push(focusedIndex);
  indexes.sort((a, b) => a - b);
  const spacer = count => `<tr aria-hidden="true"><td colspan="12" style="height:${Math.round(count * rowHeight)}px;padding:0;border:0"></td></tr>`;
  const pieces = [], keys = [];
  let previous = 0;
  for (const index of indexes) {
    if (index > previous) { pieces.push(spacer(index - previous)); keys.push(`$gap:${previous}`); }
    pieces.push(row(rows[index]).replace('<tr>', `<tr data-index="${index}" aria-rowindex="${index + 2}">`));
    keys.push(rows[index][0].id);
    previous = index + 1;
  }
  if (previous < total) { pieces.push(spacer(total - previous)); keys.push(`$gap:${previous}`); }
  const body = rows.length ? pieces.join('') :
    `<tr><td colspan="12" class="empty"><strong>${isDemo || (live.generatedAt && !live.error) ? 'Связки не найдены' :
      live.error ? 'Сервер данных недоступен' : 'Загружаем публичные данные'}</strong><p>${live.error && !isDemo ? 'Запустите Start-UI.cmd или повторите обновление.' : 'Измените условия поиска или сбросьте фильтры.'}</p>${button('Сбросить фильтры', 'reset', 'control')}</td></tr>`;
  if (!rows.length) keys.push('$empty');
  const hiddenIndices = [...hiddenColumns].map(column => columns.indexOf(column) + 3).filter(index => index > 2);
  const html = `<table class="${layout === 'compact' ? 'compact' : ''}" aria-rowcount="${total + 1}">
    <colgroup><col class="coin-col"><col class="pair-col"><col class="fund-col"><col class="delta-col"><col class="apr-col"><col class="apr-col">
      <col class="spread-col"><col class="entry-col"><col class="entry-col"><col class="status-col"><col class="liq-col"><col class="slip-col"></colgroup>
    <thead><tr><th scope="col" class="coin-head"><div>${button('Монета', 'sort', 'plain', 'data-key="coin"')}
      ${button(icon('link'), 'builder', 'plain head-icon', 'aria-label="Конструктор связки"')}
      ${button(icon('search'), 'search', 'plain head-icon', 'aria-label="Поиск монеты"')}</div></th>
      <th scope="col">Связка</th><th scope="col">Фандинг</th>
      ${sortHeader('delta', 'Фанд. 24 ч', 'Разница фандинга за 24 часа')}
      ${sortHeader('apr', 'Экстраполированный APR', 'Годовая экстраполяция разницы ставок на номинал одной ноги. Комиссии и изменение ставок не учтены')}
      ${sortHeader('aprAfterFees', 'APR после комиссий', 'Условная оценка на суммарный номинал двух ног при одинаковых номиналах и указанном сроке удержания')}
      ${sortHeader('funding', 'Спред 8 ч', 'Спред фандинга за 8 часов')}
      ${sortHeader('entry', 'Спред входа')}
      <th scope="col">${button('Спред по глубине', 'executable', 'plain', 'title="Проверить до двух видимых связок, номинал 1000 на обе ноги"')}</th>
      <th scope="col" class="center">Статус</th>
      <th scope="col"><select class="head-select" id="liquidity" aria-label="Уровень ликвидности">${options({1:'Ликв. L1', 2:'Ликв. L2', 3:'Ликв. L3', 4:'Ликв. L4', 5:'Ликв. L5'}, liquidity)}</select></th>
      <th scope="col"><select class="head-select" id="nominal" aria-label="Номинал для проскальзывания">${options({100:'Проск. $100', 1000:'Проск. $1k', 2000:'Проск. $2k'}, nominal)}</select></th></tr></thead>
    <tbody>${body}</tbody>
    </table>`;
  updateRows(table, html, keys, hiddenIndices);
  document.querySelector('#result-count').innerHTML = `Связок · ${rows.length}${rows.length > shownRows
    ? ' · ' + button('Показать ещё', 'more', 'plain') : ''}${!isDemo && live.priceAnomalies
    ? ` · Отброшено направлений по расхождению цен в текущем расчёте: ${live.priceAnomalies}` : ''}`;
}
window.addEventListener('scroll', () => { if (document.querySelector('#market-table')) renderTable(); }, {passive: true});
window.addEventListener('resize', () => { if (document.querySelector('#market-table')) renderTable(); });
let searchOpen = Boolean(state.query);
function filterCount() {
  return Object.keys(numericFilters).filter(key => state[key]).length +
    Number(Boolean(state.categories.length)) + Number(Boolean(state.quotes.length)) + Number(!state.group);
}
function renderScreener(focusSearch = false) {
  app.className = 'shell screener';
  app.innerHTML = `<div class="page-title">
    <div class="title-group"><h1>Арбитраж</h1>${isDemo ? button('✦ VIP', 'vip', 'vip') : ''}</div>
    <div class="right-actions">
      ${menu(icon('link') + 'Ссылки', button('Площадки', 'links', 'menu-item') + button('Поделиться таблицей', 'share', 'menu-item'), 'align-right blue-menu')}
      ${button(icon('send') + 'Сигналы', 'signals', 'control outline')}
    </div></div>
    <div class="toolbar">
      <div class="toolbar-controls">
        <select id="mode" class="control" aria-label="Режим арбитража">${options(modes, state.mode)}</select>
        ${button('Биржи' + (state.exchanges.length < exchanges.length ? ` <span class="counter">${state.exchanges.length}</span>` : '') + icon('chevron'), 'exchanges', 'control')}
        ${button(icon('filters') + 'Фильтры' + (filterCount() ? ` <span class="counter">${filterCount()}</span>` : ''), 'filters', 'control')}
        ${menu(icon('heart'), button(state.favorites ? 'Все связки' : 'Только избранное', 'favorites', 'menu-item') + button('Избранные монеты', 'manage-favorites', 'menu-item'), `icon-menu ${state.favorites ? 'selected' : ''}`)}
        ${menu(icon('grid'), button('Обычная таблица', 'regular', 'menu-item') + button('Компактная таблица', 'compact', 'menu-item') +
          button('Колонки', 'columns', 'menu-item') + button('Пресеты фильтров', 'presets', 'menu-item'), 'icon-menu')}
      </div>
      <div class="feed"><span class="demo-label" id="feed-state"></span>${button(icon('clock') + '<span id="countdown"></span>', 'refresh', 'plain feed-timer', 'aria-label="Обновить данные"')}</div>
    </div>
    ${searchOpen || state.query || state.expand ? `<div class="search-strip">
      ${icon('search')}<label for="coin-search" class="sr-only">Поиск монеты</label><input id="coin-search" placeholder="Поиск по тикеру" value="${esc(state.query || state.expand)}" autocomplete="off">
      ${state.expand ? '<span class="muted">Все связки монеты</span>' : ''}
      ${button(icon('close'), 'clear-search', 'icon-button', 'aria-label="Закрыть поиск"')}</div>` : ''}
    <div class="market-table" id="market-table" tabindex="0" aria-label="Таблица арбитражных связок"></div>
    <div class="table-meta"><span id="result-count"></span>${button('Что означают показатели?', 'help', 'plain')}</div>`;
  renderTable();
  updateFeedLabel();
  if (focusSearch) document.querySelector('#coin-search')?.focus();
}

const startedAt = Date.now();
function fundingClock(leg) {
  if (!isDemo && leg.fundingPending) return '<time>Обновляется</time>';
  const value = isDemo ? leg.hours : leg.next;
  return value ? `<time data-clock="${value}">${timeLeft(value)}</time>` : '<time>—</time>';
}
function timeLeft(hours) {
  const elapsed = Math.floor((Date.now() - startedAt) / 1000);
  const duration = hours * 3600;
  const seconds = isDemo ? ((duration - 2070 - elapsed) % duration + duration) % duration :
    Math.max(0, Math.ceil((hours - Date.now()) / 1000));
  return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
    .map(value => String(value).padStart(2, '0')).join(':');
}
setInterval(() => {
  countdown = countdown > 1 ? countdown - 1 : 15;
  updateFeedLabel();
  for (const clock of document.querySelectorAll('[data-clock]')) clock.textContent = timeLeft(Number(clock.dataset.clock));
}, 1000);

const chartTabs = {spread: 'Спред', ...(!isDemo ? {funding: 'Фандинг'} : {}),
  index: 'Индекс', price: 'Цена', oi: 'Открытый интерес', info: 'Инфо'};
const intervals = {...(isDemo ? {'20s': '20 с'} : {}), '1m': '1 мин', '5m': '5 мин', '15m': '15 мин', '1h': '1 ч', '4h': '4 ч'};
let activePair;

function fundingTotals(pair) {
  if (!isDemo) return [pair.long.totals[1], pair.short.totals[1]];
  const long = pair.coin === 'CLAUDIA' ? (pair.long.exchange === 'Gate' ? -12 : -.03) : .03;
  return [long, long + pair.delta];
}

function venueCard(pair, short) {
  const leg = short ? pair.short : pair.long;
  const index = Number(short);
  const alternatives = dataSource.markets[pair.coin].legs.filter(item => item !== leg &&
    item !== (short ? pair.long : pair.short) && (!short || item.type !== 'spot'));
  const choices = alternatives.map(item => {
    const candidate = dataSource.resolve(pair.coin, short ? pair.long : item, short ? item : pair.short);
    return candidate ? `<a href="${routeHref(candidate.id)}">${badge(item.exchange)}${item.exchange} · ${item.type === 'spot' ? 'спот' : 'фьючерс'} · ${item.quote}</a>` : '';
  }).join('');
  const book = (side, multiplier) => isDemo ? leg.noBook ? '—' : Number((leg.price * multiplier).toPrecision(6)) : price(leg[side]);
  const history = fundingTotals(pair)[index];
  return `<article class="venue">
    <div class="venue-head">${arrow(short)}
      <h2>${button(badge(leg.exchange) + leg.exchange, 'market', 'plain market-name', `data-pair="${pair.id}" data-leg="${index}"`)}</h2>
      <span class="leg-type">${leg.type === 'spot' ? 'Спот' : short ? 'Шорт' : 'Лонг'}</span>
      ${menu('<span class="sr-only">Заменить ' + (short ? 'короткую' : 'длинную') + ' ногу</span>', choices, 'align-right')}
    </div>
    <dl><dt>Аск</dt><dd>${book('ask', 1.0012)}</dd><dt>Бид</dt><dd>${book('bid', .9993)}</dd>
    ${leg.type === 'spot' ? '' : `<dt>Фандинг ${leg.hours ? leg.hours + ' ч' : ''}</dt><dd>${button(pct(leg.rate, 4), 'limits', 'plain', `data-pair="${pair.id}" data-leg="${index}"`)}</dd>
      <dt>Отклонение от индекса</dt><dd>${signed(isDemo ? leg.noBook ? 0 : short ? pair.entry * .474 : -pair.entry * .117 :
        leg.index > 0 && leg.last > 0 ? 100 * (leg.last - leg.index) / leg.index : null, 3)}</dd>
      <dt>Фандинг 24 ч</dt><dd>${button(signed(history), 'history', 'plain', `data-pair="${pair.id}" data-leg="${index}"`)}</dd>
      <dt>До начисления</dt><dd>${fundingClock(leg)}</dd>`}
    <dt>Оборот</dt><dd>${Number.isFinite(leg.volume) ? money(leg.volume) + ' млн ' + leg.quote : '—'}</dd>
    <dt>Статус</dt><dd>${status(leg, pair.id, index)}</dd>
    ${leg.limit ? `<dt>Лимит</dt><dd class="bad">$${leg.limit}</dd>` : ''}
    </dl></article>`;
}

function renderDetail(pair) {
  activePair = pair;
  const showInterest = isDemo || [pair.long, pair.short].some(leg => Number.isFinite(leg.oi) ||
    live.details.get(leg.id)?.samples?.some(row => Number.isFinite(row[7])));
  if (!showInterest && chartTab === 'oi') chartTab = 'spread';
  app.className = `shell detail${wide ? ' wide' : ''}`;
  const reverse = pair.long.type === 'swap' && dataSource.resolve(pair.coin, pair.short, pair.long);
  const flipped = reverse ? `<a href="${routeHref(reverse.id)}">Развернуть связку</a>` : '';
  const missing = isDemo && (pair.long.noBook || pair.short.noBook);
  const entry = missing ? '—' : signed(pair.entry, 3);
  const exit = missing ? '—' : signed(pair.exit, 3);
  app.innerHTML = `<a href="${routeHref()}" class="breadcrumb">${icon('arrow')} К скринеру</a>
    <div class="detail-title">
      <div class="title-group"><h1>${button(pair.coin, 'builder', 'plain', 'title="Открыть конструктор"')}</h1>
        ${menu('•••', button('Поделиться', 'share', 'menu-item') +
          button(favorites.has(pair.coin) ? 'Убрать из избранного' : 'В избранное', 'toggle-coin', 'menu-item') +
          button(wide ? 'Обычная ширина' : 'Широкий режим', 'wide', 'menu-item') + flipped + button('Обновить', 'refresh', 'menu-item'))}
      </div>
      <div class="detail-summary"><strong>Спред фандинга <b>${pct(pair.funding, 3)}</b></strong>
      <span>Вход <b>${entry}</b></span><span>Выход <b>${exit}</b></span></div>
    </div>
    ${isDemo ? '' : `<p class="muted pair-quality">${liveQuality[pair.quality]}${pair.timestampSuspect ? ' · биржевые метки расходятся, требуется осторожность' : ''} · ${pair.verified ? 'Идентичность подтверждена реестром' : 'Идентичность по тикеру предварительная'}</p>`}
    <div class="venue-grid">${venueCard(pair, true)}${venueCard(pair, false)}</div>
    ${isDemo ? '' : `<div class="detail-actions">${button('Проверить стаканы', 'depth', 'control', `data-pair="${pair.id}"`)}
      ${pair.verified ? button('Оценить издержки', 'estimate', 'control') : ''}
      ${button('Добавить сигнал', 'pair-signal', 'control')}<span id="depth-summary" class="muted"></span></div>
      <p class="muted" id="detail-status" role="status">${esc(detailError)}</p>`}
    <div class="chart-tabs" role="tablist" aria-label="Графики">
      ${Object.entries(chartTabs).filter(([key]) => key !== 'oi' || showInterest).map(([key, label]) => button((key === 'spread' ? icon('chart') : key === 'oi' ? icon('bars') : '') + label,
        'chart-tab', '', `id="tab-${key}" role="tab" aria-controls="chart-panel" aria-selected="${chartTab === key}" data-tab="${key}"`)).join('')}
    </div><div id="chart-panel" role="tabpanel" aria-labelledby="tab-${chartTab}"></div>`;
  renderChart();
  if (!isDemo) updateDepthSummary();
}

function renderChart() {
  const panel = document.querySelector('#chart-panel');
  panel.setAttribute('aria-labelledby', 'tab-' + chartTab);
  if (!isDemo) {
    renderLiveChart(panel);
    return;
  }
  if (chartTab === 'info') {
    panel.innerHTML = `<div class="chart-frame chart-info"><h2>О связке</h2><p>${activePair.coin} ·
      ${activePair.long.exchange} (лонг) / ${activePair.short.exchange} (шорт)</p>
      <p>Котировки, ставки, история и статусы на этой странице демонстрационные.</p></div>`;
    return;
  }
  const spread = chartTab === 'spread';
  const width = 640;
  const mainHeight = Math.max(160, Math.min(420, Number(chartOptions.height) || 244));
  const plotWidth = 573;
  const bottom = mainHeight + (spread ? 113 : 16);
  const totalHeight = bottom + 27;
  const top = 26;
  const mainRange = spread ? Math.max(12, Math.ceil(Math.abs(activePair.entry) * 2 / 10) * 10)
    : chartTab === 'oi' ? 4 : Math.max(activePair.long.price, activePair.short.price) * 1.35;
  const seed = Array.from(activePair.id).reduce((sum, char) => sum + char.charCodeAt(0), 0) + Object.keys(intervals).indexOf(interval) * 17;
  const points = Array.from({length: 207}, (_, index) => {
    const t = index + seed + chartOffset * 34;
    const noise = Math.sin(t * 15.31) * .8 + Math.sin(t * 4.9) * .26;
    const peak = Math.exp(-((index - 90) ** 2) / 85) * (.56 + .32 * Math.sin(t * .63));
    const wave = Math.sin(t * .026) * .075 + Math.cos(t * .14) * .026;
    const first = .21 + wave + peak + noise * .062;
    const second = .23 + wave * .7 + peak * .87 + noise * .058;
    return {
      x: index * plotWidth / 206,
      a: spread ? first * mainRange : mainRange * (.58 + wave * 1.3 + noise * .019 + peak * .12),
      b: spread ? -second * mainRange : mainRange * (.72 + wave * 1.2 + noise * .014 + peak * .08),
      funding: Math.min(.13, -.22 + wave * 2.4 - peak * .8 + Math.sin(t * .12) * .15),
    };
  });
  const y = n => mainHeight - (n + (spread ? mainRange : 0)) / (mainRange * (spread ? 2 : 1)) * (mainHeight - top);
  const lowerY = n => mainHeight + 48 - n * 50;
  const line = (key, toY) => points.map(p => `${p.x.toFixed(2)},${toY(p[key]).toFixed(2)}`).join(' ');
  const a = line('a', y);
  const b = line('b', y);
  const funding = line('funding', lowerY);
  const shortFundingY = lowerY(.025);
  const colorA = '#2abd81', colorB = '#ee6878';
  const times = ['02:00', '05:00', '08:00', '11:00', '14:00', '17:00', '20:00'];
  const labels = spread && chartOptions.labels ? [22, 48, 83, 94, 137, 166, 192].map((index, i) => {
    const p = points[index];
    const key = i % 2 ? 'b' : 'a';
    return `<text x="${p.x}" y="${y(p[key]) + (key === 'a' ? -8 : 12)}" fill="${key === 'a' ? colorA : colorB}" text-anchor="middle">${pct(p[key], 1)}</text>`;
  }).join('') : '';
  const vertical = times.map((label, index) => {
    const x = 25 + index * (plotWidth - 49) / 6;
    const hour = Number(label.slice(0, 2));
    const shifted = (hour + chartOffset * 3 + 2400) % 24;
    return `<path d="M${x} 0V${bottom}"/><text x="${x}" y="${bottom + 17}" text-anchor="middle" stroke="none" fill="var(--muted)">${String(shifted).padStart(2, '0')}:00</text>`;
  }).join('');
  const horizontal = [1, .5, 0, -.5, -1].map((step, index) => {
    const value = mainRange * (spread ? step : (step + 1) / 2);
    const axis = y(value);
    const label = spread || chartTab === 'oi' ? value.toFixed(1) : value.toPrecision(3);
    return `<path d="M0 ${axis}H${plotWidth}"/><text x="${plotWidth + 9}" y="${axis + (index === 4 && spread ? -3 : 4)}" stroke="none" fill="var(--muted)">${label}</text>`;
  }).join('');
  panel.innerHTML = `<div class="chart-frame" id="chart-frame">
    <div class="chart-legend"><span>${badge(activePair.short.exchange)} ${activePair.short.exchange}</span><span>${badge(activePair.long.exchange)} ${activePair.long.exchange}</span>
      <select class="control" id="interval" aria-label="Интервал графика">${options(intervals, interval)}</select></div>
    <div class="chart-tooltip" id="chart-tooltip"></div>
    <svg class="chart-svg" id="chart-svg" viewBox="0 0 ${width} ${totalHeight}" role="img" aria-label="${chartTabs[chartTab]}, демонстрационные данные">
      <g fill="none" stroke="var(--grid)" stroke-width="1" font-size="11">${vertical}${horizontal}
      <path d="M0 ${bottom}H${plotWidth}V0"/>
      ${spread ? `<path d="M0 ${mainHeight + 46}H${plotWidth}M0 ${mainHeight + 80}H${plotWidth}"/>
      <text x="582" y="${mainHeight + 45}" fill="var(--muted)" stroke="none">0,0%</text><text x="582" y="${mainHeight + 82}" fill="var(--muted)" stroke="none">−0,6%</text>` : ''}</g>
      <g fill="none" stroke-width="1.4"><polyline points="${a}" stroke="${colorA}"/><polyline points="${b}" stroke="${colorB}"/></g>
      <g font-size="10">${labels}</g>
      ${spread ? `${chartOptions.areas ? `<polygon points="0,${bottom} ${funding} ${plotWidth},${bottom}" fill="${colorA}" opacity=".12"/>` : ''}
      <polyline points="${funding}" fill="none" stroke="${colorA}" stroke-width="1.45"/>
      <path d="M0 ${shortFundingY}H${plotWidth}" stroke="${colorB}" stroke-width="1.2"/>
      <g font-size="10" font-weight="600"><rect x="505" y="${shortFundingY - 14}" width="68" height="14" fill="#a83348"/><text x="511" y="${shortFundingY - 4}" fill="white">${activePair.short.exchange}</text>
      <rect x="505" y="${mainHeight + 58}" width="68" height="14" fill="#19704f"/><text x="511" y="${mainHeight + 68}" fill="white">${activePair.long.exchange}</text></g>` : ''}
      <line id="crosshair" y1="25" y2="${bottom}" stroke="var(--muted)" stroke-dasharray="3 4" visibility="hidden"/>
    </svg>
    <div class="chart-tools">
      ${button('←', 'chart-left', '', 'aria-label="График влево"')}
      ${button('→', 'chart-right', '', 'aria-label="График вправо"')}
      ${button('⇥', 'chart-end', '', 'aria-label="К последним данным"')}
      ${button('↻', 'chart-reset', '', 'aria-label="Сбросить график"')}
      ${button(icon('settings'), 'chart-settings', '', 'aria-label="Настройки графика"')}
      ${button('⛶', 'fullscreen', '', 'aria-label="На весь экран"')}
    </div>
  </div>`;
  if (!chartOptions.tooltip) return;
  const svg = document.querySelector('#chart-svg');
  const tooltip = document.querySelector('#chart-tooltip');
  const lineElement = document.querySelector('#crosshair');
  svg.addEventListener('pointermove', event => {
    const rect = svg.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width * width;
    const index = Math.max(0, Math.min(points.length - 1, Math.round(x / plotWidth * (points.length - 1))));
    const p = points[index];
    lineElement.setAttribute('x1', p.x);
    lineElement.setAttribute('x2', p.x);
    lineElement.setAttribute('visibility', 'visible');
    tooltip.style.display = 'block';
    tooltip.style.left = Math.max(5, Math.min(rect.width - tooltip.offsetWidth - 5, event.clientX - rect.left + 12)) + 'px';
    tooltip.textContent = spread ? `Вход ${signed(p.a)} · Выход ${signed(p.b)}`
      : `${activePair.long.exchange} ${p.a.toPrecision(5)} · ${activePair.short.exchange} ${p.b.toPrecision(5)}`;
  });
  svg.addEventListener('pointerleave', () => {
    tooltip.style.display = 'none';
    lineElement.setAttribute('visibility', 'hidden');
  });
}

function renderRoute() {
  if (!isDemo && Date.now() >= live.nextDataChange) refreshQuotePairs();
  route = currentRoute();
  state = listSettings(route.params);
  activePair = undefined;
  renderHeader();
  if (route.section === 'arbitrage' && !route.id) {
    document.title = 'Арбитраж · Монитор';
    searchOpen = Boolean(state.query || state.expand);
    renderScreener();
    return;
  }
  const pair = route.section === 'arbitrage' && dataSource.fromId(route.id);
  if (pair) {
    document.title = `${pair.coin} · ${pair.short.exchange} / ${pair.long.exchange} · Арбитраж`;
    renderDetail(pair);
    return;
  }
  const title = Object.hasOwn(sections, route.section) ? sections[route.section] :
    Object.hasOwn(tables, route.section) ? tables[route.section] : 'Раздел не найден';
  document.title = title + ' · Арбитраж Монитор';
  app.className = 'shell placeholder';
  app.innerHTML = `<h1>${route.id ? 'Связка не найдена' : title}</h1><p>${route.id ?
    isDemo ? 'В демонстрационном наборе нет связки с таким адресом.' :
      live.error ? 'Сервер данных недоступен. Запустите Start-UI.cmd и обновите страницу.' :
        !live.generatedAt ? 'Загружаем каталог бирж.' : 'В текущем каталоге нет совместимых рынков по этому адресу.' :
    'Этот раздел пока представлен заглушкой.'}</p>
    <a class="control back" href="#/arbitrage">${icon('arrow')} К арбитражу</a>`;
}

const check = (name, value, label, checked) => `<label class="check"><input type="checkbox" name="${name}" value="${value}"${checked ? ' checked' : ''}>${label}</label>`;
const checkboxGroup = (legend, content) => `<fieldset><legend>${legend}</legend><div class="check-grid">${content}</div></fieldset>`;

function showFilters(filters = state) {
  const fields = Object.entries(numericFilters).map(([key, label]) => {
    const disabled = isDemo && ['delta3', 'delta7'].includes(key);
    const input = `<input type="number" step="any" name="${key}" value="${esc(filters[key] ?? '')}"
      placeholder="${disabled ? 'Нет источника' : 'Любое значение'}"${disabled ? ' disabled' : ''}>`;
    return `<label class="field"><span>${label}</span>${key === 'spread' ? `<span class="field-row">
      <select name="operator" aria-label="Условие спреда">${options({gte: '≥', lte: '≤'}, filters.operator ?? 'gte')}</select>${input}</span>` : input}</label>`;
  }).join('');
  showDialog('Фильтры', `<div class="form-grid">${fields}</div>
    <p class="muted">APR после комиссий использует указанные ставки для каждой связки, четыре taker-комиссии и одинаковый номинал ног. База — суммарный номинал обеих ног. Ставки фандинга считаются постоянными на выбранный срок.</p>
    <div class="form-grid">${[['longFee', 'Taker лонга, %', 0], ['shortFee', 'Taker шорта, %', 0], ['holdDays', 'Срок удержания, дней', 0.001]].map(([key, label, min]) =>
      `<label class="field">${label}<input type="number" name="${key}" value="${esc(filters[key] ?? '')}" min="${min}" ${key === 'holdDays' ? '' : 'max="1"'} step="any"></label>`).join('')}</div>
    <p id="filters-error" class="unavailable" role="alert"></p>
    ${isDemo ? '' : '<p class="muted">Капитализация и FDV из CoinGecko доступны для связок с двумя сверенными контрактами. При заданном пороге связки без значения исключаются.</p>'}
    ${checkboxGroup('Валюта котировки', quotes.map(quote => check('quotes', quote, quote, filters.quotes.includes(quote))).join(''))}
    ${checkboxGroup('Категории', Object.entries(categories).map(([key, label]) => check('categories', key, label, filters.categories.includes(key))).join(''))}
    ${check('group', '1', 'Группировать по монете', filters.group)}`,
  button('Сбросить', 'clear-filter-draft', 'control') + cancel() + submit('Применить'), 'filters');
}
function showExchanges() {
  showDialog('Биржи', `<input id="exchange-search" placeholder="Поиск биржи" aria-label="Поиск биржи" autocomplete="off">
    <div class="form-actions">${button('Выбрать все', 'all-exchanges', 'control')}${button('Очистить', 'no-exchanges', 'control')}</div>
    <div class="check-grid" id="exchange-list">${exchanges.map(exchange =>
      check('exchanges', exchange, badge(exchange) + exchange, state.exchanges.includes(exchange))).join('')}</div>`,
  cancel() + submit('Применить'), 'exchanges');
}
function showFavorites() {
  showDialog('Избранные монеты', `<p>Отметьте монеты, которые хотите отслеживать на этом устройстве.</p>
    <div class="check-grid">${[...new Set([...favorites, ...Object.keys(dataSource.markets)])].sort().map(coin => check('coin', coin, coin, favorites.has(coin))).join('')}</div>`,
  cancel() + submit('Сохранить'), 'favorites');
}
const builderKey = (leg, index) => leg.id ?? String(index);

function builderOptions(legs, pair) {
  const choices = legs.map((leg, index) => [
    builderKey(leg, index), leg.exchange + ' · ' + (leg.type === 'spot' ? 'спот' : 'фьючерс') + ' · ' + leg.quote,
  ]);
  const shortChoices = choices.filter((_, index) => legs[index].type !== 'spot');
  const key = leg => builderKey(leg, legs.indexOf(leg));
  return `<div class="field"><label for="builder-long">Лонг</label>
      <select id="builder-long" name="long">${options(Object.fromEntries(choices), key(pair.long))}</select></div>
    <div class="field"><label for="builder-short">Шорт</label>
      <select id="builder-short" name="short">${options(Object.fromEntries(shortChoices), key(pair.short))}</select></div>`;
}
function showBuilder() {
  const coins = Object.keys(dataSource.markets).sort();
  const coin = activePair?.coin ?? coins.find(key => key === state.query.toUpperCase()) ??
    (coins.includes(isDemo ? 'CLAUDIA' : 'BTC') ? isDemo ? 'CLAUDIA' : 'BTC' : coins[0]);
  if (!coin) return toast('Каталог рынков пока недоступен');
  const legs = dataSource.markets[coin].legs;
  const initial = activePair ?? dataSource.pairs.find(pair => pair.coin === coin);
  showDialog('Конструктор связки', `<p>Выберите монету и две ноги в одной валюте котировки.</p>
    <div class="field"><label for="builder-coin">Монета</label>
      <select name="coin" id="builder-coin">${options(Object.fromEntries(coins.map(key => [key, key])), coin)}</select></div>
    <div class="form-grid" id="builder-legs">${builderOptions(legs, initial)}</div><p id="builder-error" class="unavailable" role="alert"></p>`,
  cancel() + submit('Открыть связку'), 'builder');
}
function showHistory(pair, leg = '', days = 1) {
  if (!isDemo) {
    showLiveHistory(pair, leg, days);
    return;
  }
  const comparison = leg === '';
  const selection = comparison ? null : Number(leg) === 1 ? pair.short : pair.long;
  const [longTotal, shortTotal] = fundingTotals(pair);
  const rows = Array.from({length: 6}, (_, index) => {
    const [l, s] = [longTotal / 6, shortTotal / 6];
    const rate = Number(leg) === 1 ? s : l;
    return `<tr><td>${String(20 - index * 4).padStart(2, '0')}:00</td>
      ${comparison ? `<td>${signed(l, 4)}</td><td>${signed(s, 4)}</td><td>${signed((s - l) * (index + 1), 4)}</td>`
      : `<td>${signed(rate, 4)}</td><td>${signed(rate * (index + 1), 4)}</td>`}</tr>`;
  }).join('');
  showDialog('История фандинга · ' + pair.coin, `<div class="chart-tabs">
    ${[['', 'Сравнение'], ['0', pair.long.exchange], ['1', pair.short.exchange]].map(([key, label]) =>
      button(label, 'history', '', `data-pair="${pair.id}" data-leg="${key}" aria-pressed="${key === leg}" aria-selected="${key === leg}"`)).join('')}</div>
    <p>Условные начисления за последние 24 часа.</p>
    <table class="mini-table"><thead><tr><th>Время</th>${comparison ?
      `<th>${pair.long.exchange}</th><th>${pair.short.exchange}</th><th>Накопленная разница</th>` :
      `<th>Ставка · ${selection.exchange}</th><th>Накопление</th>`}</tr></thead><tbody>${rows}</tbody></table>`,
  button('Закрыть', 'close', 'control'));
}
function showStatus(pair, leg) {
  if (!isDemo) {
    const state = value => value === true ? 'Доступен' : value === false ? 'Приостановлен' : '—';
    showDialog(`${pair.coin} · ${leg.exchange}`, leg.networks.length ?
      `<table class="mini-table"><thead><tr><th>Сеть</th><th>Ввод</th><th>Вывод</th></tr></thead><tbody>${leg.networks.map(network =>
        `<tr><td>${esc(network.name)}</td><td>${state(network.deposit)}</td><td>${state(network.withdraw)}</td></tr>`).join('')}</tbody></table>` :
      '<p>Публичный источник не вернул сетевые статусы для этого актива.</p>');
    return;
  }
  showDialog(`${pair.coin} · ${leg.exchange}`, leg.status.length ?
    `<p>Демонстрационный статус актива</p><dl class="status-description">
    <dt>Сеть</dt><dd>${pair.coin === 'CLAUDIA' ? 'Solana (SOL)' : 'Условная сеть'}</dd>
    <dt>Ввод</dt><dd class="${leg.status[0] ? 'available' : 'unavailable'}">${leg.status[0] ? 'Доступен' : 'Приостановлен'}</dd>
    <dt>Вывод</dt><dd class="${leg.status[1] ? 'available' : 'unavailable'}">${leg.status[1] ? 'Доступен' : 'Приостановлен'}</dd></dl>` :
    '<p>Данные о вводе и выводе отсутствуют.</p>');
}
function showChartSettings() {
  showDialog('Настройки графика', `<p>Настройки сохраняются на этом устройстве.</p>
    <label class="field">Высота графика, пиксели<input type="number" name="height" min="160" max="420" required value="${Number(chartOptions.height)}"></label>
    ${check('labels', '1', 'Подписи спреда', chartOptions.labels)}
    ${check('areas', '1', 'Заливка области фандинга', chartOptions.areas)}
    ${check('tooltip', '1', 'Подсказка при наведении', chartOptions.tooltip)}`,
  cancel() + submit('Сохранить'), 'chart');
}
async function share() {
  try {
    await navigator.clipboard.writeText(location.href);
    toast('Ссылка скопирована');
  } catch {
    showDialog('Ссылка на экран', `<label class="field">Скопируйте адрес<input readonly value="${esc(location.href)}"></label>`);
    dialog.querySelector('input').select();
  }
}

const actions = {
  close: () => dialog.close(),
  theme() {
    theme = theme === 'dark' ? 'light' : 'dark';
    savePreference('theme', theme);
    renderHeader();
    document.querySelector('[data-action="theme"]').focus();
  },
  search() { searchOpen = true; renderScreener(true); },
  'clear-search'() { searchOpen = false; applyList({query: '', expand: ''}); },
  expand(target) {
    searchOpen = true;
    applyList({expand: target.dataset.coin, query: ''});
  },
  sort(target) {
    const sort = target.dataset.key;
    applyList({sort, direction: sort === state.sort && state.direction === 'desc' ? 'asc' : 'desc'});
    document.querySelector(`[data-key="${sort}"]`).focus({preventScroll: true});
  },
  reset() { searchOpen = false; state = listSettings(new URLSearchParams()); applyList({}); },
  filters: () => showFilters(),
  'clear-filter-draft': () => showFilters({quotes: [], categories: [], group: true}),
  exchanges: showExchanges,
  'all-exchanges': () => dialog.querySelectorAll('[name="exchanges"]').forEach(input => input.checked = true),
  'no-exchanges': () => dialog.querySelectorAll('[name="exchanges"]').forEach(input => input.checked = false),
  favorites: () => applyList({favorites: !state.favorites}),
  'manage-favorites': showFavorites,
  'toggle-coin'() {
    const coin = activePair.coin;
    if (favorites.has(coin)) favorites.delete(coin);
    else favorites.add(coin);
    savePreference('favorites', [...favorites]);
    toast(favorites.has(coin) ? 'Монета добавлена в избранное' : 'Монета удалена из избранного');
  },
  regular: () => setLayout('regular'),
  compact: () => setLayout('compact'),
  more() { shownRows += 80; renderTable(); },
  columns: () => showDialog('Колонки', checkboxGroup('Показать', Object.entries(columnLabels).map(([key, label]) =>
    check('column', key, label, !hiddenColumns.has(key))).join('')), cancel() + submit('Применить'), 'columns'),
  presets: showPresets,
  'apply-preset'(target) {
    const preset = presets[Number(target.dataset.index)];
    dialog.close();
    state = listSettings(new URLSearchParams(preset.query));
    applyList({});
  },
  'delete-preset'(target) {
    presets.splice(Number(target.dataset.index), 1);
    savePreference('presets', presets);
    showPresets();
  },
  builder: showBuilder,
  share,
  refresh() {
    if (!isDemo) {
      refreshMarkets();
      if (activePair) loadPairDetails(activePair);
      return;
    }
    countdown = 15;
    const timer = document.querySelector('#countdown');
    if (timer) timer.textContent = '15 с';
    toast('Демонстрационный снимок обновлён');
  },
  history: (target, pair) => showHistory(pair, target.dataset.leg, Number(target.dataset.days ?? 1)),
  status: (target, pair, leg) => showStatus(pair, leg),
  market: (target, pair, leg) => showDialog(`${leg.exchange} · ${pair.coin}/${leg.quote}`,
    `<p>${leg.type === 'spot' ? 'Спотовый' : 'Бессрочный'} рынок · ${isDemo ? 'демоданные' : esc(leg.symbol)}.</p>
    <p>Последняя цена — ${isDemo ? leg.price : price(leg.last)} ${leg.quote}.</p>
    ${isDemo ? '' : `<p>Получено ${leg.receivedAt ? new Date(leg.receivedAt).toLocaleString('ru') : '—'}.
      Биржевое время ${leg.sourceAt ? new Date(leg.sourceAt).toLocaleString('ru') : '—'}.</p>`}`),
  limits: (target, pair, leg) => showDialog('Пределы фандинга · ' + leg.exchange,
    `<p>Интервал начисления — ${leg.hours ? leg.hours + ' ч' : '—'}. Текущая ставка — ${pct(leg.rate, 4)}.</p>
      <p>Нижний предел ${pct(leg.floor, 4)} · верхний предел ${pct(leg.cap, 4)}.</p>`),
  wide() { wide = !wide; savePreference('wide', wide); app.classList.toggle('wide', wide); renderDetail(activePair); },
  'chart-tab'(target) {
    chartTab = target.dataset.tab;
    for (const tab of app.querySelectorAll('[data-tab]')) tab.setAttribute('aria-selected', String(tab === target));
    renderChart();
  },
  'chart-left'() { chartOffset = Math.max(-48, chartOffset - 1); renderChart(); },
  'chart-right'() { chartOffset = Math.min(0, chartOffset + 1); renderChart(); },
  'chart-end'() { chartOffset = 0; renderChart(); },
  'chart-reset'() { chartOffset = 0; interval = '1m'; renderChart(); },
  'chart-settings': showChartSettings,
  async fullscreen() {
    if (document.fullscreenElement) await document.exitFullscreen();
    else {
      try { await document.querySelector('#chart-frame').requestFullscreen(); }
      catch { toast('Полноэкранный режим недоступен'); }
    }
  },
  vip: () => showDialog('VIP', '<p>Демонстрационная витрина. Сравнение связок, избранное и графики доступны в прототипе.</p>'),
  profile: () => showDialog('Локальный профиль', '<p>Тема, избранное, пресеты, сигналы и настройки графика сохраняются в этом браузере.</p>'),
  signals: showSignals,
  'pair-signal': () => showSignals(activePair),
  'remove-signal'(target) {
    signalRules.splice(Number(target.dataset.index), 1);
    savePreference('signal-rules', signalRules);
    showSignals(activePair);
  },
  depth: (_target, pair) => requestDepth(pair),
  executable: () => loadExecutableSpreads(),
  estimate: () => showEstimate(activePair),
  links: () => showDialog('Площадки и источники', isDemo ?
    `<div class="check-grid">${exchanges.map(exchange => `<span>${badge(exchange)} ${exchange}</span>`).join('')}</div>` :
    `<ul>${Object.entries(live.venues).map(([name, status]) => {
      const stale = status.batchReceivedAt && Date.now() - status.batchReceivedAt > 60000;
      return `<li>${esc(name)} · ${stale ? '<span class="unavailable">котировки устарели</span>' :
        status.state === 'ok' ? 'подключено' : esc(status.error ?? 'ожидание')}</li>`;
    }).join('')}</ul>`),
  help: () => showDialog('Справка по скринеру', `<p><b>Связка</b> объединяет длинную (↑) и короткую (↓) ноги на двух рынках.</p>
    <p><b>Фанд. 24 ч</b> показывает разницу начислений. Нажмите значение, чтобы открыть историю.</p>
    <p><b>Спред 8 ч</b> сравнивает текущие ставки, приведённые к восьми часам. <b>Экстраполированный APR</b> линейно переносит текущую разницу ставок на год без комиссий и изменений фандинга.</p>
    <p><b>Спред входа</b> = (Bid шорта − Ask лонга) / среднее этих цен.
    Спред выхода использует Bid лонга и Ask шорта с той же базой.
    <b>Ликвидность Ln</b> суммирует номиналы первых n уровней. <b>Проскальзывание</b> сравнивает VWAP с первым уровнем.
    Прочерк означает отсутствие пригодного значения. Расчёты индикативны.</p>`),
};

function setLayout(value) {
  layout = value;
  savePreference('layout', layout);
  renderTable();
}

document.addEventListener('click', event => {
  const target = event.target.closest('[data-action]');
  for (const open of document.querySelectorAll('details[open]')) {
    if (!open.contains(event.target) || target || event.target.closest('a')) open.open = false;
  }
  if (!target) return;
  const pair = target.dataset.pair ? dataSource.fromId(target.dataset.pair) : undefined;
  if (target.dataset.pair && !pair) return toast('Связка больше недоступна в текущем снимке');
  const leg = pair && (target.dataset.leg === '1' ? pair.short : pair.long);
  actions[target.dataset.action]?.(target, pair, leg);
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && !dialog.open) {
    for (const open of document.querySelectorAll('details[open]')) {
      open.open = false;
      open.querySelector('summary').focus();
    }
  }
});
document.addEventListener('input', event => {
  const input = event.target;
  if (input.id === 'coin-search') applyList({query: input.value, expand: ''}, true);
  if (input.id === 'exchange-search') {
    for (const checkbox of dialog.querySelectorAll('[name="exchanges"]')) {
      checkbox.parentElement.hidden = !checkbox.value.toLowerCase().includes(input.value.trim().toLowerCase());
    }
  }
});
document.addEventListener('change', event => {
  const input = event.target;
  if (input.id === 'mode') {
    const mode = input.value;
    applyList({mode, sort: ['funding', 'spot'].includes(mode) ? 'funding' : 'entry', direction: mode === 'negative' ? 'asc' : 'desc', expand: ''});
    document.querySelector('#mode').focus();
  }
  if (input.id === 'liquidity') { liquidity = Number(input.value); renderTable(); }
  if (input.id === 'nominal') { nominal = Number(input.value); renderTable(); }
  if (input.id === 'interval') { interval = input.value; renderChart(); document.querySelector('#interval').focus(); }
  if (input.id === 'builder-coin') {
    const legs = dataSource.markets[input.value]?.legs;
    const pair = dataSource.pairs.find(item => item.coin === input.value);
    document.querySelector('#builder-legs').innerHTML = pair ? builderOptions(legs, pair) : '';
    document.querySelector('#builder-error').textContent = pair ? '' : 'Монета недоступна в текущем каталоге.';
  }
});
dialog.addEventListener('submit', event => {
  event.preventDefault();
  const form = event.target;
  const data = new FormData(form);
  switch (form.dataset.form) {
    case 'filters': {
      const patch = Object.fromEntries(Object.keys(numericFilters).map(key => [key, data.get(key) || '']));
      for (const key of ['longFee', 'shortFee', 'holdDays']) patch[key] = data.get(key) || '';
      if (patch.aprAfterFees && (!patch.longFee || !patch.shortFee || !patch.holdDays)) {
        form.querySelector('#filters-error').textContent = 'Для порога APR после комиссий укажите обе комиссии и срок удержания.';
        return;
      }
      applyList({...patch, quotes: data.getAll('quotes'), categories: data.getAll('categories'),
        group: data.has('group'),
        operator: data.get('operator'), expand: ''});
      break;
    }
    case 'exchanges':
      applyList({exchanges: data.getAll('exchanges'), expand: ''});
      break;
    case 'favorites':
      favorites = new Set(data.getAll('coin'));
      savePreference('favorites', [...favorites]);
      renderTable();
      break;
    case 'builder': {
      const coin = data.get('coin');
      const legs = dataSource.markets[coin]?.legs ?? [];
      const [long, short] = ['long', 'short'].map(side => legs.find((leg, index) => builderKey(leg, index) === data.get(side)));
      const pair = long && short && long !== short && dataSource.resolve(coin, long, short);
      if (!pair) {
        document.querySelector('#builder-error').textContent = 'Выберите два совместимых рынка в одной валюте.';
        return;
      }
      location.hash = routeHref(pair.id);
      break;
    }
    case 'chart':
      chartOptions = {height: Number(data.get('height')), labels: data.has('labels'), areas: data.has('areas'), tooltip: data.has('tooltip')};
      savePreference('chart', chartOptions);
      renderChart();
      break;
    case 'columns':
      hiddenColumns = new Set(Object.keys(columnLabels).filter(key => !data.getAll('column').includes(key)));
      savePreference('hidden-columns', [...hiddenColumns]);
      renderTable();
      break;
    case 'presets': {
      const name = data.get('name').trim();
      if (!name) return;
      presets = [...presets.filter(preset => preset.name !== name), {name, query: listQuery()}].slice(-30);
      savePreference('presets', presets);
      break;
    }
    case 'signals': {
      const pair = activePair ?? dataSource.fromId(data.get('pair'));
      const threshold = Number(data.get('threshold'));
      if (!pair || !Number.isFinite(threshold)) return;
      const key = pair.id + ':' + data.get('metric');
      signalStates.delete(key);
      signalRules = [...signalRules.filter(rule => rule.key !== key),
        {key, pair: pair.id, label: pair.coin + ' · ' + pair.long.exchange + ' → ' + pair.short.exchange,
          threshold, metric: data.get('metric')}].slice(-30);
      savePreference('signal-rules', signalRules);
      break;
    }
    case 'estimate':
      calculateEstimate(activePair, data);
      return;
  }
  dialog.close();
});

window.addEventListener('hashchange', () => {
  if (dialog.open) dialog.close();
  chartTab = 'spread';
  chartOffset = 0;
  detailError = '';
  renderRoute();
  if (!isDemo && activePair) loadPairDetails(activePair);
  window.scrollTo(0, 0);
});
renderRoute();
if (!isDemo) {
  refreshMarkets();
  startEarlyQuotes();
  setInterval(refreshMarketsFallback, 15000);
  setInterval(expireLiveData, 1000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      expireLiveData();
      refreshMarkets();
    }
  });
}
