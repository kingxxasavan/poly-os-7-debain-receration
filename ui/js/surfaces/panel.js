// Floating dock: Start (pinwheel) · pinned/running apps · ^ (running apps) · status and clock.

import { api, launch, on, windowAction, withToken } from '../api.js';
import { clockTicker, fill, fmtTime, h, icon, networkIcon, networkLabel, volumeIcon } from '../ui.js';
import { weatherLook } from '../views/widgets.js';

function openPopup(view, el, extra = {}) {
  const r = el.getBoundingClientRect();
  // anchorX is relative to the dock; the shell adds the dock's inset from the screen edge.
  api.post('/api/popup', { view, anchorX: Math.round(r.left + r.width / 2), ...extra })
    .catch((err) => console.warn(err.message));
}

// Pinned apps first (in pin order), then any other running apps in opening order.
function taskItems(state) {
  const apps = new Map(state.apps.map((a) => [a.id, a]));
  const items = state.settings.pinned
    .filter((id) => apps.has(id))
    .map((id) => ({ key: id, appId: id, app: apps.get(id), pinned: true, windows: [] }));
  for (const win of state.windows) {
    let item = win.appId && items.find((i) => i.appId === win.appId);
    if (!item) {
      const key = win.appId || `x${win.xid}`;
      item = { key, appId: win.appId, app: apps.get(win.appId), pinned: false, windows: [] };
      items.push(item);
    }
    item.windows.push(win);
  }
  return items;
}

