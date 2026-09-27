// Poly Account API tests, on PGlite (Postgres in this process): npm test
import assert from 'node:assert/strict';
import { before, test } from 'node:test';
import { handle } from '../app.js';
import { outbox } from '../email.js';

const RELEASE = {
  tag_name: 'v9.1.0', name: 'PolyOS v9.1.0', body: 'Faster startup.', published_at: '2026-10-01T00:00:00Z', prerelease: false, draft: false,
  assets: [
    { name: 'polyos-amd64.iso', browser_download_url: 'https://example.test/polyos-amd64.iso', size: 2e9 },
    { name: 'polyos-update.json', browser_download_url: 'https://example.test/polyos-update.json', size: 500 },
    { name: 'polyos-update.json.sig', browser_download_url: 'https://example.test/polyos-update.json.sig', size: 64 },
    { name: 'polyos-shell_9.1.0_all.deb', browser_download_url: 'https://example.test/shell.deb', size: 3e6 },
  ],
};

before(() => {
  globalThis.fetch = async (url) => {
    if (String(url).includes('/releases/latest')) return new Response(JSON.stringify(RELEASE));
    if (String(url).includes('/releases?')) return new Response(JSON.stringify([{ ...RELEASE, tag_name: 'v9.2.0-beta', prerelease: true }, RELEASE]));
    return new Response('{}', { status: 404 });
  };
});

// A tiny browser: keeps the session cookie, sends X-Poly on changes like the site's own pages.
function client({ csrf = true, auth, ip = '203.0.113.9' } = {}) {
  let cookie = '';
  return async function call(method, path, body) {
    const headers = { host: 'localhost:8792', 'user-agent': 'Mozilla/5.0 (Windows NT 10.0) Chrome/130', 'x-forwarded-for': ip,
      'x-vercel-ip-city': 'Charlotte', 'x-vercel-ip-country': 'US' };
    if (cookie) headers.cookie = cookie;
    if (csrf && method !== 'GET') headers['x-poly'] = '1';
    if (auth) headers.authorization = `Bearer ${auth}`;
    const req = { method, url: path, headers, body: body || undefined, socket: {} };
    const out = { status: 200, headers: {}, text: '' };
    const res = {
      headersSent: false,
      set statusCode(v) { out.status = v; },
      get statusCode() { return out.status; },
      setHeader(k, v) { out.headers[k.toLowerCase()] = v; },
      end(t = '') { out.text = t; this.headersSent = true; },
    };
    await handle(req, res);
    const set = out.headers['set-cookie'];
    if (set) cookie = /Max-Age=0/.test(set) ? '' : set.split(';')[0];
    return { status: out.status, headers: out.headers, data: out.text ? JSON.parse(out.text) : null };
  };
}

const PASSWORD = 'correct horse battery';
let counter = 0;
async function newAccount(call, extra = {}) {
  counter += 1;
  const email = `person${counter}@example.com`;
  const r = await call('POST', '/api/auth/register', { name: 'Savan', email, password: PASSWORD, country: 'United States', acceptTerms: true, ...extra });
  return { ...r, email };
}

test('create an account, sign out, sign in', async () => {
  const call = client();
  const r = await newAccount(call);
  assert.equal(r.status, 200, JSON.stringify(r.data));
  assert.match(r.data.recoveryKey, /^([2-9A-HJ-NP-Z]{4}-){5}[2-9A-HJ-NP-Z]{4}$/);
  assert.match(r.headers['set-cookie'], /HttpOnly; SameSite=Lax/);
  const me = await call('GET', '/api/me');
  assert.equal(me.data.user.name, 'Savan');
  assert.equal(me.data.user.emailVerified, false);
  assert.equal(me.data.user.prefs.communications.promotions, false);
  assert.equal((await call('POST', '/api/auth/logout')).status, 200);
  assert.equal((await call('GET', '/api/me')).status, 401);
  assert.equal((await call('POST', '/api/auth/login', { email: r.email, password: 'wrong password!' })).status, 401);
  assert.equal((await call('POST', '/api/auth/login', { email: r.email.toUpperCase(), password: PASSWORD })).status, 200);
  const events = (await call('GET', '/api/security/events')).data.events.map((e) => e.kind);
  assert.ok(events.includes('account-created') && events.includes('sign-in-failed') && events.includes('signed-in'));
});

test('what registration refuses', async () => {
  const call = client();
  assert.match((await newAccount(call, { password: 'short' })).data.error, /10 characters/);
  assert.match((await newAccount(call, { acceptTerms: false })).data.error, /Terms/);
  assert.match((await newAccount(call, { country: 'Atlantis' })).data.error, /country/);
  const ok = await newAccount(call);
  const again = await call('POST', '/api/auth/register', { name: 'X', email: ok.email, password: PASSWORD, country: 'Canada', acceptTerms: true });
  assert.equal(again.status, 409);
});

