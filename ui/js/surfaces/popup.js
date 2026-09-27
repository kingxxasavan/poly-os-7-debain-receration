// Popup surface: hosts whichever view the backend opened (Start, launcher, quick settings, ...).

import { api, closePopup, on } from '../api.js';
import { h } from '../ui.js';
import calendar from '../views/calendar.js';
import home from '../views/home.js';
import launcher from '../views/launcher.js';
import power from '../views/power.js';
import project from '../views/project.js';
import quick from '../views/quick.js';
import quickmenu from '../views/quickmenu.js';
import run from '../views/run.js';
import taskmenu from '../views/taskmenu.js';
import tray from '../views/tray.js';
import vara from '../views/vara.js';
import widgets from '../views/widgets.js';

// "start" is the Start menu (the name keybindings and polyos-ctl use).
const views = { start: home, launcher, power, run, quick, calendar, taskmenu, vara, quickmenu, widgets, tray, project };

export function mount(root, store) {
  root.className = 'popup-root';
  let current = null;
  let cleanup = null;

  function show(popup) {
    if (cleanup) cleanup();
    cleanup = null;
    current = popup;
    if (!popup || !views[popup.view]) {
      root.replaceChildren();
      return;
    }
    const card = h(`div.card.view-${popup.view}`);
    root.replaceChildren(card);
    cleanup = views[popup.view](card, store, popup.data || {}) || null;
    focusDefault();
  }

  function focusDefault() {
    const target = root.querySelector('[autofocus]');
    if (target) target.focus();
  }

  on('popup', (e) => show(e.popup));
  show(store.state.popup);

  // Losing focus closes the popup (the shell also watches the X focus).
  window.addEventListener('blur', () => {
    if (current) api.post('/api/popup/closed', {}).catch(() => {});
  });
  window.addEventListener('focus', focusDefault);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && current && !e.defaultPrevented) closePopup();
  });
}
