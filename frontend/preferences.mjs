const SCHEMA_VERSION = 1;

const validators = {
  theme: value => value === 'dark' || value === 'light',
  layout: value => value === 'regular' || value === 'compact',
  wide: value => typeof value === 'boolean',
  favorites: value => Array.isArray(value) && value.every(item => typeof item === 'string'),
  'hidden-columns': value => Array.isArray(value) && value.every(item => typeof item === 'string'),
  chart: value => value !== null && typeof value === 'object' && !Array.isArray(value),
  presets: value => Array.isArray(value) && value.every(item => item && typeof item.name === 'string' && typeof item.query === 'string'),
  'signal-rules': value => Array.isArray(value) && value.every(item => item && typeof item === 'object' && !Array.isArray(item)),
  'signal-log': value => Array.isArray(value) && value.every(item => item && typeof item === 'object' && !Array.isArray(item)),
};

export function savePreference(key, value) {
  try {
    localStorage.setItem(`arbitrage:${key}`, JSON.stringify({schemaVersion: SCHEMA_VERSION, value}));
  } catch { /* Preferences remain available for this page session. */ }
}

export function readPreference(key, fallback) {
  try {
    const raw = localStorage.getItem(`arbitrage:${key}`);
    if (raw === null) return fallback;
    const stored = JSON.parse(raw);
    const envelope = stored && typeof stored === 'object' && !Array.isArray(stored) &&
      Object.hasOwn(stored, 'schemaVersion');
    if (envelope && stored.schemaVersion !== SCHEMA_VERSION) return fallback;
    const value = envelope ? stored.value : stored;
    if (!validators[key]?.(value)) return fallback;
    if (!envelope) savePreference(key, value);
    return value;
  } catch {
    return fallback;
  }
}