test('changes need the site header (CSRF guard)', async () => {
  const call = client({ csrf: false });
  const r = await call('POST', '/api/auth/register', { name: 'A', email: 'csrf@example.com', password: PASSWORD, country: 'Canada', acceptTerms: true });
  assert.equal(r.status, 403);
});

test('email verification and password reset by email', async () => {
  const call = client();
  const { email } = await newAccount(call);
  const link = outbox.filter((m) => m.to === email).at(-1).button.url;
  const verify = await call('POST', '/api/auth/verify', { token: link.split('#verify=')[1] });
  assert.equal(verify.data.user.emailVerified, true);
  assert.equal((await call('POST', '/api/auth/verify', { token: link.split('#verify=')[1] })).status, 400); // once only
  const other = client();
  assert.equal((await other('POST', '/api/auth/forgot', { email: 'nobody@example.com' })).data.ok, true); // no hint either way
  await other('POST', '/api/auth/forgot', { email });
  const reset = outbox.filter((m) => m.to === email).at(-1).button.url.split('#reset=')[1];
  assert.equal((await other('POST', '/api/auth/reset', { token: reset, password: 'a brand new password' })).status, 200);
  assert.equal((await call('GET', '/api/me')).status, 401); // other sessions were signed out
  assert.equal((await other('POST', '/api/auth/login', { email, password: 'a brand new password' })).status, 200);
});

test('recovery key resets the password and is replaced', async () => {
  const call = client();
  const { email, data } = await newAccount(call);
  const other = client();
  const bad = await other('POST', '/api/auth/recover', { email, recoveryKey: 'AAAA-BBBB-CCCC-DDDD-EEEE-FFFF', password: 'another good password' });
  assert.equal(bad.status, 401);
  const good = await other('POST', '/api/auth/recover', { email, recoveryKey: data.recoveryKey.toLowerCase().replace(/-/g, ' '), password: 'another good password' });
  assert.equal(good.status, 200, JSON.stringify(good.data));
  assert.notEqual(good.data.recoveryKey, data.recoveryKey);
  const reuse = await client()('POST', '/api/auth/recover', { email, recoveryKey: data.recoveryKey, password: 'a third good password' });
  assert.equal(reuse.status, 401);
});

test('link a computer with a 6-digit code, then manage it', async () => {
  const web = client();
  await newAccount(web);
  const pc = client({ csrf: false });
  const start = await pc('POST', '/api/v1/device/start', { name: 'Savan-PC', version: '0.8.0', arch: 'amd64', info: { maker: 'Lenovo', model: 'IdeaPad 5', ram: 16e9 } });
  assert.match(start.data.userCode, /^\d{6}$/);
  assert.equal((await pc('POST', '/api/v1/device/token', { deviceCode: start.data.deviceCode })).data.status, 'pending');
  const look = await web('POST', '/api/link/lookup', { code: start.data.userCode.replace(/(\d{3})/, '$1 ') });
  assert.equal(look.data.device.name, 'Savan-PC');
  assert.equal(look.data.device.place, 'Charlotte, US');
  await web('POST', '/api/link/approve', { code: start.data.userCode });
  const tok = await pc('POST', '/api/v1/device/token', { deviceCode: start.data.deviceCode });
  assert.equal(tok.data.status, 'approved');
  assert.match(tok.data.credential, /^pd_/);
  assert.equal((await pc('POST', '/api/v1/device/token', { deviceCode: start.data.deviceCode })).data.status, 'expired'); // handed out once

  const device = client({ csrf: false, auth: tok.data.credential });
  const check = await device('POST', '/api/v1/device/checkin', { version: '0.8.0', arch: 'amd64', channel: 'stable', remoteManagement: false });
  assert.equal(check.data.account.name, 'Savan');
  const [d] = (await web('GET', '/api/devices')).data.devices;
  assert.equal(d.online, true);
  assert.equal(d.info.model, 'IdeaPad 5');

  // restart needs Remote management on the computer itself
  assert.equal((await web('POST', `/api/devices/${d.id}/commands`, { kind: 'restart' })).status, 409);
  assert.equal((await web('POST', `/api/devices/${d.id}/commands`, { kind: 'check-updates' })).status, 200);
  await device('POST', '/api/v1/device/checkin', { version: '0.8.0', remoteManagement: true });
  assert.equal((await web('POST', `/api/devices/${d.id}/commands`, { kind: 'restart' })).status, 200);
  const cmds = (await device('POST', '/api/v1/device/checkin', { version: '0.8.0', remoteManagement: true })).data.commands.map((c) => c.kind).sort();
  assert.deepEqual(cmds, ['restart']);

  const up = await web('GET', '/api/updates');
  assert.equal(up.data.latest.stable.version, '9.1.0');
  assert.equal(up.data.devices[0].updateAvailable, true);

  await web('PATCH', `/api/devices/${d.id}`, { name: 'Desktop' });
  assert.equal((await web('GET', `/api/devices/${d.id}`)).data.device.name, 'Desktop');
  assert.equal((await client()('GET', `/api/devices/${d.id}`)).status, 401);
  await web('DELETE', `/api/devices/${d.id}`);
  const gone = await device('POST', '/api/v1/device/checkin', {});
  assert.equal(gone.status, 401);
  assert.equal(gone.data.removed, true);
});

