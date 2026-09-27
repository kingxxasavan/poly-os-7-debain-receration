// Boots one surface (desktop, panel, popup or settings) for this page.

import { api, connectEvents, on, surface } from './api.js';
import { store } from './store.js';
import { applyTheme, h } from './ui.js';

const surfaces = {
  desktop: () => import('./surfaces/desktop.js'),
  panel: () => import('./surfaces/panel.js'),
  popup: () => import('./surfaces/popup.js'),
  settings: () => import('./surfaces/settings.js'),
  files: () => import('./surfaces/files.js'),
  setup: () => import('./surfaces/setup.js'),
  greeter: () => import('./surfaces/greeter.js'),
  taskmgr: () => import('./surfaces/taskmgr.js'),
  drivers: () => import('./surfaces/drivers.js'),
  store: () => import('./surfaces/store.js'),
  lock: () => import('./surfaces/lock.js'),
  camera: () => import('./surfaces/camera.js'),
};

async function resync() {
  try {
    store.set(await api.get('/api/state'));
    applyTheme(store.state);
  } catch (err) {
    console.warn('resync failed:', err.message);
  }
}

async function boot() {
  document.documentElement.dataset.surface = surface;
  store.state = await api.get('/api/state');
  applyTheme(store.state);

  on('windows', (e) => store.set({ windows: e.windows }));
  on('apps', (e) => store.set({ apps: e.apps }));
  on('system', (e) => store.set({ system: e.system }));
  on('settings', (e) => {
    store.set({ settings: e.settings });
    applyTheme(store.state);
  });
  on('popup', (e) => store.set({ popup: e.popup }));
  on('connected', resync); // catch up on anything missed while disconnected
  connectEvents();

  const mod = await (surfaces[surface] || surfaces.desktop)();
  mod.mount(document.getElementById('root'), store);
}

boot().catch((err) => {
  console.error(err);
  document.getElementById('root').replaceChildren(h('pre.fatal', `PolyOS could not start this surface.\n\n${err.message}`));
});
