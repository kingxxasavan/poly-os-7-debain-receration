// The taskbar's ^ (like Windows): the apps running now, and the ones running in the background
// without a window (Steam, Discord...). Click one to bring it up; x closes it.

import { api, closePopup, launch, openSettings, windowAction, withToken } from '../api.js';
import { h, icon } from '../ui.js';

export default function tray(root, store, data = {}) {
  root.classList.add('tray-flyout');
  let background = data.background || [];
  const list = h('div.tf-list');
  root.append(
    h('div.tf-head', h('b', 'Running apps')),
    list,
    h('div.tf-foot', h('button.link-btn', { onclick: () => { api.post('/api/open', { app: 'taskmgr' }); closePopup(); } },
      icon('activity'), 'Task Manager')),
  );

  // running apps: one row per app, with all its windows
  function groups() {
    const apps = new Map(store.state.apps.map((a) => [a.id, a]));
    const out = new Map();
    for (const w of store.state.windows) {
      const key = w.appId || `x${w.xid}`;
      if (!out.has(key)) out.set(key, { key, app: apps.get(w.appId), windows: [] });
      out.get(key).windows.push(w);
    }
    return [...out.values()];
  }

  const row = (img, name, sub, onOpen, onClose, closeTitle) => h('div.tf-row',
    h('button.tf-open', { onclick: onOpen, title: name },
      h('img', { src: withToken(img), alt: '', draggable: 'false' }),
      h('span.tf-text', h('b', name), h('small', sub))),
    h('button.icon-btn.tf-close', { title: closeTitle, 'aria-label': closeTitle, onclick: onClose }, icon('close')));

  function render() {
    const running = groups().map((g) => {
      const name = g.app ? g.app.name : g.windows[0].title;
      const sub = g.windows.length > 1 ? `${g.windows.length} windows` : g.windows[0].title;
      return row(g.app ? g.app.icon : g.windows[0].icon, name, sub, () => {
        const w = g.windows.find((x) => x.active) || g.windows[0];
        windowAction(w.xid, 'activate');
        closePopup();
      }, () => g.windows.forEach((w) => windowAction(w.xid, 'close')), `Close ${name}`);
    });
    const quiet = background.map((b) => row(b.icon, b.name, 'Running in the background', () => {
      launch(b.appId).catch(() => {});
      closePopup();
    }, async () => {
      try { await api.post('/api/background-apps/end', { app: b.appId }); } catch { /* gone already */ }
      background = background.filter((x) => x.appId !== b.appId);
      render();
    }, `Quit ${b.name}`));
    list.replaceChildren(
      ...running,
      ...(quiet.length ? [h('div.tf-caption', 'In the background'), ...quiet] : []),
      ...(running.length || quiet.length ? [] : [h('p.tf-empty', 'No apps are running.')]),
    );
  }

  const unsubscribe = store.subscribe((_s, changed) => { if (changed.has('windows') || changed.has('apps')) render(); });
  render();
  return unsubscribe;
}
