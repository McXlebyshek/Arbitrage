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

export function icon(name) {
  return `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name]}</svg>`;
}

export const esc = value => String(value).replace(/[&<>"']/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[character]));
export const pct = (n, decimals = 2) => Number.isFinite(n) ? `${n.toFixed(decimals)}%` : '—';
export const price = n => Number.isFinite(n) ? Number(n.toPrecision(8)).toLocaleString('ru', {maximumFractionDigits: 14}) : '—';
export const money = n => Number.isFinite(n) ? n.toLocaleString('ru', {maximumFractionDigits: 2}) : '—';
export const signed = (n, decimals = 2) => (n > 0 ? '+' : '') + pct(n, decimals);
export const tones = n => n > 0.05 ? 'positive' : n < -0.15 ? 'negative' : 'neutral';

export function menu(label, content, classes = '') {
  return `<details class="menu ${classes}"><summary>${label}${icon('chevron')}</summary><div class="menu-panel">${content}</div></details>`;
}
export function button(label, action, classes = '', extra = '') {
  return `<button type="button" class="${classes}" data-action="${action}" ${extra}>${label}</button>`;
}
export function options(items, selected) {
  return Object.entries(items).map(([value, label]) =>
    `<option value="${esc(value)}"${String(selected) === value ? ' selected' : ''}>${esc(label)}</option>`).join('');
}
