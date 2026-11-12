export function listSettings(params, {numericFilters, modes, defaults, categories, exchanges, quotes}) {
  const mode = params.get('mode');
  const sort = params.get('sort');
  const filters = {};
  for (const key of Object.keys(numericFilters)) {
    const value = params.get(key);
    if (value && Number.isFinite(Number(value))) filters[key] = value;
  }
  for (const key of ['longFee', 'shortFee', 'holdDays']) {
    const value = params.get(key);
    if (value !== null && value !== '' && Number.isFinite(Number(value))) filters[key] = value;
  }
  return {
    ...filters,
    mode: Object.hasOwn(modes, mode) ? mode : defaults.mode,
    sort: ['coin', 'delta', 'apr', 'aprAfterFees', 'funding', 'entry'].includes(sort) ? sort : defaults.sort,
    direction: params.get('direction') === 'asc' ? 'asc' : 'desc',
    group: params.get('group') !== '0', expand: params.get('expand') ?? '',
    categories: params.has('categories') ? params.get('categories').split(',').filter(item => Object.hasOwn(categories, item)) : defaults.categories,
    exchanges: params.has('exchanges') ? params.get('exchanges').split(',').filter(item => exchanges.includes(item)) : exchanges,
    quotes: (params.get('quotes') ?? '').split(',').filter(item => quotes.includes(item)),
    query: params.get('coin') ?? '', operator: params.get('operator') === 'lte' ? 'lte' : 'gte',
    favorites: params.get('favorites') === '1',
  };
}
