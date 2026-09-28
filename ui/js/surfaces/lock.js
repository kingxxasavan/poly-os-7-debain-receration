// PolyOS lock screen: shown instantly by the shell over everything (no switch to the login
// screen). Unlock checks the password with PAM; "Forgot password?" uses the recovery key.

import { api, on, withToken } from '../api.js';
import { mountAuth } from '../authscreen.js';
import { h } from '../ui.js';

const POWER = { suspend: 'suspend', restart: 'reboot', shutdown: 'poweroff' };

export function mount(root, store) {
  const { user } = store.state;
  mountAuth(root, store, {
    kind: 'lock',
    load: () => api.get('/api/account/pin').catch(() => ({ set: false })).then((pin) => ({
      users: [{ name: user.name, displayName: user.fullName || user.name, pin: !!(pin.set && !pin.blocked) }],
      selectedUser: user.name,
      sessions: [],
      hostname: store.state.hostname,
      can: { suspend: true, restart: true, shutdown: true },
    })),
    signIn: (_user, password) => api.post('/api/lock/unlock', { password }),
    recover: (_user, key, password) => api.post('/api/lock/recover', { key, password }),
    power: (action) => api.post('/api/power', { action: POWER[action] }),
    headline: () => api.get('/api/widgets/news').then((n) => (n.items[0] ? { title: n.items[0].title, source: n.source } : null)),
  });

  // The lock window covers every screen. With more than one, the lock screen itself sits on the
  // main screen and the others show the lock wallpaper, so no screen shows the desktop behind it.
  const others = h('div.gr-others', { 'aria-hidden': 'true' });
  root.after(others);
  const wallpaper = () => `url("${withToken(`/wallpaper/lock?v=${encodeURIComponent(store.state.settings.lockWallpaper)}`)}")`;
  const place = (monitors) => {
    const main = monitors.find((m) => m.primary) || monitors[0];
    const many = monitors.length > 1 && main;
    Object.assign(root.style, many
      ? { left: `${main.x}px`, top: `${main.y}px`, width: `${main.width}px`, height: `${main.height}px`, right: 'auto', bottom: 'auto' }
      : { left: '', top: '', width: '', height: '', right: '', bottom: '' });
    others.replaceChildren(...(many ? monitors.filter((m) => m !== main).map((m) => h('div.gr-other', {
      style: { left: `${m.x}px`, top: `${m.y}px`, width: `${m.width}px`, height: `${m.height}px` },
    }, h('i', { style: { backgroundImage: wallpaper() } }), h('img', { src: '/img/logo-white.svg', alt: '' }))) : []));
  };
  api.get('/api/monitors').then((r) => place(r.monitors || []), () => {});
  on('monitors', (e) => place(e.monitors || []));
}
