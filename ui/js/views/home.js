// The Start menu (the "start" popup), laid out like Windows 11's:
//   search
//   Pinned (a page of apps; right-click to pin, unpin or put on the desktop) · All apps ›
//   Recommended: files and apps you opened lately
//   you · Vara · Settings · power (lock, sleep, sign out, restart, shut down)
// "All apps" lists every app A to Z; typing searches apps, settings and your recent files.

import { api, closePopup, launch, openSettings, power, saveSettings, withToken } from '../api.js';
import { fileSvg, h, icon, searchApps } from '../ui.js';

const PER_PAGE = 18; // 6 columns, 3 rows
const SETTINGS_PAGES = [
  ['display', 'Display', 'monitor', 'screen resolution brightness night light scale'],
  ['sound', 'Sound', 'volume', 'speakers microphone volume output input'],
  ['account', 'Account', 'user', 'password pin name picture user poly account sync sign in cloud'],
  ['privacy', 'Privacy & Security', 'shield', 'firewall camera microphone lock'],
  ['network', 'Wi-Fi & Network', 'wifi', 'internet wireless ethernet airplane'],
  ['appearance', 'Personalization', 'palette', 'appearance wallpaper background theme dark light accent color'],
  ['taskbar', 'Taskbar & Desktop', 'taskbar', 'dock icons widgets clock'],
  ['gaming', 'Gaming', 'gamepad', 'game mode cloud controllers steam'],
  ['vara', 'Vara', 'chat', 'assistant ai'],
  ['apps', 'Apps', 'apps', 'uninstall startup default programs'],
  ['storage', 'Storage', 'disk', 'disk space drive free clean trash'],
  ['power', 'Power & Performance', 'bolt', 'battery energy saver sleep screen off'],
  ['updates', 'Updates', 'download', 'update upgrade version'],
  ['about', 'About', 'info', 'version computer name system information'],
];
const POWER = [['lock', 'Lock', 'lock'], ['moon', 'Sleep', 'suspend'], ['logout', 'Sign out', 'logout'],
  ['restart', 'Restart', 'reboot'], ['power', 'Shut down', 'poweroff']];

const popup = (view, data, extra = {}) => api.post('/api/popup', { view, data, ...extra }).catch((err) => console.warn(err.message));

