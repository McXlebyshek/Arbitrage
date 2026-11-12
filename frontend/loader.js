if (location.protocol === 'file:') {
  document.querySelector('#app').textContent = 'Для деморежима запустите Start-UI.cmd и откройте /?demo=1.';
} else {
  import('./bootstrap.mjs').catch(() => {
    document.querySelector('#app').textContent = 'Не удалось загрузить интерфейс. Обновите страницу.';
  });
}