test('sign in from PolyOS, create an account from PolyOS, and sync', async () => {
  const web = client();
  const { email } = await newAccount(web);
  const pc = client({ csrf: false });
  const signin = await pc('POST', '/api/v1/device/signin', { email, password: PASSWORD, name: 'Laptop', info: {} });
  assert.match(signin.data.credential, /^pd_/);
  const made = await pc('POST', '/api/v1/device/register', { name: 'New Person', email: 'fromos@example.com', password: PASSWORD, country: 'Canada',
    acceptTerms: true, deviceName: 'Their PC', info: {} });
  assert.match(made.data.recoveryKey, /-/);

  const a = client({ csrf: false, auth: signin.data.credential });
  assert.equal((await a('PUT', '/api/v1/sync', { items: { 'settings.theme': 'light', accent: '#678fd9' } })).data.saved, 2);
  assert.equal((await a('PUT', '/api/v1/sync', { items: { '../bad': 1 } })).status, 400);
  const items = (await a('GET', '/api/v1/sync')).data.items;
  assert.equal(items['settings.theme'].value, 'light');
  const b = client({ csrf: false, auth: made.data.credential });
  assert.deepEqual((await b('GET', '/api/v1/sync')).data.items, {}); // another account sees nothing
});

test('preferences, sessions, recovery key and deleting the account', async () => {
  const call = client();
  const { email } = await newAccount(call);
  const p = await call('PUT', '/api/preferences', { telemetry: 'standard', communications: { security: false, promotions: true } });
  assert.equal(p.data.user.prefs.telemetry, 'standard');
  assert.equal(p.data.user.prefs.communications.security, true); // can't turn security alerts off
  const second = client();
  await second('POST', '/api/auth/login', { email, password: PASSWORD });
  assert.equal((await call('GET', '/api/sessions')).data.sessions.length, 2);
  await call('POST', '/api/sessions/others/revoke');
  assert.equal((await second('GET', '/api/me')).status, 401);
  assert.equal((await call('POST', '/api/recovery/regenerate', { password: 'nope nope nope' })).status, 401);
  assert.match((await call('POST', '/api/recovery/regenerate', { password: PASSWORD })).data.recoveryKey, /-/);
  const exported = await call('GET', '/api/account/export');
  assert.equal(exported.data.account.email, email);
  assert.equal((await call('POST', '/api/account/delete', { password: PASSWORD })).data.deleted, true);
  assert.equal((await client()('POST', '/api/auth/login', { email, password: PASSWORD })).status, 401);
});

test('update check and downloads need no account', async () => {
  const anyone = client({ csrf: false });
  const r = await anyone('GET', '/api/v1/updates/check?channel=stable&version=0.8.0');
  assert.equal(r.data.update_available, true);
  assert.equal(r.data.signature, 'https://example.test/polyos-update.json.sig');
  assert.equal((await anyone('GET', '/api/v1/updates/check?version=9.1.0')).data.update_available, false);
  assert.equal((await anyone('GET', '/api/v1/updates/check?channel=beta&version=9.1.0')).data.version, '9.2.0-beta');
  const dl = await anyone('GET', '/api/download/pc');
  assert.equal(dl.status, 302);
  assert.equal(dl.headers.location, 'https://example.test/polyos-amd64.iso');
  assert.equal((await anyone('GET', '/api/releases/latest')).data.wholeIso, false);
  // with SourceForge set up, the whole ISO comes from there (GitHub may hold it only in parts)
  process.env.SOURCEFORGE_PROJECT = 'polyos-7';
  try {
    // a whole ISO on GitHub is still downloaded from there
    assert.equal((await anyone('GET', '/api/download/pc')).headers.location, 'https://example.test/polyos-amd64.iso');
    // the ARM64 one isn't on this release: SourceForge unreachable, its direct link, which picks a mirror by itself
    const sf = await anyone('GET', '/api/download/arm64');
    assert.equal(sf.headers.location, 'https://downloads.sourceforge.net/project/polyos-7/v9.1.0/polyos-arm64.iso');
    assert.equal(sf.headers['cache-control'], 'no-store');
    // normally: straight to a mirror's link to the file, so the download starts from this site
    const saved = globalThis.fetch;
    const mirror = 'https://example.dl.sourceforge.net/project/polyos-7/v9.1.0/polyos-arm64.iso?viasf=1&e=1&st=x';
    let answer = mirror;
    globalThis.fetch = async (url, init) => (String(url).startsWith('https://downloads.sourceforge.net/')
      ? new Response(null, { status: 302, headers: { location: answer } })
      : saved(url, init));
    try {
      assert.equal((await anyone('GET', '/api/download/arm64')).headers.location, mirror);
      answer = 'https://elsewhere.test/polyos-arm64.iso'; // only SourceForge's own mirrors are passed on
      assert.equal((await anyone('GET', '/api/download/arm64')).headers.location, sf.headers.location);
    } finally {
      globalThis.fetch = saved;
    }
    assert.equal((await anyone('GET', '/api/download/checksums')).headers.location.includes('sourceforge'), false);
    assert.equal((await anyone('GET', '/api/releases/latest')).data.wholeIso, true);
  } finally {
    delete process.env.SOURCEFORGE_PROJECT;
  }
});

