import {readPreference, savePreference} from './preferences.mjs';
import {listSettings} from './state.mjs';
import {updateRows, visibleRange} from './table.mjs';

globalThis.monitorModules = {readPreference, savePreference, listSettings, updateRows, visibleRange};
const script = document.createElement('script');
script.src = 'app.js';
document.head.append(script);
