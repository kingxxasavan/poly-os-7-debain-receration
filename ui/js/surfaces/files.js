// Files: the PolyOS file manager (real filesystem through /api/files/*).

import { api, on, params, withToken } from '../api.js';
import { fileSvg, fill, formatBytes, h, icon } from '../ui.js';

const TRASH = 'trash:///';
const IMAGE_EXT = /\.(png|jpe?g|jfif|gif|webp|avif|bmp|svg)$/i;
const PLACE_ICONS = { home: 'home', desktop: 'monitor', documents: 'doc', download: 'download', music: 'music',
  pictures: 'image', videos: 'video', drive: 'drive', usb: 'drive' };

// ---- icons -------------------------------------------------------------------------
function folderSvg() {
  return '<svg viewBox="0 0 64 64" aria-hidden="true"><path d="M6 16a5 5 0 0 1 5-5h14l6 6h22a5 5 0 0 1 5 5v4H6z" fill="#c9922e"/>'
    + '<rect x="6" y="21" width="52" height="33" rx="6" fill="#f0b848"/><rect x="6" y="21" width="52" height="6" rx="3" fill="#ffd27a" opacity=".55"/></svg>';
}

function entryIcon(entry, big) {
  const box = h('span.fx-icon', { class: big ? 'big' : '' });
  if (entry.dir) {
    box.innerHTML = folderSvg();
  } else if (entry.kind === 'image' && IMAGE_EXT.test(entry.name) && entry.size < 40e6) {
    const img = h('img', { src: withToken(`/files/raw?path=${encodeURIComponent(entry.path)}&v=${entry.mtime}`), alt: '', loading: 'lazy', draggable: 'false' });
    img.addEventListener('error', () => { box.innerHTML = fileSvg(entry.kind, entry.name); }, { once: true });
    box.append(img);
    box.classList.add('thumb');
  } else {
    box.innerHTML = fileSvg(entry.kind, entry.name);
  }
  return box;
}

const extraIcons = {
  home: '<path d="M4 11.5 12 5l8 6.5V19a1 1 0 0 1-1 1h-4.5v-5h-5v5H5a1 1 0 0 1-1-1z"/>',
  doc: '<path d="M7 3.5h7l4 4V20a.5.5 0 0 1-.5.5h-10A.5.5 0 0 1 7 20z"/><path d="M13.5 3.5V8h4.5M9.5 12.5h6M9.5 16h6"/>',
  music: '<path d="M9 18V6l10-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="16.5" cy="16" r="2.5"/>',
  video: '<rect x="3" y="6" width="13" height="12" rx="2"/><path d="m16 10.5 5-3v9l-5-3z"/>',
  drive: '<rect x="3" y="13" width="18" height="7" rx="2"/><path d="M5 13 7.5 5h9L19 13"/><path d="M16.5 16.5h.01"/>',
  trash: '<path d="M4 7h16M9.5 7V4.5h5V7M6 7l1 13h10l1-13"/><path d="M10 11v5.5M14 11v5.5"/>',
  back: '<path d="m14.5 6-6 6 6 6"/>',
  forward: '<path d="m9.5 6 6 6-6 6"/>',
  up: '<path d="M12 19V6M6 12l6-6 6 6"/>',
  list: '<path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01"/>',
  more: '<circle cx="5" cy="12" r="1.3" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.3" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.3" fill="currentColor" stroke="none"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
  cut: '<circle cx="6" cy="17" r="2.6"/><circle cx="18" cy="17" r="2.6"/><path d="M7.8 15 17 4M16.2 15 7 4"/>',
  paste: '<rect x="6" y="5" width="12" height="16" rx="2"/><path d="M9 5V3.5h6V5"/>',
  rename: '<path d="M4 20h4l10.5-10.5a2.8 2.8 0 0 0-4-4L4 16z"/>',
  newFolder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 10.5v5M9.5 13h5"/>',
  newFile: '<path d="M7 3.5h7l4 4V20a.5.5 0 0 1-.5.5h-10A.5.5 0 0 1 7 20z"/><path d="M12 11v6M9 14h6"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
  restore: '<path d="M4 12a8 8 0 1 0 2.34-5.66"/><path d="M4 4v4.5h4.5"/>',
  props: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.8v.01"/>',
};
function ico(name) {
  if (!extraIcons[name]) return icon(name);
  const span = document.createElement('span');
  span.className = 'ico';
  span.innerHTML = `<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${extraIcons[name]}</svg>`;
  return span;
}

