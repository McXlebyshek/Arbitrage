const {defineConfig} = require('@playwright/test');

const python = process.env.ARBITRAGE_PYTHON || (process.platform === 'win32'
  ? '.venv/Scripts/python.exe' : '.venv/bin/python');

module.exports = defineConfig({
  testDir: './tests/e2e',
  workers: 1,
  use: {baseURL: 'http://127.0.0.1:18784', channel: 'chrome'},
  webServer: {
    command: `"${python}" -B -m tests.e2e.fixture_server`,
    url: 'http://127.0.0.1:18784/api/health',
    reuseExistingServer: false,
    timeout: 15000,
  },
});
