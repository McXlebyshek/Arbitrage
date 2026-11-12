const {test} = require('node:test');
const assert = require('node:assert/strict');

function storageMock() {
  const values = new Map();
  return {
    getItem: key => values.has(key) ? values.get(key) : null,
    setItem: (key, value) => values.set(key, String(value)),
  };
}

test('preferences migrate legacy values and reject only an incompatible version', async () => {
  global.localStorage = storageMock();
  const {readPreference} = await import('../frontend/preferences.mjs');
  localStorage.setItem('arbitrage:theme', '"dark"');
  assert.equal(readPreference('theme', 'light'), 'dark');
  assert.deepEqual(JSON.parse(localStorage.getItem('arbitrage:theme')),
    {schemaVersion: 1, value: 'dark'});

  localStorage.setItem('arbitrage:layout', JSON.stringify({schemaVersion: 0, value: 'compact'}));
  assert.equal(readPreference('layout', 'regular'), 'regular');
  assert.deepEqual(JSON.parse(localStorage.getItem('arbitrage:theme')),
    {schemaVersion: 1, value: 'dark'});
});
