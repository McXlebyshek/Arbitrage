/* The scroll window keeps DOM work bounded for long screener results. */
const marketTableWindow = (() => {
  const threshold = 160;
  const overscan = 14;

  function calculate(table, rows, limit, compact) {
    const count = Math.min(rows.length, limit);
    if (count <= threshold) return {count, ranges: [[0, count]], height: 0};
    const body = table.querySelector('tbody');
    const sample = body?.querySelector('[data-pair-row]');
    const previousCompact = table.firstElementChild?.classList.contains('compact');
    const height = previousCompact === compact && sample?.getBoundingClientRect().height ||
      (compact ? 49 : 64);
    const top = body?.getBoundingClientRect().top ?? table.getBoundingClientRect().top + 39;
    const first = Math.min(count - 1, Math.max(0, Math.floor(-top / height)));
    const end = Math.min(count, Math.max(first + 1, Math.ceil((window.innerHeight - top) / height)) + overscan);
    const start = Math.max(0, first - overscan);
    const ranges = [[start, end]];
    const focusedId = document.activeElement?.closest('[data-pair-row]')?.dataset.pairRow;
    if (focusedId) {
      const focused = rows.findIndex(([pair], index) => index < count && pair.id === focusedId);
      if (focused >= 0 && (focused < start || focused >= end)) {
        ranges.push([focused, focused + 1]);
        ranges.sort(([a], [b]) => a - b);
      }
    }
    return {count, ranges, height};
  }

  function markup({count, ranges, height}, rows, columns, row) {
    const spacer = (start, end) => end > start
      ? `<tr class="market-spacer" data-window-spacer="${start}" aria-hidden="true"><td colspan="${columns}" style="height:${(end - start) * height}px"></td></tr>`
      : '';
    let html = '';
    let cursor = 0;
    for (const [start, end] of ranges) {
      html += spacer(cursor, start);
      html += rows.slice(start, end).map((item, index) => row(item, start + index)).join('');
      cursor = end;
    }
    return html + spacer(cursor, count);
  }

  return {calculate, markup};
})();