export function mount(root, store) {
  root.className = 'panel';
  const start = h('button.dbtn.start-btn', { title: 'Start', 'aria-label': 'Start' },
    h('img', { src: '/img/logo.svg', alt: '' }));
  const tasks = h('div.tasks', { role: 'toolbar', 'aria-label': 'Apps' });
  const status = h('button.dbtn.tray', { 'aria-label': 'Quick settings' });
  const clock = h('button.dbtn.clock', { 'aria-label': 'Calendar' });
  // ^ like Windows: what's running, including apps that live in the background (Steam, Discord)
  const overflow = h('button.dbtn.overflow-btn', { title: 'Show running apps', 'aria-label': 'Show running apps' }, icon('chevronUp'));
  // Weather and the widgets board, like the left end of the Windows 11 taskbar
  const weather = h('button.dbtn.weather-btn', { title: 'Widgets (Win+W)', 'aria-label': 'Widgets' }, icon('widgets'));
  weather.addEventListener('click', () => openPopup('widgets', weather));
  const loadWeather = () => api.get('/api/widgets/weather').then((w) => {
    if (!w.place || w.temp == null) return fill(weather, icon('widgets'));
    const [desc, ico] = weatherLook(w.code, w.day);
    fill(weather, icon(ico), h('span.weather-text', h('b', `${Math.round(w.temp)}°`), h('small', desc)));
    weather.title = `${w.place.name}: ${desc}, ${Math.round(w.temp)}° (Win+W for widgets)`;
  }, () => {});
  loadWeather();
  setInterval(loadWeather, 15 * 60000);
  on('widgets', (e) => { if (e.keys.includes('weather')) loadWeather(); });
  // The Start menu opens over the middle of a centered taskbar, like Windows 11 (at the left when the icons are)
  start.addEventListener('click', () => (store.state.settings.taskbarAlign === 'left' ? openPopup('start', start)
    : api.post('/api/popup', { view: 'start' }).catch((err) => console.warn(err.message))));
  status.addEventListener('click', () => openPopup('quick', status));
  clock.addEventListener('click', () => openPopup('calendar', clock));
  overflow.addEventListener('click', async () => {
    let background = [];
    try { background = (await api.get('/api/background-apps')).apps; } catch { /* only the windows, then */ }
    const running = new Set(store.state.windows.map((w) => w.appId || `x${w.xid}`)).size;
    const rows = Math.max(1, running + background.length);
    const height = Math.min(520, 112 + rows * 52 + (running && background.length ? 34 : 0));
    openPopup('tray', overflow, { data: { background }, height });
  });
  root.append(
    h('div.dock-left', start, weather),
    h('div.dock-center', tasks),
    h('div.dock-right', overflow, status, clock),
  );

  const popupButtons = { start, quick: status, calendar: clock, widgets: weather, tray: overflow };
  const markPopup = (popup) => {
    for (const [view, btn] of Object.entries(popupButtons)) btn.classList.toggle('open', popup?.view === view);
  };
  on('popup', (e) => markPopup(e.popup));
  markPopup(store.state.popup);

  // ---- tasks ------------------------------------------------------------------
  const taskEls = new Map();

  function onTaskClick(item, el) {
    const wins = item.windows;
    if (!wins.length) {
      el.classList.remove('launching');
      void el.offsetWidth; // restart the animation
      el.classList.add('launching');
      return launch(item.appId).catch((err) => console.warn(err.message));
    }
    if (wins.length === 1) return windowAction(wins[0].xid, 'toggle');
    const idx = wins.findIndex((w) => w.active);
    const next = idx < 0 ? wins.find((w) => !w.minimized) || wins[0] : wins[(idx + 1) % wins.length];
    return windowAction(next.xid, 'activate');
  }

  function makeTask() {
    const img = h('img', { alt: '', draggable: 'false' });
    const el = h('button.dbtn.task', img, h('span.task-dot'));
    el.addEventListener('click', () => onTaskClick(el.item, el));
    el.addEventListener('auxclick', (e) => {
      if (e.button === 1 && el.item.appId) launch(el.item.appId);
    });
    el.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      const it = el.item;
      const count = (it.appId ? 3 : 0) + (it.windows.length ? 2 : 0) + 1;
      openPopup('taskmenu', el, {
        data: { appId: it.appId, xids: it.windows.map((w) => w.xid) },
        height: 70 + count * 42 + 10,
      });
    });
    el.addEventListener('animationend', () => el.classList.remove('launching'));
    el.img = img;
    return el;
  }

  function renderTasks() {
    const items = taskItems(store.state);
    const keep = new Set();
    items.forEach((item, i) => {
      let el = taskEls.get(item.key);
      if (!el) {
        el = makeTask();
        taskEls.set(item.key, el);
      }
      el.item = item;
      const src = withToken(item.app ? item.app.icon : item.windows[0].icon);
      if (el.img.getAttribute('src') !== src) el.img.setAttribute('src', src);
      const running = item.windows.length > 0;
      el.classList.toggle('running', running);
      el.classList.toggle('multi', item.windows.length > 1);
      el.classList.toggle('active', item.windows.some((w) => w.active && !w.minimized));
      if (running) el.classList.remove('launching');
      el.title = item.windows.length === 1 ? item.windows[0].title : item.app ? item.app.name : item.windows[0].title;
      keep.add(item.key);
      if (tasks.children[i] !== el) tasks.insertBefore(el, tasks.children[i] || null);
    });
    for (const [key, el] of taskEls) {
      if (!keep.has(key)) {
        el.remove();
        taskEls.delete(key);
      }
    }
  }

  // ---- tray -------------------------------------------------------------------
  function renderStatus() {
    const { network, volume, battery } = store.state.system;
    const parts = [h('span.tray-ico', networkIcon(network)), h('span.tray-ico', volumeIcon(volume))];
    const tips = [networkLabel(network), volume.available ? `Volume ${volume.muted ? 'muted' : `${volume.level}%`}` : 'No audio'];
    if (battery.present) {
      parts.push(h('span.tray-bat', icon('battery', battery.level, battery.charging), h('span', `${battery.level}%`)));
      tips.push(`Battery ${battery.level}%${battery.charging ? ', charging' : ''}`);
    }
    status.replaceChildren(...parts);
    status.title = tips.join('\n');
  }

  // Settings > Taskbar: style, alignment, widgets button, date under the clock.
  function syncPrefs() {
    const s = store.state.settings;
    root.classList.toggle('full', s.taskbarStyle === 'full');
    root.classList.toggle('align-left', s.taskbarAlign === 'left');
    weather.hidden = !s.taskbarWidgets;
  }

  function renderClock(now = new Date()) {
    const s = store.state.settings;
    fill(clock,
      h('span.clock-time', fmtTime(now, s)),
      s.taskbarDate ? h('span.clock-date', now.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })) : null,
    );
    clock.title = now.toLocaleDateString([], { dateStyle: 'full' });
  }

  store.subscribe((_s, changed) => {
    if (changed.has('windows') || changed.has('apps') || changed.has('settings')) renderTasks();
    if (changed.has('system')) renderStatus();
    if (changed.has('settings')) {
      renderClock();
      syncPrefs();
    }
  });
  syncPrefs();
  renderTasks();
  renderStatus();
  clockTicker(renderClock, () => store.state.settings.showSeconds);
}