function ago(t) {
  const mins = Math.round((Date.now() / 1000 - t) / 60);
  if (mins < 1) return 'Just now';
  if (mins < 60) return `${mins} min ago`;
  if (mins < 24 * 60) return `${Math.round(mins / 60)}h ago`;
  const days = Math.round(mins / 1440);
  return days === 1 ? 'Yesterday' : days < 7 ? `${days} days ago` : new Date(t * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

export default function home(root, store) {
  root.classList.add('start-menu');
  const { user } = store.state;
  const visible = () => store.state.apps.filter((a) => store.state.settings.showAllApps || !a.hidden);
  const open = (app) => { launch(app.id).catch((err) => console.warn(err.message)); closePopup(); };
  const openFile = (f) => { api.post('/api/files/open', { path: f.path }).catch((err) => console.warn(err.message)); closePopup(); };
  const appImg = (app) => h('img', { src: withToken(app.icon), alt: '', draggable: 'false' });
  const fileImg = (f) => { const el = h('span.sm-file-ico'); el.innerHTML = fileSvg(f.kind || 'file', f.name); return el; };

  const input = h('input.sm-input', { type: 'search', placeholder: 'Search for apps, settings and documents', autocomplete: 'off',
    spellcheck: 'false', autofocus: true, 'aria-label': 'Search' });
  const body = h('div.sm-body');
  root.append(h('label.sm-search', icon('search'), input), body, footer());

  let windows = false; // a dual-boot computer: Power › Restart to Windows
  api.get('/api/power/windows').then((r) => { windows = !!r.available; }, () => {});
  let view = 'pinned';
  let page = 0;
  let files = [];
  api.get('/api/recent-files').then((r) => { files = r.files || []; if (view === 'pinned' || input.value) render(); }, () => {});

  // ---- right-click an app: pin to Start or the taskbar, put it on the desktop ----
  let menu = null;
  const closeMenu = () => { menu?.remove(); menu = null; };
  function appMenu(app, x, y) {
    closeMenu();
    const s = store.state.settings;
    const has = (key) => s[key].includes(app.id);
    const flip = (key) => saveSettings({ [key]: has(key) ? s[key].filter((id) => id !== app.id) : [...s[key], app.id] })
      .catch((err) => console.warn(err.message));
    const item = (ico, label, fn) => h('button.menu-item', { role: 'menuitem', onclick: (e) => { e.stopPropagation(); closeMenu(); fn(); } }, icon(ico), label);
    menu = h('div.menu.sm-menu', { role: 'menu' },
      item('arrowRight', 'Open', () => open(app)),
      item(has('startPinned') ? 'unpin' : 'pin', has('startPinned') ? 'Unpin from Start' : 'Pin to Start', () => flip('startPinned')),
      item(has('pinned') ? 'unpin' : 'taskbar', has('pinned') ? 'Unpin from taskbar' : 'Pin to taskbar', () => flip('pinned')),
      item(has('desktopIcons') ? 'close' : 'monitor', has('desktopIcons') ? 'Remove from desktop' : 'Add to desktop', () => flip('desktopIcons')),
      item('apps', 'App settings', () => { openSettings('apps'); closePopup(); }));
    root.append(menu);
    const r = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(8, Math.min(x, innerWidth - r.width - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(y, innerHeight - r.height - 8))}px`;
  }
  root.addEventListener('pointerdown', (e) => { if (menu && !menu.contains(e.target)) closeMenu(); });
  const withMenu = (el, app) => {
    el.addEventListener('contextmenu', (e) => { e.preventDefault(); appMenu(app, e.clientX, e.clientY); });
    return el;
  };

  const head = (title, action) => h('div.sm-head', h('b', title), action || null);
  const pill = (label, ico, fn, before = false) => h('button.sm-pill', { onclick: fn }, before ? icon(ico) : null, label, before ? null : icon(ico));

  // ---- the views ----
  function pinnedView() {
    const byId = new Map(visible().map((a) => [a.id, a]));
    const pins = store.state.settings.startPinned.map((id) => byId.get(id)).filter((a) => a && !a.superseded);
    const pages = Math.max(1, Math.ceil(pins.length / PER_PAGE));
    page = Math.min(page, pages - 1);
    const grid = h('div.sm-grid', pins.slice(page * PER_PAGE, (page + 1) * PER_PAGE).map((app) =>
      withMenu(h('button.sm-app', { title: app.description || app.name, onclick: () => open(app) }, appImg(app), h('span', app.name)), app)));
    if (!pins.length) grid.append(h('p.sm-empty', 'Right-click an app in All apps and choose Pin to Start.'));
    const dots = pages > 1 ? h('div.sm-dots', Array.from({ length: pages }, (_, i) =>
      h('button.sm-dot', { class: i === page ? 'on' : '', 'aria-label': `Page ${i + 1}`, onclick: () => { page = i; render(); } }))) : null;
    grid.addEventListener('wheel', (e) => {
      if (pages < 2 || Math.abs(e.deltaY) < 20) return;
      const next = Math.max(0, Math.min(pages - 1, page + (e.deltaY > 0 ? 1 : -1)));
      if (next !== page) { page = next; render(); }
    }, { passive: true });

    // Recommended: recent files and recently opened apps (not the pinned ones), newest first
    const recentApps = store.state.settings.recent.map((id) => byId.get(id))
      .filter((a) => a && !store.state.settings.startPinned.includes(a.id)).slice(0, 3)
      .map((app) => ({ kind: 'app', app, time: null }));
    const recs = [...files.slice(0, 6 - recentApps.length).map((f) => ({ kind: 'file', f, time: f.time })), ...recentApps].slice(0, 6);
    const recGrid = h('div.sm-recs', recs.map((r) => (r.kind === 'file'
      ? h('button.sm-rec', { title: r.f.path, onclick: () => openFile(r.f) }, fileImg(r.f),
        h('span.sm-rec-text', h('b', r.f.name), h('small', ago(r.f.time))))
      : withMenu(h('button.sm-rec', { onclick: () => open(r.app) }, appImg(r.app),
        h('span.sm-rec-text', h('b', r.app.name), h('small', 'Recently opened'))), r.app))));
    if (!recs.length) recGrid.append(h('p.sm-empty', 'The files and apps you open will show here.'));
    return [
      head('Pinned', pill('All apps', 'chevronRight', () => { view = 'all'; render(); })),
      grid, dots,
      head('Recommended'),
      recGrid,
    ];
  }

  function allView() {
    const list = h('div.sm-list');
    let letter = '';
    for (const app of [...visible()].filter((a) => !a.superseded).sort((a, b) => a.name.localeCompare(b.name))) {
      const first = /[a-z]/i.test(app.name[0]) ? app.name[0].toUpperCase() : '#';
      if (first !== letter) { letter = first; list.append(h('div.sm-letter', first)); }
      list.append(withMenu(h('button.sm-row', { title: app.description || app.name, onclick: () => open(app) }, appImg(app), h('span', app.name)), app));
    }
    return [head('All apps', pill('Back', 'chevronLeft', () => { view = 'pinned'; render(); }, true)), list];
  }

  function searchView(q) {
    const apps = searchApps(visible().filter((a) => !a.superseded), q).slice(0, 8);
    const plain = (t) => t.toLowerCase().replace(/[^a-z0-9 ]/g, ''); // "wifi" finds "Wi-Fi"
    const ql = plain(q.trim());
    const typed = ql.split(' ').filter(Boolean); // "night light", "poly account": every word counts
    const pages = SETTINGS_PAGES.filter(([, name, , words]) => {
      const keys = plain(`${name} ${words}`).split(' ').filter(Boolean);
      return plain(name).includes(ql) || typed.every((t) => keys.some((k) => k.startsWith(t)));
    }).slice(0, 4);
    const docs = files.filter((f) => plain(f.name).includes(ql)).slice(0, 4);
    const out = [];
    const best = apps[0] ? { app: apps[0] } : pages[0] ? { page: pages[0] } : docs[0] ? { file: docs[0] } : null;
    if (!best) return [h('p.sm-empty.sm-none', `No results for “${q.trim()}”`)];
    const bestEl = best.app
      ? withMenu(h('button.sm-best', { onclick: () => open(best.app) }, appImg(best.app), h('span.sm-rec-text', h('b', best.app.name), h('small', 'App'))), best.app)
      : best.page ? h('button.sm-best', { onclick: () => { openSettings(best.page[0]); closePopup(); } }, h('span.sm-set-ico', icon(best.page[2])), h('span.sm-rec-text', h('b', best.page[1]), h('small', 'Settings')))
        : h('button.sm-best', { onclick: () => openFile(best.file) }, fileImg(best.file), h('span.sm-rec-text', h('b', best.file.name), h('small', 'Document')));
    out.push(h('div.sm-cap', 'Best match'), bestEl);
    const restApps = apps.filter((a) => a !== best.app);
    if (restApps.length) out.push(h('div.sm-cap', 'Apps'), ...restApps.map((app) => withMenu(h('button.sm-row', { onclick: () => open(app) }, appImg(app), h('span', app.name)), app)));
    const restPages = pages.filter((p) => p !== best.page);
    if (restPages.length) {
      out.push(h('div.sm-cap', 'Settings'), ...restPages.map(([id, name, ico]) =>
        h('button.sm-row', { onclick: () => { openSettings(id); closePopup(); } }, h('span.sm-set-ico', icon(ico)), h('span', name))));
    }
    const restDocs = docs.filter((f) => f !== best.file);
    if (restDocs.length) out.push(h('div.sm-cap', 'Documents'), ...restDocs.map((f) => h('button.sm-row', { title: f.path, onclick: () => openFile(f) }, fileImg(f), h('span', f.name))));
    return [h('div.sm-list.sm-results', out)];
  }

  function render() {
    closeMenu();
    const q = input.value.trim();
    body.className = `sm-body sm-v-${q ? 'results' : view}`;
    body.replaceChildren(...(q ? searchView(q) : view === 'all' ? allView() : pinnedView()).filter(Boolean));
  }

  // ---- the footer: you, Vara, Settings and power ----
  function footer() {
    const name = user.fullName || user.name;
    const powerBtn = h('button.sm-round', { title: 'Power', 'aria-label': 'Power', 'aria-haspopup': 'menu' }, icon('power'));
    powerBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (menu) return closeMenu();
      const items = POWER.map(([ico, label, action]) =>
        h('button.menu-item', { role: 'menuitem', onclick: async (ev) => {
          ev.stopPropagation();
          closeMenu();
          try { await power(action); } catch (err) { console.warn(err.message); }
        } }, icon(ico), label));
      if (windows) {
        items.push(h('div.menu-sep'), h('button.menu-item', { role: 'menuitem', onclick: (ev) => {
          ev.stopPropagation();
          closeMenu();
          api.post('/api/power/windows', {}).catch((err) => console.warn(err.message));
        } }, icon('window'), 'Restart to Windows'));
      }
      menu = h('div.menu.sm-menu.sm-power', { role: 'menu' }, items);
      root.append(menu);
      const r = powerBtn.getBoundingClientRect();
      const m = menu.getBoundingClientRect();
      menu.style.left = `${Math.max(8, r.right - m.width)}px`;
      menu.style.top = `${r.top - m.height - 8}px`;
    });
    return h('div.sm-foot',
      h('button.sm-user', { title: 'Account settings', onclick: () => { openSettings('account'); closePopup(); } },
        h('span.sm-avatar', (name || '?').trim()[0].toUpperCase()), h('span', name)),
      h('div.sm-foot-right',
        h('button.sm-round', { title: 'Ask Vara', onclick: () => popup('vara') }, h('img.sm-vara', { src: '/img/vara.png', alt: '' })),
        h('button.sm-round', { title: 'Files', onclick: () => { api.post('/api/open', { app: 'files' }); closePopup(); } }, icon('folder')),
        h('button.sm-round', { title: 'Settings', onclick: () => { openSettings(); closePopup(); } }, icon('settings')),
        powerBtn));
  }

  input.addEventListener('input', render);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const first = body.querySelector('.sm-best, .sm-app, .sm-row');
      if (first && input.value.trim()) first.click();
    } else if (e.key === 'Escape' && (input.value || view !== 'pinned')) {
      e.preventDefault();
      input.value = '';
      view = 'pinned';
      render();
    }
  });

  const unsubscribe = store.subscribe((_s, changed) => {
    if ((changed.has('apps') || changed.has('settings')) && !menu) render();
  });
  render();
  return unsubscribe;
}
