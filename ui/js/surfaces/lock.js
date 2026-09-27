// PolyOS lock screen: shown instantly by the shell over everything (no switch to the login
// screen). Unlock checks the password with PAM; "Forgot password?" uses the recovery key.

import { api } from '../api.js';
import { mountAuth } from '../authscreen.js';

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
}
