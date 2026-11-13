export function createDialogs(context) {
  const check = (name, value, label, checked) =>
    `<label class="check"><input type="checkbox" name="${name}" value="${value}"${checked ? ' checked' : ''}>${label}</label>`;
  const checkboxGroup = (legend, content) => `<fieldset><legend>${legend}</legend><div class="check-grid">${content}</div></fieldset>`;
  const builderKey = (leg, index) => leg.id ?? String(index);

  function showFilters(filters = context.state) {
    const fields = Object.entries(context.numericFilters).map(([key, label]) => {
      const disabled = context.isDemo && ['delta3', 'delta7'].includes(key);
      const input = `<input type="number" step="any" name="${key}" value="${context.esc(filters[key] ?? '')}"
        placeholder="${disabled ? 'Нет источника' : 'Любое значение'}"${disabled ? ' disabled' : ''}>`;
      return `<label class="field"><span>${label}</span>${key === 'spread' ? `<span class="field-row">
        <select name="operator" aria-label="Условие спреда">${context.options({gte: '≥', lte: '≤'}, filters.operator ?? 'gte')}</select>${input}</span>` : input}</label>`;
    }).join('');
    context.showDialog('Фильтры', `<div class="form-grid">${fields}</div>
      <p class="muted">APR после комиссий использует указанные ставки для каждой связки, четыре taker-комиссии и одинаковый номинал ног. База — суммарный номинал обеих ног. Ставки фандинга считаются постоянными на выбранный срок.</p>
      <div class="form-grid">${[['longFee', 'Taker лонга, %', 0], ['shortFee', 'Taker шорта, %', 0], ['holdDays', 'Срок удержания, дней', 0.001]].map(([key, label, min]) =>
        `<label class="field">${label}<input type="number" name="${key}" value="${context.esc(filters[key] ?? '')}" min="${min}" ${key === 'holdDays' ? '' : 'max="1"'} step="any"></label>`).join('')}</div>
      <p id="filters-error" class="unavailable" role="alert"></p>
      ${context.isDemo ? '' : '<p class="muted">Капитализация и FDV из CoinGecko доступны для связок с двумя сверенными контрактами. При заданном пороге связки без значения исключаются.</p>'}
      ${checkboxGroup('Валюта котировки', context.quotes.map(quote => check('quotes', quote, quote, filters.quotes.includes(quote))).join(''))}
      ${checkboxGroup('Категории', Object.entries(context.categories).map(([key, label]) => check('categories', key, label, filters.categories.includes(key))).join(''))}
      ${check('group', '1', 'Группировать по монете', filters.group)}`,
    context.button('Сбросить', 'clear-filter-draft', 'control') + context.cancel() + context.submit('Применить'), 'filters');
  }

  function showExchanges() {
    context.showDialog('Биржи', `<input id="exchange-search" placeholder="Поиск биржи" aria-label="Поиск биржи" autocomplete="off">
      <div class="form-actions">${context.button('Выбрать все', 'all-exchanges', 'control')}${context.button('Очистить', 'no-exchanges', 'control')}</div>
      <div class="check-grid" id="exchange-list">${context.exchanges.map(exchange =>
        check('exchanges', exchange, context.badge(exchange) + exchange, context.state.exchanges.includes(exchange))).join('')}</div>`,
    context.cancel() + context.submit('Применить'), 'exchanges');
  }

  function showFavorites() {
    context.showDialog('Избранные монеты', `<p>Отметьте монеты, которые хотите отслеживать на этом устройстве.</p>
      <div class="check-grid">${[...new Set([...context.favorites, ...Object.keys(context.dataSource.markets)])].sort()
        .map(coin => check('coin', coin, coin, context.favorites.has(coin))).join('')}</div>`,
    context.cancel() + context.submit('Сохранить'), 'favorites');
  }

  function builderOptions(legs, pair) {
    const choices = legs.map((leg, index) => [
      builderKey(leg, index), leg.exchange + ' · ' + (leg.type === 'spot' ? 'спот' : 'фьючерс') + ' · ' + leg.quote,
    ]);
    const shortChoices = choices.filter((_, index) => legs[index].type !== 'spot');
    const key = leg => builderKey(leg, legs.indexOf(leg));
    return `<div class="field"><label for="builder-long">Лонг</label>
        <select id="builder-long" name="long">${context.options(Object.fromEntries(choices), key(pair.long))}</select></div>
      <div class="field"><label for="builder-short">Шорт</label>
        <select id="builder-short" name="short">${context.options(Object.fromEntries(shortChoices), key(pair.short))}</select></div>`;
  }

  function showBuilder() {
    const coins = Object.keys(context.dataSource.markets).sort();
    const coin = context.activePair?.coin ?? coins.find(key => key === context.state.query.toUpperCase()) ??
      (coins.includes(context.isDemo ? 'CLAUDIA' : 'BTC') ? context.isDemo ? 'CLAUDIA' : 'BTC' : coins[0]);
    if (!coin) return context.toast('Каталог рынков пока недоступен');
    const legs = context.dataSource.markets[coin].legs;
    const initial = context.activePair ?? context.dataSource.pairs.find(pair => pair.coin === coin);
    context.showDialog('Конструктор связки', `<p>Выберите монету и две ноги в одной валюте котировки.</p>
      <div class="field"><label for="builder-coin">Монета</label>
        <select name="coin" id="builder-coin">${context.options(Object.fromEntries(coins.map(key => [key, key])), coin)}</select></div>
      <div class="form-grid" id="builder-legs">${builderOptions(legs, initial)}</div><p id="builder-error" class="unavailable" role="alert"></p>`,
    context.cancel() + context.submit('Открыть связку'), 'builder');
  }

  function showHistory(pair, leg = '', days = 1) {
    if (!context.isDemo) return context.showLiveHistory(pair, leg, days);
    const comparison = leg === '';
    const selection = comparison ? null : Number(leg) === 1 ? pair.short : pair.long;
    const [longTotal, shortTotal] = context.fundingTotals(pair);
    const rows = Array.from({length: 6}, (_, index) => {
      const [l, s] = [longTotal / 6, shortTotal / 6];
      const rate = Number(leg) === 1 ? s : l;
      return `<tr><td>${String(20 - index * 4).padStart(2, '0')}:00</td>
        ${comparison ? `<td>${context.signed(l, 4)}</td><td>${context.signed(s, 4)}</td><td>${context.signed((s - l) * (index + 1), 4)}</td>`
        : `<td>${context.signed(rate, 4)}</td><td>${context.signed(rate * (index + 1), 4)}</td>`}</tr>`;
    }).join('');
    context.showDialog('История фандинга · ' + pair.coin, `<div class="chart-tabs">
      ${[['', 'Сравнение'], ['0', pair.long.exchange], ['1', pair.short.exchange]].map(([key, label]) =>
        context.button(label, 'history', '', `data-pair="${pair.id}" data-leg="${key}" aria-pressed="${key === leg}" aria-selected="${key === leg}"`)).join('')}</div>
      <p>Условные начисления за последние 24 часа.</p>
      <table class="mini-table"><thead><tr><th>Время</th>${comparison ?
        `<th>${pair.long.exchange}</th><th>${pair.short.exchange}</th><th>Накопленная разница</th>` :
        `<th>Ставка · ${selection.exchange}</th><th>Накопление</th>`}</tr></thead><tbody>${rows}</tbody></table>`,
    context.button('Закрыть', 'close', 'control'));
  }

  function showStatus(pair, leg) {
    if (!context.isDemo) {
      const status = value => value === true ? 'Доступен' : value === false ? 'Приостановлен' : '—';
      context.showDialog(`${pair.coin} · ${leg.exchange}`, leg.networks.length ?
        `<table class="mini-table"><thead><tr><th>Сеть</th><th>Ввод</th><th>Вывод</th></tr></thead><tbody>${leg.networks.map(network =>
          `<tr><td>${context.esc(network.name)}</td><td>${status(network.deposit)}</td><td>${status(network.withdraw)}</td></tr>`).join('')}</tbody></table>` :
        '<p>Публичный источник не вернул сетевые статусы для этого актива.</p>');
      return;
    }
    context.showDialog(`${pair.coin} · ${leg.exchange}`, leg.status.length ?
      `<p>Демонстрационный статус актива</p><dl class="status-description">
      <dt>Сеть</dt><dd>${pair.coin === 'CLAUDIA' ? 'Solana (SOL)' : 'Условная сеть'}</dd>
      <dt>Ввод</dt><dd class="${leg.status[0] ? 'available' : 'unavailable'}">${leg.status[0] ? 'Доступен' : 'Приостановлен'}</dd>
      <dt>Вывод</dt><dd class="${leg.status[1] ? 'available' : 'unavailable'}">${leg.status[1] ? 'Доступен' : 'Приостановлен'}</dd></dl>` :
      '<p>Данные о вводе и выводе отсутствуют.</p>');
  }

  function showChartSettings() {
    context.showDialog('Настройки графика', `<p>Настройки сохраняются на этом устройстве.</p>
      <label class="field">Высота графика, пиксели<input type="number" name="height" min="160" max="420" required value="${Number(context.chartOptions.height)}"></label>
      ${check('labels', '1', 'Подписи спреда', context.chartOptions.labels)}
      ${check('areas', '1', 'Заливка области фандинга', context.chartOptions.areas)}
      ${check('tooltip', '1', 'Подсказка при наведении', context.chartOptions.tooltip)}`,
    context.cancel() + context.submit('Сохранить'), 'chart');
  }

  async function share() {
    try {
      await navigator.clipboard.writeText(location.href);
      context.toast('Ссылка скопирована');
    } catch {
      context.showDialog('Ссылка на экран', `<label class="field">Скопируйте адрес<input readonly value="${context.esc(location.href)}"></label>`);
      context.dialog.querySelector('input').select();
    }
  }

  return {showFilters, showExchanges, showFavorites, showBuilder, showHistory, showStatus, showChartSettings, share, builderOptions};
}
