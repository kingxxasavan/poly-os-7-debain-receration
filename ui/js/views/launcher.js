// Full-screen app launcher (PolyOS 7 style): search, paged icon grid, pin mode.

import { closePopup, launch, saveSettings, withToken } from '../api.js';
import { h, icon, searchApps } from '../ui.js';

export default function launcher(root, store, data = {}) {
  root.classList.add('launcher');
  const wallpaper = withToken(`/wallpaper/current?v=${encodeURIComponent(store.state.settings.wallpaper)}`);
  const backdrop = h('div.lp-backdrop', { style: { backgroundImage: `url("${wallpaper}")` } });
  const input = h('input.lp-input', {
    type: 'search', placeholder: 'Search apps', autocomplete: 'off', spellcheck: 'false', autofocus: true,
    'aria-label': 'Search apps',
  });
  if (data.q) input.value = String(data.q).slice(0, 100); // search typed elsewhere
  const grid = h('div.lp-grid');
  const dots = h('div.lp-dots');
  const prev = h('button.lp-arrow.prev', { title: 'Previous page' }, icon('chevronLeft'));
  const next = h('button.lp-arrow.next', { title: 'Next page' }, icon('chevronRight'));
  // "Add apps to the desktop" (desktop right-click menu) opens the launcher in desktop mode.
  const target = data.target === 'desktop' ? 'desktopIcons' : 'pinned';
  const pinBtn = h('button.pill-btn', target === 'desktopIcons' ? 'Add to Desktop' : 'Pin Apps');
  const hint = h('span.lp-hint', target === 'desktopIcons'
    ? 'Click an app to put it on the desktop or take it off'
    : 'Click an app to add it to the dock or remove it. Right-click any app to add it to the desktop.');
  root.append(
    backdrop,
    h('button.lp-collapse', { title: 'Close', onclick: () => closePopup() }, icon('chevronDown')),
    h('label.lp-search', input, icon('search')),
    h('div.lp-stage', prev, grid, next),
    dots,
    h('div.lp-actions', pinBtn, hint),
  );

  let page = 0;
  let pages = [];
  let pinMode = target === 'desktopIcons'; // desktop mode starts ready to pick apps
  let perPage = 18;
  let cols = 6;

  function layout() {
    const w = grid.clientWidth || root.clientWidth - 200;
    const hgt = grid.clientHeight || root.clientHeight - 300;
    cols = Math.max(3, Math.min(7, Math.floor(w / 150)));
    const rows = Math.max(2, Math.min(5, Math.floor(hgt / 150)));
    perPage = cols * rows;
    grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
    grid.style.gridTemplateRows = `repeat(${rows}, 1fr)`;
  }

  function tile(app) {
    const pinned = store.state.settings[target].includes(app.id);
    const el = h('button.lp-tile', { class: pinMode && pinned ? 'pinned' : '', title: app.description || app.name },
      h('span.lp-icon', h('img', { src: withToken(app.icon), alt: '', draggable: 'false' }),
        pinMode ? h('span.lp-badge', icon(pinned ? 'check' : 'plus')) : null),
      h('span.lp-label', app.name));
    el.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      e.stopPropagation();
      appMenu(app, e.clientX, e.clientY);
    });
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!pinMode) {
        launch(app.id).catch((err) => console.warn(err.message));
        closePopup();
        return;
      }
      const list = store.state.settings[target];
      saveSettings({ [target]: pinned ? list.filter((id) => id !== app.id) : [...list, app.id] })
        .catch((err) => console.warn(err.message));
    });
    return el;
  }

  // Right-click an app: open it, put it on the desktop, or pin it to the dock.
  let menu = null;
  const closeMenu = () => { menu?.remove(); menu = null; };
  function appMenu(app, x, y) {
    closeMenu();
    const { pinned, desktopIcons } = store.state.settings;
    const onDock = pinned.includes(app.id);
    const onDesk = desktopIcons.includes(app.id);
    const save = (patch) => saveSettings(patch).catch((err) => console.warn(err.message));
    const item = (ico, label, fn) => h('button.menu-item', { role: 'menuitem', onclick: (e) => { e.stopPropagation(); closeMenu(); fn(); } }, icon(ico), label);
    menu = h('div.menu.lp-menu', { role: 'menu' },
      item('arrowRight', 'Open', () => { launch(app.id); closePopup(); }),
      item(onDesk ? 'close' : 'monitor', onDesk ? 'Remove from desktop' : 'Add to desktop',
        () => save({ desktopIcons: onDesk ? desktopIcons.filter((id) => id !== app.id) : [...desktopIcons, app.id] })),
      item(onDock ? 'unpin' : 'pin', onDock ? 'Unpin from dock' : 'Pin to dock',
        () => save({ pinned: onDock ? pinned.filter((id) => id !== app.id) : [...pinned, app.id] })));
    root.append(menu);
    const r = menu.getBoundingClientRect();
    menu.style.left = `${Math.min(x, innerWidth - r.width - 8)}px`;
    menu.style.top = `${Math.min(y, innerHeight - r.height - 8)}px`;
  }
  root.addEventListener('pointerdown', (e) => { if (menu && !menu.contains(e.target)) closeMenu(); });

  function render(direction = 0) {
    layout();
    const { settings } = store.state;
    const apps = searchApps(store.state.apps.filter((a) => settings.showAllApps || !a.hidden), input.value);
    pages = [];
    for (let i = 0; i < apps.length; i += perPage) pages.push(apps.slice(i, i + perPage));
    if (!pages.length) pages.push([]);
    page = Math.max(0, Math.min(page, pages.length - 1));
    grid.replaceChildren(...pages[page].map(tile));
    if (!apps.length) grid.append(h('p.lp-empty', `No apps match “${input.value.trim()}”`));
    grid.classList.remove('slide-left', 'slide-right');
    if (direction) {
      void grid.offsetWidth;
      grid.classList.add(direction > 0 ? 'slide-left' : 'slide-right');
    }
    prev.hidden = page === 0;
    next.hidden = page >= pages.length - 1;
    dots.replaceChildren(...(pages.length > 1 ? pages.map((_, i) =>
      h('button.lp-dot', { class: i === page ? 'on' : '', 'aria-label': `Page ${i + 1}`, onclick: (e) => { e.stopPropagation(); go(i); } })) : []));
    pinBtn.textContent = pinMode ? 'Done' : target === 'desktopIcons' ? 'Add to Desktop' : 'Pin Apps';
    pinBtn.classList.toggle('on', pinMode);
    hint.hidden = !pinMode;
  }

  function go(target) {
    const clamped = Math.max(0, Math.min(target, pages.length - 1));
    if (clamped === page) return;
    const dir = clamped > page ? 1 : -1;
    page = clamped;
    render(dir);
  }

  prev.addEventListener('click', (e) => { e.stopPropagation(); go(page - 1); });
  next.addEventListener('click', (e) => { e.stopPropagation(); go(page + 1); });
  pinBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    pinMode = !pinMode;
    render();
  });
  input.addEventListener('input', () => {
    page = 0;
    render();
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && input.value.trim()) {
      const first = pages[page]?.[0];
      if (first) {
        launch(first.id);
        closePopup();
      }
    } else if (e.key === 'Escape' && input.value) {
      e.preventDefault();
      input.value = '';
      render();
    } else if ((e.key === 'ArrowRight' || e.key === 'ArrowLeft') && !input.value) {
      go(page + (e.key === 'ArrowRight' ? 1 : -1));
    }
  });
  // Clicking empty space closes the launcher, like Launchpad.
  root.addEventListener('click', (e) => {
    if (e.target === root || e.target === grid || e.target.classList.contains('lp-stage') || e.target === backdrop) closePopup();
  });
  let wheelLock = 0;
  root.addEventListener('wheel', (e) => {
    const now = Date.now();
    if (now < wheelLock || Math.abs(e.deltaY) + Math.abs(e.deltaX) < 20) return;
    wheelLock = now + 450;
    go(page + ((e.deltaY || e.deltaX) > 0 ? 1 : -1));
  }, { passive: true });
  const onResize = () => render();
  window.addEventListener('resize', onResize);

  const unsubscribe = store.subscribe((_s, changed) => {
    if (changed.has('apps') || changed.has('settings')) render();
  });
  requestAnimationFrame(() => render());
  return () => {
    unsubscribe();
    window.removeEventListener('resize', onResize);
  };
}