test('rate limits sign-in attempts', async () => {
  const call = client({ ip: '198.51.100.7' });
  const { email } = await newAccount(call);
  let last;
  for (let i = 0; i < 11; i += 1) last = await client({ ip: '198.51.100.7' })('POST', '/api/auth/login', { email, password: 'wrong password!!' });
  assert.equal(last.status, 429);
});

test('downloads and stable updates still work when GitHub’s API is busy', async () => {
  const saved = globalThis.fetch;
  globalThis.fetch = async (url) => {
    if (String(url).startsWith('https://api.github.com')) return new Response('rate limited', { status: 403 });
    if (String(url).endsWith('/releases/latest/download/polyos-update.json')) {
      return new Response(JSON.stringify({ version: '9.9.0', packages: [{ name: 'polyos-shell', file: 'polyos-shell_9.9.0_all.deb', size: 5 }] }));
    }
    return new Response('', { status: 404 });
  };
  try {
    // a separate copy of the module, so the earlier tests' cached answers don't count
    const fresh = await import(`../releases.js?fresh=${Date.now()}`);
    const r = await fresh.check({ channel: 'stable', version: '0.8.0' });
    assert.equal(r.version, '9.9.0');
    assert.equal(r.update_available, true);
    assert.equal(fresh.latestFileUrl('polyos-amd64.iso'), `https://github.com/${fresh.releasesRepo()}/releases/latest/download/polyos-amd64.iso`);
    await assert.rejects(fresh.check({ channel: 'beta' })); // other channels need the API
  } finally {
    globalThis.fetch = saved;
  }
});

test('edition releases go only to that edition', async () => {
  const saved = globalThis.fetch;
  const asset = (v, name) => ({ name, browser_download_url: `https://example.test/${v}/${name}`, size: 1 });
  const rel = (v, prerelease) => ({ tag_name: `v${v}`, name: `PolyOS v${v}`, body: '', prerelease, draft: false,
    assets: [asset(v, 'polyos-update.json'), asset(v, 'polyos-update.json.sig'), asset(v, `polyos-shell_${v}_all.deb`)] });
  const list = [rel('9.4.0', true), rel('9.3.0', true), rel('9.2.0', false)]; // newest first
  const editions = { '9.4.0': ['developer'], '9.3.0': [], '9.2.0': [] }; // 9.4.0: Developer only; 9.3.0: a beta
  globalThis.fetch = async (url) => {
    const u = String(url);
    if (u.includes('/releases?')) return new Response(JSON.stringify(list));
    if (u.includes('/releases/latest')) return new Response(JSON.stringify(list[2]));
    const m = /example\.test\/([\d.]+)\/polyos-update\.json$/.exec(u);
    if (m) return new Response(JSON.stringify({ version: m[1], editions: editions[m[1]] }));
    return new Response('{}', { status: 404 });
  };
  try {
    const fresh = await import(`../releases.js?editions=${Date.now()}`);
    assert.equal((await fresh.check({ version: '9.0.0', edition: 'developer' })).version, '9.4.0');
    assert.equal((await fresh.check({ version: '9.0.0', edition: 'regular' })).version, '9.2.0'); // not the beta, not 9.4.0
    assert.equal((await fresh.check({ version: '9.0.0', edition: 'gaming', channel: 'beta' })).version, '9.3.0');
    assert.equal((await fresh.check({ version: '9.0.0' })).version, '9.2.0'); // older PolyOS: everyone's releases
    assert.equal((await fresh.check({ version: '9.4.0', edition: 'developer' })).update_available, false);
  } finally {
    globalThis.fetch = saved;
  }
});
