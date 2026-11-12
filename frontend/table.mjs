export function visibleRange(count, tableTop, viewportHeight, rowHeight) {
  if (count <= 30) return [0, count];
  const first = Math.max(0, Math.floor(-tableTop / rowHeight) - 6);
  const last = Math.min(count, Math.ceil((viewportHeight - tableTop) / rowHeight) + 6);
  return [Math.min(first, count - 1), Math.max(Math.min(first + 1, count), last)];
}

function replaceChangedCells(current, desired) {
  const cells = [...desired.cells];
  for (let index = 0; index < cells.length; index++) {
    const oldCell = current.cells[index];
    const newCell = cells[index];
    if (!oldCell || oldCell.outerHTML !== newCell.outerHTML) {
      if (oldCell) oldCell.replaceWith(newCell);
      else current.append(newCell);
    }
  }
  while (current.cells.length > cells.length) current.lastElementChild.remove();
}

export function updateRows(container, html, rowKeys, hiddenIndices) {
  const template = document.createElement('template');
  template.innerHTML = html;
  const desired = template.content.firstElementChild;
  for (const index of hiddenIndices) {
    desired.querySelectorAll(`tr > :nth-child(${index}), col:nth-child(${index})`).forEach(element => { element.hidden = true; });
  }
  let table = container.querySelector(':scope > table');
  if (!table) {
    container.append(desired);
    table = desired;
    [...table.tBodies[0].rows].forEach((row, index) => { row.dataset.key = rowKeys[index]; });
    return;
  }
  const active = table.contains(document.activeElement) ? document.activeElement : null;
  const focus = active && {
    key: active.closest('tr[data-key]')?.dataset.key,
    id: active.id,
    action: active.dataset.action,
    pair: active.dataset.pair,
    tag: active.tagName,
    href: active.getAttribute('href'),
  };
  table.className = desired.className;
  table.setAttribute('aria-rowcount', desired.getAttribute('aria-rowcount'));
  if (table.querySelector('colgroup').outerHTML !== desired.querySelector('colgroup').outerHTML) {
    table.querySelector('colgroup').replaceWith(desired.querySelector('colgroup'));
  }
  if (table.tHead.outerHTML !== desired.tHead.outerHTML) replaceChangedCells(table.tHead.rows[0], desired.tHead.rows[0]);
  const tbody = table.tBodies[0];
  const existing = new Map([...tbody.rows].map(row => [row.dataset.key, row]));
  let cursor = tbody.firstElementChild;
  [...desired.tBodies[0].rows].forEach((newRow, index) => {
    const key = rowKeys[index];
    let current = existing.get(key);
    if (current) {
      existing.delete(key);
      replaceChangedCells(current, newRow);
      for (const attribute of [...current.attributes]) if (attribute.name !== 'data-key' && !newRow.hasAttribute(attribute.name)) current.removeAttribute(attribute.name);
      for (const attribute of [...newRow.attributes]) if (attribute.name !== 'data-key') current.setAttribute(attribute.name, attribute.value);
    } else {
      current = newRow;
      current.dataset.key = key;
    }
    if (current !== cursor) {
      if (current.parentNode === tbody && typeof tbody.moveBefore === 'function') tbody.moveBefore(current, cursor);
      else tbody.insertBefore(current, cursor);
    }
    cursor = current.nextElementSibling;
  });
  for (const row of existing.values()) row.remove();
  if (focus && !table.contains(document.activeElement)) {
    const target = [...table.querySelectorAll('a, button, select')].find(element =>
      (!focus.key || element.closest('tr[data-key]')?.dataset.key === focus.key) &&
      (focus.id ? element.id === focus.id : element.tagName === focus.tag &&
        element.dataset.action === focus.action && element.dataset.pair === focus.pair && element.getAttribute('href') === focus.href));
    (target ?? container).focus({preventScroll: true});
  }
}
