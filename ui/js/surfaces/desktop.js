// Desktop: wallpaper, app shortcuts, optional clock widget, installer card on live media,
// right-click menu.

import { api, launch, on, openSettings, saveSettings, withToken } from '../api.js';
import { clockTicker, fmtDate, fmtTime, greeting, h, icon } from '../ui.js';

export function mount(root, store) {
  root.className = 'desktop';
  const wall = h('div.wallpaper');
  const walls = h('div.walls'); // with more than one screen: a wallpaper on each
  const clock = h('div.desk-clock');
  const icons = h('div.desk-icons', { role: 'list', 'aria-label': 'Desktop shortcuts' });
  const area = h('div.desk-area', icons, clock); // the main screen: shortcuts and the clock live here
  root.append(wall, walls, area);

  // ---- screens: the desktop spans them all; each gets the wallpaper, the main one the shortcuts ----
  let monitors = [];
  function renderScreens() {
    const many = monitors.length > 1;
    wall.hidden = many;
    walls.replaceChildren(...(many ? monitors.map((m) => h('div.wallpaper.ready', { style: {
      left: `${m.x}px`, top: `${m.y}px`, width: `${m.width}px`, height: `${m.height}px`, inset: 'auto',
      backgroundImage: wall.style.backgroundImage } })) : []));
    const main = monitors.find((m) => m.primary) || monitors[0];
    Object.assign(area.style, many && main
      ? { left: `${main.x}px`, top: `${main.y}px`, width: `${main.width}px`, height: `${main.height}px`, right: 'auto', bottom: 'auto' }
      : { left: '', top: '', width: '', height: '', right: '', bottom: '' });
  }
  api.get('/api/monitors').then((r) => { monitors = r.monitors || []; renderScreens(); }, () => {});
  on('monitors', (e) => { monitors = e.monitors || []; renderScreens(); });

  // ---- shortcuts on the desktop (like Windows): double-click to open, right-click for more ----
  let selected = null;
  const run = (fn) => Promise.resolve().then(fn).catch((err) => console.warn(err.message));
  function renderIcons() {
    const { settings, apps } = store.state;
    const byId = new Map(apps.map((a) => [a.id, a]));
    const items = settings.desktopIcons.map((id) => byId.get(id)).filter((a) => a && !a.superseded);
    const single = settings.desktopOpen === 'single';
    icons.replaceChildren(...items.map((app) => {
      const el = h('button.desk-icon', { role: 'listitem', class: app.id === selected ? 'sel' : '', title: app.description || app.name },
        h('img', { src: withToken(app.icon), alt: '', draggable: 'false' }), h('span', app.name));
      el.addEventListener('click', (e) => {
        e.stopPropagation();
        if (single) return run(() => launch(app.id));
        selected = app.id;
        renderIcons();
      });
      el.addEventListener('dblclick', () => { if (!single) run(() => launch(app.id)); });
      el.addEventListener('keydown', (e) => { if (e.key === 'Enter') run(() => launch(app.id)); });
      el.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        e.stopPropagation();
        selected = app.id;
        renderIcons();
        const { pinned, desktopIcons } = store.state.settings;
        const onDock = pinned.includes(app.id);
        showMenu(e, [
          ['arrowRight', 'Open', () => launch(app.id)],
          [onDock ? 'unpin' : 'pin', onDock ? 'Unpin from dock' : 'Pin to dock',
            () => saveSettings({ pinned: onDock ? pinned.filter((id) => id !== app.id) : [...pinned, app.id] })],
          null,
          ['close', 'Remove from desktop', () => saveSettings({ desktopIcons: desktopIcons.filter((id) => id !== app.id) })],
        ]);
      });
      return el;
    }));
    root.classList.toggle('has-icons', items.length > 0);
  }
  root.addEventListener('click', () => { if (selected) { selected = null; renderIcons(); } });

  let wallpaperKey = null;
  function renderWallpaper() {
    const key = store.state.settings.wallpaper;
    if (key === wallpaperKey) return;
    wallpaperKey = key;
    const url = withToken(`/wallpaper/current?v=${encodeURIComponent(key)}`);
    const img = new Image();
    img.onload = () => {
      wall.style.backgroundImage = `url("${url}")`;
      wall.classList.add('ready');
      renderScreens();
      hideSplash();
    };
    img.onerror = () => {
      wall.classList.add('ready');
      hideSplash();
    };
    img.src = url;
  }

  function renderClock(now = new Date()) {
    const { settings, user } = store.state;
    clock.hidden = !settings.desktopClock;
    if (clock.hidden) return;
    const first = (user.fullName || user.name).split(' ')[0];
    clock.replaceChildren(
      h('div.desk-time', fmtTime(now, settings, false)),
      h('div.desk-date', fmtDate(now)),
      h('div.desk-greet', store.state.env.live ? 'Welcome to PolyOS 7' : `${greeting(now)}, ${first}`),
    );
  }

  // Startup splash, as PolyOS shows while it finishes setting up.
  const splash = h('div.splash', h('img.splash-logo', { src: '/img/logo-white.svg', alt: '' }), h('div.splash-text', 'Finishing up…'));
  root.append(splash);
  const started = Date.now();
  const hideSplash = () => {
    setTimeout(() => {
      splash.classList.add('done');
      splash.addEventListener('transitionend', () => splash.remove(), { once: true });
    }, Math.max(0, 1400 - (Date.now() - started)));
  };

  // Live USB: the PolyOS "It's time to get started" card, behind the installer until it's closed.
  // Once you choose to try PolyOS it goes away; "Install PolyOS 7" in the dock and on the desktop
  // brings the installer back.
  const { env } = store.state;
  let card = null;
  const syncCard = () => {
    const want = env.live && !store.state.settings.setupDone;
    if (want && !card) {
      card = h('div.welcome',
        h('div.welcome-main',
          h('h2', 'It’s time to get started.'),
          h('p', 'Install PolyOS on this computer, or keep exploring first. Nothing is saved until you install.'),
          h('button.choice', { onclick: () => api.post('/api/open', { app: 'setup' }) },
            h('span.choice-text', h('b', 'Install PolyOS 7'), h('small', 'Start a fresh install or dual boot')),
            h('img', { src: '/img/logo-white.svg', alt: '' })),
          h('button.choice.secondary', { onclick: () => api.post('/api/setup/done', {}) },
            h('span.choice-text', h('b', 'Keep exploring'), h('small', 'Try PolyOS from this USB drive')),
            icon('arrowRight'))),
        h('img.welcome-mark', { src: '/img/logo.svg', alt: '' }));
      root.append(card);
    } else if (!want && card) {
      card.remove();
      card = null;
    }
  };
  syncCard();

  // ---- context menu ------------------------------------------------------------
  let menu = null;
  const closeMenu = () => {
    menu?.remove();
    menu = null;
  };
  const open = (app, page) => api.post('/api/open', { app, page });
  const items = [
    ['plus', 'Add apps to the desktop', () => api.post('/api/popup', { view: 'launcher', data: { target: 'desktop' } })],
    ['taskbar', 'Taskbar settings', () => openSettings('taskbar')],
    null,
    ['palette', 'Personalization', () => openSettings('appearance')],
    ['monitor', 'Display settings', () => openSettings('display')],
    null,
    ['activity', 'Task Manager', () => open('taskmgr')],
    ['terminal', 'Open Terminal', () => api.post('/api/run', { what: 'terminal' })],
    ['folder', 'Open Files', () => api.post('/api/run', { what: 'files' })],
    ['bag', 'PolyMarket', () => open('store')],
    null,
    ['settings', 'Settings', () => openSettings()],
    ['info', 'About PolyOS', () => openSettings('about')],
  ];
  root.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    showMenu(e, items);
  });
  function showMenu(e, list) {
    closeMenu();
    menu = h(
      'div.menu',
      { role: 'menu' },
      list.map((it) =>
        it
          ? h('button.menu-item', { role: 'menuitem', onclick: () => { closeMenu(); run(it[2]); } },
            icon(it[0]), it[1])
          : h('div.menu-sep'),
      ),
    );
    root.append(menu);
    const { innerWidth: w, innerHeight: hgt } = window;
    const r = menu.getBoundingClientRect();
    menu.style.left = `${Math.min(e.clientX, w - r.width - 8)}px`;
    menu.style.top = `${Math.min(e.clientY, hgt - r.height - 8)}px`;
  }
  root.addEventListener('pointerdown', (e) => {
    if (menu && !menu.contains(e.target)) closeMenu();
  });
  window.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMenu(); });
  window.addEventListener('blur', closeMenu);

  store.subscribe((_s, changed) => {
    if (changed.has('settings')) {
      renderWallpaper();
      renderClock();
      syncCard();
    }
    if (changed.has('settings') || changed.has('apps')) renderIcons();
  });
  renderIcons();
  renderWallpaper();
  clockTicker(renderClock, () => false);
}