// ---- persistent view prefs + a clipboard shared by every Files window --------------------
const store = {
  get(key, fallback) {
    try {
      const v = localStorage.getItem(`polyos.files.${key}`);
      return v == null ? fallback : JSON.parse(v);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try { localStorage.setItem(`polyos.files.${key}`, JSON.stringify(value)); } catch { /* storage unavailable */ }
  },
};

const fmtDate = (sec) => new Date(sec * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
const baseName = (p) => (p === '/' ? 'Computer' : p.replace(/\/+$/, '').split('/').pop() || p);

export function mount(root) {
  root.className = 'files';
  const sidebar = h('aside.fx-side');
  const crumbs = h('div.fx-crumbs');
  const pathInput = h('input.fx-path-input', { spellcheck: 'false', 'aria-label': 'Folder path', hidden: true });
  const search = h('input.fx-search-input', { type: 'search', placeholder: 'Search this folder', spellcheck: 'false', 'aria-label': 'Search' });
  const btnBack = h('button.fx-round', { title: 'Back (Alt+Left)' }, ico('back'));
  const btnFwd = h('button.fx-round', { title: 'Forward (Alt+Right)' }, ico('forward'));
  const btnUp = h('button.fx-round', { title: 'Enclosing folder (Alt+Up)' }, ico('up'));
  const btnView = h('button.fx-round', { title: 'Switch view' });
  const btnNew = h('button.fx-round', { title: 'New folder (Ctrl+Shift+N)' }, ico('newFolder'));
  const btnMore = h('button.fx-round', { title: 'More' }, ico('more'));
  const trashBar = h('div.fx-trashbar', { hidden: true });
  const content = h('div.fx-content', { tabindex: '0' });
  const status = h('div.fx-status');
  const toast = h('div.fx-toast', { hidden: true });
  const panel = h('aside.fx-panel', { hidden: true });
  root.append(
    sidebar,
    h('section.fx-main',
      h('header.fx-bar', btnBack, btnFwd, btnUp,
        h('div.fx-path', crumbs, pathInput),
        h('label.fx-search', icon('search'), search),
        btnView, btnNew, btnMore),
      trashBar,
      h('div.fx-body', content, panel),
      status),
    toast,
  );

  // ---- state ----------------------------------------------------------------------
  let places = { places: [], devices: [], home: '/' };
  let listing = null;
  let current = params.get('path') || null;
  let entries = [];
  let selected = new Set();
  let anchor = -1;
  let history = [];
  let future = [];
  let view = store.get('view', 'grid');
  let sort = store.get('sort', { by: 'name', desc: false });
  let hidden = store.get('hidden', false);
  let query = '';
  let renaming = null;
  let menu = null;

  // ---- data -------------------------------------------------------------------------
  const inTrash = () => current === TRASH;

  async function load({ keepSelection = false } = {}) {
    try {
      listing = query
        ? await api.get(`/api/files/search?path=${encodeURIComponent(current)}&q=${encodeURIComponent(query)}&hidden=${hidden ? 1 : 0}`)
        : await api.get(`/api/files/list?path=${encodeURIComponent(current || '')}&hidden=${hidden ? 1 : 0}`);
      if (!current) current = listing.path;
      entries = sortEntries(listing.entries);
      if (!keepSelection) selected = new Set();
      else selected = new Set([...selected].filter((p) => entries.some((e) => e.path === p)));
      render();
    } catch (err) {
      showToast(err.message);
      if (!listing) {
        current = places.home;
        if (current) load();
      }
    }
  }

  async function loadPlaces() {
    try {
      places = await api.get('/api/files/places');
      if (!current) current = places.home;
      renderSidebar();
    } catch (err) {
      showToast(err.message);
    }
  }

  function sortEntries(list) {
    const dir = sort.desc ? -1 : 1;
    const cmp = {
      name: (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }),
      mtime: (a, b) => a.mtime - b.mtime,
      size: (a, b) => a.size - b.size,
      kind: (a, b) => a.kind.localeCompare(b.kind) || a.name.localeCompare(b.name),
    }[sort.by];
    return [...list].sort((a, b) => (b.dir - a.dir) || dir * cmp(a, b));
  }

  function go(path, { push = true } = {}) {
    if (!path || path === current) return;
    if (push && current) {
      history.push(current);
      future = [];
    }
    current = path;
    query = '';
    search.value = '';
    closePanel();
    load();
  }

  // ---- rendering ----------------------------------------------------------------------
  function renderSidebar() {
    const item = (p, label, iconName, extra) => {
      const btn = h('button.fx-place', { class: current === p ? 'active' : '', title: p === TRASH ? 'Trash' : p },
        ico(iconName), h('span', label), extra || null);
      btn.addEventListener('click', () => go(p));
      dropTarget(btn, p);
      return btn;
    };
    sidebar.replaceChildren(...[
      h('div.fx-brand', h('span.fx-brand-icon', { html: folderSvg() }), 'Files'),
      h('div.fx-group', h('div.fx-group-title', 'Places'),
        ...places.places.map((pl) => item(pl.path, pl.name, PLACE_ICONS[pl.icon] || 'folder'))),
      places.devices.length
        ? h('div.fx-group', h('div.fx-group-title', 'Devices'),
          ...places.devices.map((d) => item(d.path, d.name, PLACE_ICONS[d.icon] || 'drive')))
        : null,
      h('div.fx-spacer'),
      item(TRASH, 'Trash', 'trash', places.trashCount ? h('span.fx-badge', String(places.trashCount)) : null),
    ].filter(Boolean)); // native replaceChildren would print "null"
  }

  function renderCrumbs() {
    crumbs.replaceChildren();
    if (inTrash()) {
      crumbs.append(h('span.fx-crumb.static', ico('trash'), 'Trash'));
      return;
    }
    if (query) {
      crumbs.append(h('span.fx-crumb.static', icon('search'), `Results for “${query}” in ${baseName(current)}`));
      return;
    }
    const home = places.home;
    let parts;
    if (current === home || current.startsWith(`${home}/`)) {
      parts = [[home, 'Home'], ...current.slice(home.length).split('/').filter(Boolean)
        .map((seg, i, arr) => [`${home}/${arr.slice(0, i + 1).join('/')}`, seg])];
    } else {
      const drive = current.match(/^[A-Za-z]:\//)?.[0]; // Windows paths only appear in the dev mock
      const root = drive || '/';
      const segs = current.slice(root.length).split('/').filter(Boolean);
      parts = [[root, drive ? drive.slice(0, 2) : 'Computer'], ...segs.map((seg, i) => [`${root}${segs.slice(0, i + 1).join('/')}`, seg])];
    }
    parts.forEach(([p, label], i) => {
      if (i) crumbs.append(h('span.fx-sep', icon('chevronRight')));
      const b = h('button.fx-crumb', { class: i === parts.length - 1 ? 'last' : '' }, label);
      b.addEventListener('click', (e) => { e.stopPropagation(); go(p); });
      crumbs.append(b);
    });
  }

  function renderTrashBar() {
    trashBar.hidden = !inTrash();
    if (!inTrash()) return;
    const count = entries.length;
    fill(trashBar,
      h('span', count ? `${count} item${count === 1 ? '' : 's'} in the Trash` : 'The Trash is empty'),
      h('button.btn', { disabled: !selected.size, onclick: restoreSelected }, ico('restore'), 'Restore'),
      h('button.btn.danger', { disabled: !count, onclick: confirmEmpty }, ico('trash'), 'Empty Trash'),
    );
  }

  function render() {
    document.title = inTrash() ? 'Trash' : query ? `Search – ${baseName(current)}` : baseName(current);
    renderSidebar();
    renderCrumbs();
    renderTrashBar();
    btnBack.disabled = !history.length;
    btnFwd.disabled = !future.length;
    btnUp.disabled = inTrash() || !listing?.parent || !!query;
    btnNew.disabled = inTrash() || !!query || !listing?.writable;
    btnView.replaceChildren(ico(view === 'grid' ? 'list' : 'grid'));
    content.className = `fx-content ${view}`;
    content.replaceChildren();
    if (!entries.length) {
      content.append(h('div.fx-empty', h('span.fx-empty-icon', { html: folderSvg() }),
        h('b', inTrash() ? 'The Trash is empty' : query ? 'Nothing matches your search' : 'This folder is empty'),
        !inTrash() && !query && listing?.writable ? h('small', 'Right-click to create a folder or file') : null));
    } else if (view === 'list') {
      content.append(listHeader(), ...entries.map(listRow));
    } else {
      content.append(...entries.map(gridItem));
    }
    renderStatus();
  }

  function listHeader() {
    const col = (by, label) => {
      const b = h('button.fx-col', { class: sort.by === by ? 'on' : '' }, label, sort.by === by ? (sort.desc ? ' ↓' : ' ↑') : '');
      b.addEventListener('click', () => setSort(by));
      return b;
    };
    return inTrash()
      ? h('div.fx-row.fx-head', col('name', 'Name'), h('span.fx-col', 'Original location'), col('mtime', 'Deleted'), col('size', 'Size'))
      : h('div.fx-row.fx-head', col('name', 'Name'), col('mtime', 'Modified'), col('size', 'Size'), col('kind', 'Kind'));
  }

  function bindItem(el, entry, index) {
    el.dataset.path = entry.path;
    if (!inTrash() && renaming !== entry.path) {
      el.draggable = true;
      el.addEventListener('dragstart', (e) => dragStart(e, entry));
      if (entry.dir) dropTarget(el, entry.path);
    }
    el.classList.toggle('selected', selected.has(entry.path));
    el.classList.toggle('dim', entry.hidden || cutPaths().has(entry.path));
    el.addEventListener('click', (e) => { e.stopPropagation(); select(index, e); });
    el.addEventListener('dblclick', () => openEntry(entry));
    el.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (!selected.has(entry.path)) select(index, {});
      itemMenu(e);
    });
    return el;
  }

  function nameNode(entry) {
    if (renaming === entry.path) {
      const input = h('input.fx-rename', { value: entry.name, spellcheck: 'false', 'aria-label': 'New name' });
      let done = false;
      const finish = async (commit) => {
        if (done) return;
        done = true;
        renaming = null;
        if (commit && input.value.trim() && input.value !== entry.name) {
          try {
            const res = await api.post('/api/files/rename', { path: entry.path, name: input.value.trim() });
            selected = new Set([res.path]);
          } catch (err) {
            showToast(err.message);
          }
        }
        load({ keepSelection: true });
      };
      input.addEventListener('keydown', (e) => {
        e.stopPropagation();
        if (e.key === 'Enter') finish(true);
        if (e.key === 'Escape') finish(false);
      });
      input.addEventListener('blur', () => finish(true));
      input.addEventListener('click', (e) => e.stopPropagation());
      requestAnimationFrame(() => {
        input.focus();
        const dot = entry.dir ? -1 : entry.name.lastIndexOf('.');
        input.setSelectionRange(0, dot > 0 ? dot : entry.name.length);
      });
      return input;
    }
    return h('span.fx-name', { title: entry.name }, entry.name);
  }

  function gridItem(entry, i) {
    return bindItem(h('div.fx-item', { role: 'option' }, entryIcon(entry, true), nameNode(entry)), entry, i);
  }

  function listRow(entry, i) {
    const kind = entry.dir ? 'Folder' : (entry.kind === 'file' ? (entry.name.split('.').pop() || 'File').toUpperCase() : entry.kind[0].toUpperCase() + entry.kind.slice(1));
    const cells = inTrash()
      ? [h('span.fx-cell.muted', entry.origin ? entry.origin.replace(/\/[^/]*$/, '') || '/' : '—'),
        h('span.fx-cell.muted', entry.deleted ? new Date(entry.deleted).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '—'),
        h('span.fx-cell.muted', entry.dir ? '—' : formatBytes(entry.size))]
      : [h('span.fx-cell.muted', fmtDate(entry.mtime)),
        h('span.fx-cell.muted', entry.dir ? '—' : formatBytes(entry.size)),
        h('span.fx-cell.muted', kind)];
    return bindItem(h('div.fx-row', { role: 'option' }, h('span.fx-cell.fx-namecell', entryIcon(entry, false), nameNode(entry)), ...cells), entry, i);
  }

  function renderStatus() {
    const sel = entries.filter((e) => selected.has(e.path));
    const size = sel.reduce((n, e) => n + (e.dir ? 0 : e.size), 0);
    const parts = [`${entries.length} item${entries.length === 1 ? '' : 's'}`];
    if (sel.length) parts.push(`${sel.length} selected${size ? ` (${formatBytes(size)})` : ''}`);
    if (listing?.truncated) parts.push('showing the first 300 matches');
    if (listing && !listing.writable && !inTrash() && !query) parts.push('read-only');
    status.textContent = parts.join(' · ');
    for (const el of content.querySelectorAll('[data-path]')) el.classList.toggle('selected', selected.has(el.dataset.path));
    renderTrashBar();
  }

  // ---- selection & actions -------------------------------------------------------------
  function select(index, e) {
    const path = entries[index].path;
    if (e.shiftKey && anchor >= 0) {
      const [a, b] = [Math.min(anchor, index), Math.max(anchor, index)];
      if (!e.ctrlKey) selected = new Set();
      for (let i = a; i <= b; i += 1) selected.add(entries[i].path);
    } else if (e.ctrlKey || e.metaKey) {
      if (selected.has(path)) selected.delete(path);
      else selected.add(path);
      anchor = index;
    } else {
      selected = new Set([path]);
      anchor = index;
    }
    renderStatus();
  }

  const selection = () => entries.filter((e) => selected.has(e.path));

  async function openEntry(entry) {
    if (inTrash()) return showProps(entry);
    if (entry.dir) return go(entry.path);
    try {
      await api.post('/api/files/open', { path: entry.path });
    } catch (err) {
      showToast(err.message);
    }
  }

  async function create(kind) {
    if (inTrash() || query) return;
    try {
      const res = await api.post(kind === 'folder' ? '/api/files/mkdir' : '/api/files/new-file',
        { parent: current, name: kind === 'folder' ? 'New Folder' : 'New File.txt' });
      renaming = res.path;
      selected = new Set([res.path]);
      await load({ keepSelection: true });
    } catch (err) {
      showToast(err.message);
    }
  }

  const cutPaths = () => {
    const clip = store.get('clipboard', null);
    return new Set(clip && clip.op === 'cut' ? clip.paths : []);
  };

  function setClipboard(op) {
    const paths = selection().map((e) => e.path);
    if (!paths.length || inTrash()) return;
    store.set('clipboard', { op, paths });
    showToast(`${op === 'cut' ? 'Cut' : 'Copied'} ${paths.length} item${paths.length === 1 ? '' : 's'}`);
    render();
  }

  async function paste() {
    const clip = store.get('clipboard', null);
    if (!clip || !clip.paths?.length || inTrash() || query) return;
    try {
      const res = await api.post(clip.op === 'cut' ? '/api/files/move' : '/api/files/copy', { sources: clip.paths, dest: current });
      if (clip.op === 'cut') store.set('clipboard', null);
      selected = new Set(res.entries.map((e) => e.path));
      await load({ keepSelection: true });
    } catch (err) {
      showToast(err.message);
    }
  }

  // ---- drag and drop: move (Ctrl copies), onto folders, the places on the left, or the Trash ----
  const DRAG_TYPE = 'application/x-polyos-paths';
  function dragStart(e, entry) {
    if (!selected.has(entry.path)) { selected = new Set([entry.path]); render(); }
    const paths = selection().map((x) => x.path);
    e.dataTransfer.effectAllowed = 'copyMove';
    e.dataTransfer.setData(DRAG_TYPE, JSON.stringify(paths));
    // other windows and apps understand plain file links
    e.dataTransfer.setData('text/uri-list', paths.map((pth) => `file://${encodeURI(pth)}`).join('\r\n'));
  }
  function droppedPaths(e) {
    const own = e.dataTransfer.getData(DRAG_TYPE);
    if (own) { try { return JSON.parse(own); } catch { return []; } }
    return (e.dataTransfer.getData('text/uri-list') || '').split(/\r?\n/)
      .filter((u) => u.startsWith('file://')).map((u) => decodeURI(u.slice(7)));
  }
  function dropTarget(el, dest) {
    el.addEventListener('dragover', (e) => {
      if (![...e.dataTransfer.types].some((t) => t === DRAG_TYPE || t === 'text/uri-list')) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = dest === TRASH ? 'move' : e.ctrlKey ? 'copy' : 'move';
      el.classList.add('drop-target');
    });
    el.addEventListener('dragleave', () => el.classList.remove('drop-target'));
    el.addEventListener('drop', async (e) => {
      e.preventDefault();
      e.stopPropagation();
      el.classList.remove('drop-target');
      const sources = droppedPaths(e).filter((pth) => pth !== dest && !dest.startsWith(`${pth}/`));
      if (!sources.length) return;
      try {
        if (dest === TRASH) {
          await api.post('/api/files/trash', { paths: sources });
          showToast(`Moved ${sources.length} item${sources.length === 1 ? '' : 's'} to the Trash`);
        } else {
          const copy = e.ctrlKey;
          await api.post(copy ? '/api/files/copy' : '/api/files/move', { sources, dest });
          showToast(`${copy ? 'Copied' : 'Moved'} ${sources.length} item${sources.length === 1 ? '' : 's'}`);
        }
        await Promise.all([load(), loadPlaces()]);
      } catch (err) {
        showToast(err.message);
      }
    });
  }

  async function trashSelected() {
    const items = selection();
    if (!items.length || inTrash()) return;
    try {
      await api.post('/api/files/trash', { paths: items.map((e) => e.path) });
      showToast(`Moved ${items.length} item${items.length === 1 ? '' : 's'} to the Trash`);
      await Promise.all([load(), loadPlaces()]);
    } catch (err) {
      showToast(err.message);
    }
  }

  async function restoreSelected() {
    const names = selection().map((e) => e.trashName).filter(Boolean);
    if (!names.length) return;
    try {
      await api.post('/api/files/restore', { names });
      showToast(`Restored ${names.length} item${names.length === 1 ? '' : 's'}`);
      await Promise.all([load(), loadPlaces()]);
    } catch (err) {
      showToast(err.message);
    }
  }

  function confirmEmpty() {
    const dialog = h('div.fx-dialog-backdrop',
      h('div.fx-dialog',
        h('b', 'Empty the Trash?'),
        h('p', `All ${entries.length} item${entries.length === 1 ? '' : 's'} in the Trash will be permanently deleted. This can't be undone.`),
        h('div.fx-dialog-actions',
          h('button.btn', { onclick: () => dialog.remove() }, 'Cancel'),
          h('button.btn.danger.solid', {
            onclick: async () => {
              dialog.remove();
              try {
                await api.post('/api/files/empty-trash', {});
                await Promise.all([load(), loadPlaces()]);
              } catch (err) {
                showToast(err.message);
              }
            },
          }, 'Empty Trash'))));
    root.append(dialog);
  }

  async function showProps(entry) {
    panel.hidden = false;
    panel.replaceChildren(h('div.fx-panel-head', entryIcon(entry, true), h('b', entry.name)), h('p.muted', 'Loading…'));
    try {
      const info = inTrash() ? entry : await api.get(`/api/files/info?path=${encodeURIComponent(entry.path)}`);
      const row = (k, v) => h('div.fx-prop', h('span', k), h('span', v));
      fill(panel,
        h('button.fx-round.fx-panel-close', { title: 'Close', onclick: closePanel }, icon('close')),
        h('div.fx-panel-head', entryIcon(entry, true), h('b', entry.name)),
        row('Kind', entry.dir ? 'Folder' : `${entry.kind} (${entry.mime})`),
        row('Size', entry.dir ? `${formatBytes(info.size || 0)} · ${info.files ?? '?'} files` : formatBytes(info.size)),
        row(inTrash() ? 'Original location' : 'Location', inTrash() ? (entry.origin || '—') : info.location),
        row(inTrash() ? 'Deleted' : 'Modified', inTrash() ? (entry.deleted || '—') : fmtDate(info.mtime)),
        info.mode ? row('Permissions', `${info.mode}${info.writable ? '' : ' (read-only)'}`) : null,
      );
    } catch (err) {
      panel.replaceChildren(h('p.error-text', err.message));
    }
  }

  function closePanel() {
    panel.hidden = true;
  }

  // ---- menus ------------------------------------------------------------------------
  function openMenu(e, items) {
    closeMenu();
    menu = h('div.menu.fx-menu', { role: 'menu' }, items.filter(Boolean).map((it) => (it === '-'
      ? h('div.menu-sep')
      : h('button.menu-item', { class: it.danger ? 'danger' : '', disabled: it.disabled, onclick: () => { closeMenu(); it.run(); } },
        ico(it.icon), it.label, it.hint ? h('span.menu-hint', it.hint) : null))));
    root.append(menu);
    const r = menu.getBoundingClientRect();
    menu.style.left = `${Math.min(e.clientX, innerWidth - r.width - 8)}px`;
    menu.style.top = `${Math.min(e.clientY, innerHeight - r.height - 8)}px`;
  }

  async function setWallpaper(path, key) {
    try {
      await api.post('/api/settings', { [key]: path });
      showToast(key === 'wallpaper' ? 'Wallpaper set' : 'Lock screen picture set');
    } catch (err) {
      showToast('That picture can’t be used. Try a JPG, PNG or WebP image.');
    }
  }

  function closeMenu() {
    menu?.remove();
    menu = null;
  }

  function itemMenu(e) {
    const items = selection();
    const one = items.length === 1 ? items[0] : null;
    if (inTrash()) {
      openMenu(e, [
        { icon: 'restore', label: 'Restore', run: restoreSelected },
        one && { icon: 'props', label: 'Properties', run: () => showProps(one) },
      ]);
      return;
    }
    openMenu(e, [
      one && { icon: 'external', label: one.dir ? 'Open' : 'Open with default app', run: () => openEntry(one) },
      one?.dir && { icon: 'terminal', label: 'Open in Terminal', run: () => api.post('/api/files/terminal', { path: one.path }).catch((err) => showToast(err.message)) },
      '-',
      { icon: 'cut', label: 'Cut', hint: 'Ctrl+X', run: () => setClipboard('cut') },
      { icon: 'copy', label: 'Copy', hint: 'Ctrl+C', run: () => setClipboard('copy') },
      one && { icon: 'rename', label: 'Rename', hint: 'F2', run: () => { renaming = one.path; render(); } },
      one && { icon: 'copy', label: 'Copy path', run: () => copyText(one.path) },
      one?.kind === 'image' && '-',
      one?.kind === 'image' && { icon: 'image', label: 'Set as wallpaper', run: () => setWallpaper(one.path, 'wallpaper') },
      one?.kind === 'image' && { icon: 'lock', label: 'Set as lock screen', run: () => setWallpaper(one.path, 'lockWallpaper') },
      '-',
      { icon: 'trash', label: 'Move to Trash', hint: 'Del', danger: true, run: trashSelected },
      one && '-',
      one && { icon: 'props', label: 'Properties', run: () => showProps(one) },
    ]);
  }

  function backgroundMenu(e) {
    if (inTrash()) return;
    const canWrite = listing?.writable && !query;
    const clip = store.get('clipboard', null);
    openMenu(e, [
      { icon: 'newFolder', label: 'New Folder', disabled: !canWrite, run: () => create('folder') },
      { icon: 'newFile', label: 'New Text File', disabled: !canWrite, run: () => create('file') },
      { icon: 'paste', label: 'Paste', hint: 'Ctrl+V', disabled: !canWrite || !clip?.paths?.length, run: paste },
      '-',
      { icon: 'terminal', label: 'Open in Terminal', run: () => api.post('/api/files/terminal', { path: current }).catch((err) => showToast(err.message)) },
      { icon: 'eye', label: hidden ? 'Hide hidden files' : 'Show hidden files', hint: 'Ctrl+H', run: toggleHidden },
      { icon: 'refresh', label: 'Refresh', hint: 'F5', run: () => load({ keepSelection: true }) },
    ]);
  }

  function moreMenu() {
    const r = btnMore.getBoundingClientRect();
    const sortItem = (by, label) => ({ icon: sort.by === by ? 'check' : 'minimize', label: `Sort by ${label}`, run: () => setSort(by) });
    openMenu({ clientX: r.right - 230, clientY: r.bottom + 6 }, [
      sortItem('name', 'name'), sortItem('mtime', 'date modified'), sortItem('size', 'size'), sortItem('kind', 'kind'),
      { icon: sort.desc ? 'check' : 'minimize', label: 'Reverse order', run: () => { sort.desc = !sort.desc; store.set('sort', sort); entries = sortEntries(entries); render(); } },
      '-',
      { icon: 'eye', label: hidden ? 'Hide hidden files' : 'Show hidden files', hint: 'Ctrl+H', run: toggleHidden },
      { icon: 'newFile', label: 'New Text File', disabled: inTrash() || !listing?.writable, run: () => create('file') },
    ]);
  }

  function setSort(by) {
    sort = { by, desc: sort.by === by ? !sort.desc : by !== 'name' };
    store.set('sort', sort);
    entries = sortEntries(entries);
    render();
  }

  function toggleHidden() {
    hidden = !hidden;
    store.set('hidden', hidden);
    load({ keepSelection: true });
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      showToast('Path copied');
    } catch {
      showToast(text);
    }
  }

  let toastTimer = 0;
  function showToast(message) {
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 3200);
  }

  // ---- wiring -----------------------------------------------------------------------
  btnBack.addEventListener('click', () => { if (history.length) { future.push(current); go(history.pop(), { push: false }); } });
  btnFwd.addEventListener('click', () => { if (future.length) { history.push(current); go(future.pop(), { push: false }); } });
  btnUp.addEventListener('click', () => listing?.parent && go(listing.parent));
  btnView.addEventListener('click', () => { view = view === 'grid' ? 'list' : 'grid'; store.set('view', view); render(); });
  btnNew.addEventListener('click', () => create('folder'));
  btnMore.addEventListener('click', (e) => { e.stopPropagation(); moreMenu(); });

  // click the empty part of the path bar to type a path
  const pathBox = crumbs.parentElement;
  pathBox.addEventListener('click', () => {
    if (inTrash() || query) return;
    crumbs.hidden = true;
    pathInput.hidden = false;
    pathInput.value = current;
    pathInput.focus();
    pathInput.select();
  });
  const endPathEdit = () => { crumbs.hidden = false; pathInput.hidden = true; };
  pathInput.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') { const v = pathInput.value.trim(); endPathEdit(); if (v) go(v.replace(/^~(?=\/|$)/, places.home)); }
    if (e.key === 'Escape') endPathEdit();
  });
  pathInput.addEventListener('blur', endPathEdit);

  let searchTimer = 0;
  search.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      if (inTrash()) return;
      query = search.value.trim();
      load();
    }, 280);
  });
  search.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Escape') { search.value = ''; query = ''; load(); content.focus(); }
  });

  content.addEventListener('click', () => { selected = new Set(); renderStatus(); });
  content.addEventListener('dragover', (e) => {
    if (inTrash() || query || !listing?.writable) return;
    if ([...e.dataTransfer.types].includes('text/uri-list')) e.preventDefault();
  });
  content.addEventListener('drop', async (e) => {
    if (inTrash() || query || !listing?.writable || e.defaultPrevented) return;
    e.preventDefault();
    const sources = droppedPaths(e).filter((pth) => !pth.startsWith(`${current}/`) || pth.slice(current.length + 1).includes('/'));
    if (!sources.length) return;
    try {
      await api.post(e.ctrlKey ? '/api/files/copy' : '/api/files/move', { sources, dest: current });
      await load();
    } catch (err) { showToast(err.message); }
  });
  content.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    selected = new Set();
    renderStatus();
    backgroundMenu(e);
  });
  document.addEventListener('pointerdown', (e) => { if (menu && !menu.contains(e.target)) closeMenu(); });
  window.addEventListener('blur', closeMenu);

  document.addEventListener('keydown', (e) => {
    if (e.target instanceof Element && e.target.closest('input, textarea')) return;
    const ctrl = e.ctrlKey || e.metaKey;
    const items = selection();
    if (e.key === 'Escape') { closeMenu(); closePanel(); selected = new Set(); renderStatus(); return; }
    if (e.altKey && e.key === 'ArrowLeft') return btnBack.click();
    if (e.altKey && e.key === 'ArrowRight') return btnFwd.click();
    if ((e.altKey && e.key === 'ArrowUp') || e.key === 'Backspace') return btnUp.click();
    if (e.key === 'F5') { e.preventDefault(); return load({ keepSelection: true }); }
    if (e.key === 'Enter' && items.length === 1) return openEntry(items[0]);
    if (e.key === 'F2' && items.length === 1 && !inTrash()) { renaming = items[0].path; return render(); }
    if (e.key === 'Delete') return inTrash() ? restoreSelected() : trashSelected();
    if (ctrl && e.key.toLowerCase() === 'a') { e.preventDefault(); selected = new Set(entries.map((x) => x.path)); return renderStatus(); }
    if (ctrl && e.key.toLowerCase() === 'c') return setClipboard('copy');
    if (ctrl && e.key.toLowerCase() === 'x') return setClipboard('cut');
    if (ctrl && e.key.toLowerCase() === 'v') return paste();
    if (ctrl && e.key.toLowerCase() === 'h') { e.preventDefault(); return toggleHidden(); }
    if (ctrl && e.key.toLowerCase() === 'f') { e.preventDefault(); return search.focus(); }
    if (ctrl && e.shiftKey && e.key.toLowerCase() === 'n') { e.preventDefault(); return create('folder'); }
    if (['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp'].includes(e.key) && entries.length) {
      e.preventDefault();
      const cols = view === 'grid' ? Math.max(1, Math.floor(content.clientWidth / 112)) : 1;
      const step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: cols, ArrowUp: -cols }[e.key];
      const at = Math.max(0, Math.min(entries.length - 1, (anchor < 0 ? -step : anchor) + step));
      select(at, e);
      content.querySelector(`[data-path="${CSS.escape(entries[at].path)}"]`)?.scrollIntoView({ block: 'nearest' });
    }
  });

  // Refresh when another window (or this one) changes the folder on show, or on focus.
  on('files', (e) => {
    if (e.folders.includes(current)) load({ keepSelection: true });
    if (e.folders.includes(TRASH)) loadPlaces();
  });
  window.addEventListener('focus', () => { if (!renaming) load({ keepSelection: true }); });

  loadPlaces().then(() => load());
}
