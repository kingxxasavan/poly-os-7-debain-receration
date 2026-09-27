// PolyMarket: PolyOS's store of trusted apps (Debian packages and Flathub apps from a
// curated catalog). Named after the PolyOS 7 app store.

import { watchJobs, withAdmin } from '../admin.js';
import { api, on, params, withToken } from '../api.js';
import { fill, h, icon } from '../ui.js';

const CATEGORY_ICONS = {
  featured: 'sparkle', browsers: 'globe', social: 'chat', media: 'music', games: 'game',
  productivity: 'briefcase', creativity: 'brush', making: 'cube', developer: 'code', utilities: 'tool', installed: 'check',
};

export function mount(root, store) {
  root.className = 'store';
  document.title = 'PolyMarket';
  const nav = h('nav.nav', { 'aria-label': 'Categories' });
  const search = h('input.st-search-input', { type: 'search', placeholder: 'Search apps', 'aria-label': 'Search apps' });
  const main = h('main.st-main');
  root.append(h('aside.sidebar',
    h('div.brand', h('img', { src: '/img/store.svg', alt: '' }), 'PolyMarket'),
    h('label.st-search', icon('search'), search), nav), main);

  let catalog = null;
  let view = params.get('page') || 'featured';
  let detail = null;
  const jobs = new Map(); // app id -> its job (running, queued or finished)
  let lastError = null;
  const buttons = new Map();

  const iconUrl = (app) => withToken(`/icon/theme/${encodeURIComponent(app.icons.join(','))}`);

  function actionButton(app, big = false) {
    const job = jobs.get(app.id);
    const cls = big ? 'st-btn big' : 'st-btn';
    if (job && job.state === 'running') {
      const pct = Math.round((job.progress || 0) * 100);
      return h('button', { class: `${cls} busy`, disabled: true, style: { '--pct': `${pct}%` } }, `${pct}%`);
    }
    // waiting its turn behind another installation: click to take it out of the queue
    if (job && job.state === 'queued') {
      return h('button', { class: `${cls} queued`, title: 'Waiting for the other installations. Click to cancel.',
        onclick: (e) => { e.stopPropagation(); api.post('/api/jobs/cancel', { id: job.id }).catch((err) => { lastError = err.message; render(); }); } },
      'Queued');
    }
    if (app.installed) {
      return app.desktop.length
        ? h('button', { class: `${cls} open`, onclick: (e) => { e.stopPropagation(); openApp(app); } }, 'Open')
        : h('button', { class: `${cls} done`, disabled: true }, 'Installed');
    }
    return h('button', { class: `${cls} get`, onclick: (e) => { e.stopPropagation(); act(app, 'install'); } }, 'Install');
  }

  async function act(app, action) {
    lastError = null;
    try {
      const job = await withAdmin(() => api.post(`/api/store/${action}`, { id: app.id }),
        { title: action === 'install' ? `Install ${app.name}` : `Remove ${app.name}`, text: 'Enter your password to change the apps on this computer.' });
      jobs.set(app.id, job);
    } catch (err) {
      if (!err.cancelled) lastError = `${app.name}: ${err.message}`;
    }
    render();
  }

  async function openApp(app) {
    try {
      await api.post('/api/store/open', { id: app.id });
    } catch (err) {
      lastError = err.message;
      render();
    }
  }

  function card(app, big = false) {
    return h('article', { class: big ? 'st-card big' : 'st-card', tabindex: '0', onclick: () => showDetail(app),
      onkeydown: (e) => { if (e.key === 'Enter') showDetail(app); } },
    h('img.st-icon', { src: iconUrl(app), alt: '', loading: 'lazy' }),
    h('div.st-card-text', h('b', app.name), h('small', app.summary)),
    actionButton(app));
  }

  function sourceBadge(app) {
    return h('span.st-source', { class: app.source }, app.source === 'debian' ? 'Debian' : 'Flathub');
  }

  function showDetail(app) {
    detail = app.id;
    render();
  }

  function detailView(app) {
    const remove = app.installed && !app.system
      ? h('button.st-btn.remove', { onclick: () => act(app, 'remove'), disabled: ['running', 'queued'].includes(jobs.get(app.id)?.state) }, 'Remove') : null;
    const cat = catalog.categories.find((c) => c[0] === app.category);
    return h('section.st-detail',
      h('button.su-back', { onclick: () => { detail = null; render(); } }, icon('chevronLeft'), 'Back'),
      h('div.st-detail-head',
        h('img.st-icon.huge', { src: iconUrl(app), alt: '' }),
        h('div.st-detail-title', h('h1', app.name), h('p', app.summary),
          h('div.st-meta', sourceBadge(app), cat ? h('span', cat[1]) : null, app.system ? h('span', 'Comes with PolyOS') : null)),
        h('div.st-detail-actions', actionButton(app, true), remove)),
      h('p.st-desc', app.description),
      h('div.st-facts',
        h('div', h('small', 'Source'), h('b', app.source === 'debian' ? 'Debian archive' : 'Flathub (sandboxed app)')),
        h('div', h('small', app.source === 'debian' ? 'Packages' : 'App ID'), h('b', app.source === 'debian' ? app.packages.join(', ') : app.ref)),
        h('div', h('small', 'Opens from'), h('b', app.desktop.length ? 'The Start menu' : 'Terminal'))));
  }

  function section(title, apps, big = false) {
    return h('section.st-section', h('h2', title), h('div', { class: big ? 'st-grid big' : 'st-grid' }, apps.map((a) => card(a, big))));
  }

  function render() {
    if (!catalog) return;
    for (const [id, btn] of buttons) btn.classList.toggle('active', id === view && !search.value.trim());
    const q = search.value.trim().toLowerCase();
    const parts = [];
    if (store.state.env.live) {
      parts.push(h('div.st-note', icon('info'), 'You’re trying PolyOS from the USB drive: apps you add now go away when you restart. Install PolyOS to keep them.'));
    }
    if (lastError) parts.push(h('div.st-note.error', icon('info'), lastError));
    const app = detail && catalog.apps.find((a) => a.id === detail);
    if (app) {
      parts.push(detailView(app));
    } else if (q) {
      const found = catalog.apps.filter((a) => `${a.name} ${a.summary} ${a.category}`.toLowerCase().includes(q));
      parts.push(found.length ? section(`Results for “${search.value.trim()}”`, found) : h('p.st-empty', 'No apps match your search.'));
    } else if (view === 'featured') {
      parts.push(h('div.st-hero',
        h('div', h('small', 'PolyMarket'), h('h1', 'Get more out of PolyOS'),
          h('p', 'Hand-picked apps from Debian and Flathub. Every app here is checked before it’s listed.')),
        h('img', { src: '/img/store.svg', alt: '' })));
      parts.push(section('Popular', catalog.apps.filter((a) => a.featured), true));
      for (const [id, label] of catalog.categories.filter((c) => c[0] !== 'featured')) {
        parts.push(section(label, catalog.apps.filter((a) => a.category === id).slice(0, 4)));
      }
    } else if (view === 'installed') {
      const mine = catalog.apps.filter((a) => a.installed);
      parts.push(mine.length ? section('Installed', mine) : h('p.st-empty', 'Apps you get from PolyMarket show up here.'));
    } else {
      const label = catalog.categories.find((c) => c[0] === view)?.[1] || 'Apps';
      parts.push(section(label, catalog.apps.filter((a) => a.category === view)));
    }
    const scroll = main.scrollTop;
    fill(main, ...parts);
    main.scrollTop = scroll;
  }

  function go(id) {
    view = id;
    detail = null;
    search.value = '';
    main.scrollTop = 0;
    render();
    window.history.replaceState(null, '', `?surface=store&page=${id}`);
  }

  async function load() {
    try {
      catalog = await api.get('/api/store');
    } catch (err) {
      fill(main, h('p.st-empty', err.message));
      return;
    }
    if (!buttons.size) {
      for (const [id, label] of [...catalog.categories, ['installed', 'Installed']]) {
        const btn = h('button.nav-item', { onclick: () => go(id) }, icon(CATEGORY_ICONS[id] || 'apps'), h('span', label));
        buttons.set(id, btn);
        if (id === 'installed') nav.append(h('div.nav-spacer'));
        nav.append(btn);
      }
    }
    render();
  }

  search.addEventListener('input', () => { detail = null; render(); });
  watchJobs((job) => {
    if (job.kind !== 'store' || !job.target) return;
    jobs.set(job.target, job);
    if (job.state === 'failed') lastError = job.error;
    render();
  });
  on('store', load);
  on('navigate', (e) => { if (e.surface === 'store' && e.page) go(e.page); });
  load();
}
