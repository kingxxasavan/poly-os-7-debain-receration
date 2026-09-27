// Poly Account API. One Vercel function (api/[...route].js) routes every request here.
//
// Two kinds of callers:
//   the website       a session cookie (HttpOnly), and the header X-Poly: 1 on changes (CSRF guard)
//   PolyOS computers  Authorization: Bearer pd_… — a credential unique to that device, stored hashed,
//                     revocable on its own. The computer never keeps the account password.
//
// PolyOS works without any of this; an account adds device management, recovery and sync.

import { q, one } from './db.js';
import { emailEnabled, sendEmail } from './email.js';
import * as releases from './releases.js';
import {
  dummyVerify, hashSecret, normalizeRecoveryKey, passwordProblem, recoveryKey, sha256, token, userCode, verifySecret,
} from './security.js';

export const TERMS = {
  version: '2026-09-26',
  title: 'Poly Account Terms and Privacy Policy',
  changes: ['First version of the Poly Account terms and privacy policy.'],
};
const SESSION_DAYS = 30;
const LINK_MINUTES = 10;
const ONLINE_MINUTES = 20;
const COMMANDS = ['check-updates', 'update', 'restart', 'lock'];
const REMOTE_ONLY = new Set(['update', 'restart', 'lock']); // need Remote management on the device itself
const TELEMETRY = ['minimal', 'standard', 'diagnostic'];
const COMMS = ['security', 'account', 'updates', 'announcements', 'promotions'];
const DEFAULT_PREFS = {
  telemetry: 'minimal',
  sync: { settings: true, themes: true, wallpapers: true, wifi: false, browser: false, apps: true, accessibility: true },
  communications: { security: true, account: true, updates: true, announcements: false, promotions: false },
};
export const COUNTRIES = ['Afghanistan', 'Albania', 'Algeria', 'Argentina', 'Armenia', 'Australia', 'Austria', 'Azerbaijan', 'Bahrain',
  'Bangladesh', 'Belarus', 'Belgium', 'Bolivia', 'Bosnia and Herzegovina', 'Brazil', 'Bulgaria', 'Cambodia', 'Cameroon', 'Canada', 'Chile',
  'China', 'Colombia', 'Costa Rica', 'Croatia', 'Cyprus', 'Czechia', 'Denmark', 'Dominican Republic', 'Ecuador', 'Egypt', 'El Salvador',
  'Estonia', 'Ethiopia', 'Finland', 'France', 'Georgia', 'Germany', 'Ghana', 'Greece', 'Guatemala', 'Honduras', 'Hong Kong', 'Hungary',
  'Iceland', 'India', 'Indonesia', 'Iran', 'Iraq', 'Ireland', 'Israel', 'Italy', 'Jamaica', 'Japan', 'Jordan', 'Kazakhstan', 'Kenya',
  'Kuwait', 'Latvia', 'Lebanon', 'Lithuania', 'Luxembourg', 'Malaysia', 'Malta', 'Mexico', 'Moldova', 'Mongolia', 'Morocco', 'Nepal',
  'Netherlands', 'New Zealand', 'Nicaragua', 'Nigeria', 'North Macedonia', 'Norway', 'Oman', 'Pakistan', 'Panama', 'Paraguay', 'Peru',
  'Philippines', 'Poland', 'Portugal', 'Puerto Rico', 'Qatar', 'Romania', 'Russia', 'Saudi Arabia', 'Senegal', 'Serbia', 'Singapore',
  'Slovakia', 'Slovenia', 'South Africa', 'South Korea', 'Spain', 'Sri Lanka', 'Sweden', 'Switzerland', 'Taiwan', 'Tanzania', 'Thailand',
  'Tunisia', 'Turkey', 'Uganda', 'Ukraine', 'United Arab Emirates', 'United Kingdom', 'United States', 'Uruguay', 'Uzbekistan',
  'Venezuela', 'Vietnam', 'Zambia', 'Zimbabwe', 'Other'];

export class ApiError extends Error {
  constructor(message, status = 400, extra = {}) {
    super(message);
    this.status = status;
    this.extra = extra;
  }
}
const fail = (message, status = 400, extra) => { throw new ApiError(message, status, extra); };

// ---- request helpers ----------------------------------------------------------------------------
async function readBody(req) {
  if (req.method === 'GET' || req.method === 'HEAD') return {};
  if (req.body && typeof req.body === 'object' && !Buffer.isBuffer(req.body)) return req.body; // Vercel parsed it
  let raw = typeof req.body === 'string' ? req.body : Buffer.isBuffer(req.body) ? req.body.toString('utf8') : '';
  if (!raw && req.readable !== false && typeof req.on === 'function') {
    raw = await new Promise((resolve, reject) => {
      let data = '';
      req.on('data', (chunk) => {
        data += chunk;
        if (data.length > 256 * 1024) reject(new ApiError('That request is too large.', 413));
      });
      req.on('end', () => resolve(data));
      req.on('error', reject);
    });
  }
  if (!raw) return {};
  try {
    const value = JSON.parse(raw);
    return value && typeof value === 'object' ? value : {};
  } catch {
    throw new ApiError('Send JSON.', 400);
  }
}

function header(req, name) {
  const v = req.headers[name.toLowerCase()];
  return Array.isArray(v) ? v[0] : v || '';
}

function clientIp(req) {
  return header(req, 'x-forwarded-for').split(',')[0].trim() || header(req, 'x-real-ip') || req.socket?.remoteAddress || '';
}

// "Charlotte, US" from Vercel's location headers (no IP address is stored).
function place(req) {
  const city = decodeURIComponent(header(req, 'x-vercel-ip-city') || '');
  const country = header(req, 'x-vercel-ip-country');
  return [city, country].filter(Boolean).join(', ');
}

function agent(req) {
  const ua = header(req, 'user-agent');
  if (/PolyOS/i.test(ua)) return 'PolyOS';
  const browser = /Edg\//.test(ua) ? 'Edge' : /Firefox\//.test(ua) ? 'Firefox' : /Chrome\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : 'Browser';
  const os = /Windows/.test(ua) ? 'Windows' : /Android/.test(ua) ? 'Android' : /iPhone|iPad/.test(ua) ? 'iOS' : /Mac OS/.test(ua) ? 'macOS' : /Linux/.test(ua) ? 'Linux' : '';
  return [os, browser].filter(Boolean).join(' · ');
}

function cookies(req) {
  return Object.fromEntries(header(req, 'cookie').split(';').map((c) => c.trim().split('=')).filter((p) => p[0])
    .map(([k, ...v]) => [k, decodeURIComponent(v.join('='))]));
}

function siteUrl(req) {
  if (process.env.SITE_URL) return process.env.SITE_URL.replace(/\/$/, '');
  const host = header(req, 'x-forwarded-host') || header(req, 'host') || 'localhost';
  const proto = header(req, 'x-forwarded-proto') || (host.startsWith('localhost') || host.startsWith('127.') ? 'http' : 'https');
  return `${proto}://${host}`;
}

function sessionCookie(req, value, maxAge) {
  const secure = siteUrl(req).startsWith('https') ? '; Secure' : '';
  return `poly_session=${value}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${maxAge}${secure}`;
}

const str = (v, max = 200) => (typeof v === 'string' ? v.trim().slice(0, max) : '');
const email = (v) => {
  const e = str(v, 254).toLowerCase();
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(e)) fail('Enter a valid email address.');
  return e;
};
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// ---- rate limits --------------------------------------------------------------------------------
async function limit(key, max, minutes) {
  const [{ n }] = await q(`SELECT count(*)::int AS n FROM attempts WHERE key = $1 AND at > now() - make_interval(mins => $2)`, [key, minutes]);
  if (n >= max) fail('Too many tries. Wait a few minutes and try again.', 429);
  await q('INSERT INTO attempts (key) VALUES ($1)', [key]);
  if (Math.random() < 0.02) await q(`DELETE FROM attempts WHERE at < now() - interval '1 day'`);
}

// ---- users and sessions ---------------------------------------------------------------------------
function prefsOf(user) {
  const p = user.prefs || {};
  return {
    telemetry: TELEMETRY.includes(p.telemetry) ? p.telemetry : DEFAULT_PREFS.telemetry,
    sync: { ...DEFAULT_PREFS.sync, ...(p.sync || {}) },
    communications: { ...DEFAULT_PREFS.communications, ...(p.communications || {}), security: true },
  };
}

function publicUser(user) {
  return {
    id: user.id, name: user.name, email: user.email, emailVerified: user.email_verified, country: user.country,
    createdAt: user.created_at, passwordChangedAt: user.password_changed_at, recoveryCreatedAt: user.recovery_created_at,
    terms: { version: user.terms_version, acceptedAt: user.terms_accepted_at, current: TERMS.version, outdated: user.terms_version !== TERMS.version },
    prefs: prefsOf(user),
  };
}

async function event(userId, kind, req, detail = {}) {
  await q('INSERT INTO events (user_id, kind, detail, place) VALUES ($1, $2, $3, $4)', [userId, kind, JSON.stringify(detail), req ? place(req) : '']);
}

async function startSession(req, res, user) {
  const raw = token('ps_');
  await q(`INSERT INTO sessions (user_id, token_hash, agent, place, expires_at) VALUES ($1, $2, $3, $4, now() + make_interval(days => $5))`,
    [user.id, sha256(raw), agent(req), place(req), SESSION_DAYS]);
  res.setHeader('Set-Cookie', sessionCookie(req, raw, SESSION_DAYS * 86400));
}

async function currentSession(req) {
  const raw = cookies(req).poly_session;
  if (!raw) return null;
  const row = await one(`SELECT s.id AS session_id, s.last_seen, u.* FROM sessions s JOIN users u ON u.id = s.user_id
                         WHERE s.token_hash = $1 AND s.expires_at > now()`, [sha256(raw)]);
  if (!row) return null;
  if (Date.now() - new Date(row.last_seen).getTime() > 5 * 60 * 1000) {
    await q(`UPDATE sessions SET last_seen = now(), expires_at = now() + make_interval(days => $2) WHERE id = $1`, [row.session_id, SESSION_DAYS]);
  }
  return { user: row, sessionId: row.session_id };
}

async function sendVerification(req, user, address = user.email) {
  const raw = token();
  await q(`INSERT INTO email_tokens (token_hash, user_id, kind, email, expires_at) VALUES ($1, $2, 'verify', $3, now() + interval '2 days')`,
    [sha256(raw), user.id, address]);
  await sendEmail(address, 'Verify your email for Poly Account', {
    title: `Hi ${escapeHtml(user.name)}, confirm your email`,
    body: '<p>Confirm this address so Poly can send you security alerts and help you recover your account.</p>',
    button: { label: 'Verify email', url: `${siteUrl(req)}/account#verify=${raw}` },
  });
}

async function securityAlert(req, user, subject, text) {
  await sendEmail(user.email, subject, {
    title: escapeHtml(subject),
    body: `<p>${escapeHtml(text)}</p><p>${escapeHtml(place(req) ? `Where: ${place(req)}.` : '')} If this wasn't you, change your password and remove devices you don't recognize.</p>`,
    button: { label: 'Review your account', url: `${siteUrl(req)}/account#security` },
  });
}

async function createUser(req, body) {
  const name = str(body.name, 80);
  const address = email(body.email);
  const country = str(body.country, 60);
  if (!name) fail('Enter your name.');
  if (!COUNTRIES.includes(country)) fail('Choose your country.');
  const problem = passwordProblem(body.password);
  if (problem) fail(problem);
  if (body.acceptTerms !== true) fail('Agree to the Terms of Service and Privacy Policy to create an account.');
  await limit(`register:${sha256(clientIp(req))}`, 10, 60);
  if (await one('SELECT 1 FROM users WHERE email = $1', [address])) {
    fail('There’s already a Poly Account with this email. Sign in instead.', 409);
  }
  const key = recoveryKey();
  const user = await one(`INSERT INTO users (email, name, country, password_hash, recovery_hash, recovery_created_at, terms_version, terms_accepted_at, prefs)
                          VALUES ($1, $2, $3, $4, $5, now(), $6, now(), $7) RETURNING *`,
  [address, name, country, await hashSecret(body.password), await hashSecret(key), TERMS.version, JSON.stringify(DEFAULT_PREFS)]);
  await event(user.id, 'account-created', req);
  await sendVerification(req, user);
  return { user, recoveryKey: key };
}

async function checkPassword(req, address, password) {
  await limit(`login:${address}`, 10, 15);
  await limit(`login-ip:${sha256(clientIp(req))}`, 40, 15);
  const user = await one('SELECT * FROM users WHERE email = $1', [address]);
  if (!user) {
    await dummyVerify(password);
    fail('That email and password don’t match a Poly Account.', 401);
  }
  if (!(await verifySecret(String(password || ''), user.password_hash))) {
    await event(user.id, 'sign-in-failed', req);
    fail('That email and password don’t match a Poly Account.', 401);
  }
  return user;
}

// ---- devices ------------------------------------------------------------------------------------
function cleanInfo(info) {
  const out = {};
  const keys = ['maker', 'model', 'cpu', 'cores', 'ram', 'storage', 'storageFree', 'gpu', 'edition', 'kernel', 'hostname', 'secureBoot', 'year', 'kind'];
  for (const k of keys) {
    const v = info?.[k];
    if (typeof v === 'string') out[k] = v.slice(0, 120);
    else if (typeof v === 'number' && Number.isFinite(v)) out[k] = v;
    else if (typeof v === 'boolean') out[k] = v;
  }
  return out;
}

function deviceName(v, info) {
  return str(v, 60) || str(info?.hostname, 60) || [info?.maker, info?.model].filter(Boolean).join(' ').slice(0, 60) || 'PolyOS computer';
}

function publicDevice(d) {
  const lastSeen = d.last_seen ? new Date(d.last_seen) : null;
  return {
    id: d.id, name: d.name, version: d.version, arch: d.arch, channel: d.channel, info: d.info || {},
    remoteManagement: d.remote_management, policy: d.policy || {}, place: d.place, createdAt: d.created_at,
    lastSeen: d.last_seen, online: Boolean(lastSeen && Date.now() - lastSeen.getTime() < ONLINE_MINUTES * 60 * 1000),
  };
}

async function createDevice(req, user, name, info, meta = {}) {
  const credential = token('pd_');
  const device = await one(`INSERT INTO devices (user_id, name, credential_hash, version, arch, channel, info, place, last_seen)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now()) RETURNING *`,
  [user.id, deviceName(name, info), sha256(credential), str(meta.version, 20), str(meta.arch, 20),
    releases.CHANNELS.includes(meta.channel) ? meta.channel : 'stable', JSON.stringify(cleanInfo(info)), place(req)]);
  await event(user.id, 'device-added', req, { device: device.name });
  await securityAlert(req, user, 'A computer was connected to your Poly Account', `“${device.name}” can now use your Poly Account.`);
  return { device, credential };
}

async function deviceFromRequest(req) {
  const auth = header(req, 'authorization');
  const m = /^Bearer (pd_[A-Za-z0-9_-]{20,})$/.exec(auth);
  if (!m) fail('This computer isn’t connected to a Poly Account.', 401);
  const row = await one(`SELECT d.*, u.name AS user_name, u.email AS user_email, u.email_verified, u.terms_version, u.prefs AS user_prefs
                         FROM devices d JOIN users u ON u.id = d.user_id WHERE d.credential_hash = $1`, [sha256(m[1])]);
  if (!row) fail('This computer was removed from its Poly Account.', 401, { removed: true });
  return row;
}

function accountForDevice(user, device, credential) {
  return {
    credential,
    account: { name: user.name, email: user.email, emailVerified: user.email_verified },
    device: { id: device.id, name: device.name },
  };
}

// ---- routes -------------------------------------------------------------------------------------
const routes = [];
const route = (method, path, auth, fn) => {
  const keys = [];
  const re = new RegExp(`^${path.replace(/:([a-z]+)/g, (_m, k) => { keys.push(k); return '([^/]+)'; })}$`);
  routes.push({ method, re, keys, auth, fn });
};

// -- public
route('GET', '/api/status', null, async () => {
  await q('SELECT 1');
  return { ok: true, email: emailEnabled(), releases: releases.releasesRepo(), terms: TERMS.version };
});
route('GET', '/api/countries', null, async () => ({ countries: COUNTRIES }));
route('GET', '/api/terms', null, async () => TERMS);
// The SourceForge project that mirrors each release's ISOs as whole files (Vercel variable SOURCEFORGE_PROJECT)
function sourceforgeProject() {
  const project = (process.env.SOURCEFORGE_PROJECT || '').trim();
  return /^[a-z0-9-]+$/i.test(project) ? project : '';
}
route('GET', '/api/releases/latest', null, async ({ url }) => {
  const rel = await releases.latest(url.searchParams.get('channel') || 'stable');
  // wholeIso: /download/pc and /download/arm64 give the whole ISO even when GitHub holds it in parts
  return { ...rel, repo: releases.releasesRepo(), wholeIso: Boolean(sourceforgeProject()) };
});
route('GET', '/api/releases', null, async () => ({ releases: await releases.history(12) }));
// A SourceForge file as a link that starts the download itself. downloads.sourceforge.net picks a
// mirror and answers with that mirror's signed link to the file (valid for about a day), so the
// browser downloads straight from the mirror and stays on this site: no SourceForge page, no countdown.
// If SourceForge is slow or answers something else, the plain direct link still works (it redirects too).
async function sourceforgeFile(project, path) {
  const direct = `https://downloads.sourceforge.net/project/${project}/${path}`;
  try {
    const res = await fetch(direct, { method: 'HEAD', redirect: 'manual', signal: AbortSignal.timeout(4000) });
    const mirror = res.headers.get('location') || '';
    if (res.status >= 300 && res.status < 400 && /^https:\/\/[a-z0-9-]+\.dl\.sourceforge\.net\//.test(mirror)) return mirror;
  } catch { /* the direct link */ }
  return direct;
}
route('GET', '/api/download/:what', null, async ({ params, res }) => {
  const file = { pc: 'polyos-amd64.iso', arm64: 'polyos-arm64.iso', checksums: 'SHA256SUMS' }[params.what];
  if (!file && /^polyos-[a-z]+$/.test(params.what)) { // the update packages, e.g. /download/polyos-shell
    const rel = await releases.latest('stable');
    const name = Object.keys(rel.assets).find((n) => n.startsWith(`${params.what}_`) && n.endsWith('.deb'));
    if (!name) fail('That package isn’t in the newest release.', 404);
    res.statusCode = 302;
    res.setHeader('Location', rel.assets[name].url);
    return null;
  }
  if (!file) fail('Not found.', 404);
  let location = releases.latestFileUrl(file); // works even when GitHub's API is busy
  let cache = 'public, max-age=300';
  try {
    const rel = await releases.latest('stable');
    const project = sourceforgeProject();
    if (rel.assets[file]) {
      location = rel.assets[file].url; // GitHub serves it as a plain download
    } else if (project && file.endsWith('.iso')) {
      // GitHub holds files under 2 GiB, so a bigger ISO is only in parts there: the whole one from SourceForge
      location = await sourceforgeFile(project, `v${rel.version}/${file}`);
      cache = 'no-store'; // a mirror's link expires
    } else if (rel.assets[`${file}.part0`]) {
      location = rel.page; // only in parts: the release page lists them (a single part isn't a working ISO)
    }
  } catch { /* the direct link above */ }
  res.statusCode = 302;
  res.setHeader('Location', location);
  res.setHeader('Cache-Control', cache);
  return null;
});
route('POST', '/api/support', null, async ({ req, body }) => {
  const message = str(body.message, 4000);
  if (message.length < 10) fail('Tell us a little more (at least 10 characters).');
  await limit(`support:${sha256(clientIp(req))}`, 5, 60);
  const address = body.email ? email(body.email) : '';
  const topic = ['install', 'account', 'bug', 'security', 'idea', 'other'].includes(body.topic) ? body.topic : 'other';
  await q('INSERT INTO feedback (email, topic, message, place) VALUES ($1, $2, $3, $4)', [address, topic, message, place(req)]);
  return { ok: true };
});

// -- signing in and out
route('POST', '/api/auth/register', 'csrf', async ({ req, res, body }) => {
  const { user, recoveryKey: key } = await createUser(req, body);
  await startSession(req, res, user);
  return { user: publicUser(user), recoveryKey: key, emailSent: emailEnabled() };
});
route('POST', '/api/auth/login', 'csrf', async ({ req, res, body }) => {
  const user = await checkPassword(req, email(body.email), body.password);
  await startSession(req, res, user);
  await event(user.id, 'signed-in', req, { agent: agent(req) });
  return { user: publicUser(user) };
});
route('POST', '/api/auth/logout', 'csrf', async ({ req, res }) => {
  const raw = cookies(req).poly_session;
  if (raw) await q('DELETE FROM sessions WHERE token_hash = $1', [sha256(raw)]);
  res.setHeader('Set-Cookie', sessionCookie(req, '', 0));
  return { ok: true };
});
route('POST', '/api/auth/verify', 'csrf', async ({ req, body }) => {
  const row = await one(`UPDATE email_tokens SET used_at = now() WHERE token_hash = $1 AND kind = 'verify' AND used_at IS NULL AND expires_at > now()
                         RETURNING user_id, email`, [sha256(str(body.token, 100))]);
  if (!row) fail('That link has expired or was already used. Send a new one from your account.');
  const user = await one('UPDATE users SET email = $2, email_verified = true WHERE id = $1 RETURNING *', [row.user_id, row.email]);
  await event(user.id, 'email-verified', req, { email: row.email });
  return { user: publicUser(user) };
});
route('POST', '/api/auth/forgot', 'csrf', async ({ req, body }) => {
  const address = email(body.email);
  await limit(`forgot:${address}`, 3, 60);
  await limit(`forgot-ip:${sha256(clientIp(req))}`, 10, 60);
  const user = await one('SELECT * FROM users WHERE email = $1', [address]);
  if (user && user.email_verified) {
    const raw = token();
    await q(`INSERT INTO email_tokens (token_hash, user_id, kind, email, expires_at) VALUES ($1, $2, 'reset', $3, now() + interval '1 hour')`,
      [sha256(raw), user.id, address]);
    await sendEmail(address, 'Reset your Poly Account password', {
      title: 'Reset your password',
      body: '<p>Someone (hopefully you) asked to reset the password for your Poly Account. The link works for an hour.</p>',
      button: { label: 'Choose a new password', url: `${siteUrl(req)}/account#reset=${raw}` },
    });
  }
  // The same answer either way, so this can't be used to find out who has an account.
  return { ok: true, emailSent: emailEnabled() };
});
async function setPassword(req, user, password, how) {
  const problem = passwordProblem(password);
  if (problem) fail(problem);
  const updated = await one('UPDATE users SET password_hash = $2, password_changed_at = now() WHERE id = $1 RETURNING *', [user.id, await hashSecret(password)]);
  await q('DELETE FROM sessions WHERE user_id = $1', [user.id]); // everyone signs in again
  await event(user.id, 'password-changed', req, { how });
  await securityAlert(req, updated, 'Your Poly Account password was changed', 'Your password was just changed and every browser was signed out.');
  return updated;
}
route('POST', '/api/auth/reset', 'csrf', async ({ req, res, body }) => {
  const row = await one(`UPDATE email_tokens SET used_at = now() WHERE token_hash = $1 AND kind = 'reset' AND used_at IS NULL AND expires_at > now()
                         RETURNING user_id`, [sha256(str(body.token, 100))]);
  if (!row) fail('That reset link has expired or was already used. Ask for a new one.');
  const user = await setPassword(req, await one('SELECT * FROM users WHERE id = $1', [row.user_id]), body.password, 'email');
  await startSession(req, res, user);
  return { user: publicUser(user) };
});
// Recovery without email: the recovery key (shown once when the account was made) resets the password.
route('POST', '/api/auth/recover', 'csrf', async ({ req, res, body }) => {
  const address = email(body.email);
  await limit(`recover:${address}`, 5, 60);
  await limit(`recover-ip:${sha256(clientIp(req))}`, 15, 60);
  const user = await one('SELECT * FROM users WHERE email = $1', [address]);
  const key = normalizeRecoveryKey(body.recoveryKey);
  if (!user || !user.recovery_hash || !(await verifySecret(key, user.recovery_hash))) {
    if (!user) await dummyVerify(key);
    fail('That email and recovery key don’t match.', 401);
  }
  const updated = await setPassword(req, user, body.password, 'recovery key');
  const fresh = recoveryKey(); // a used key is replaced
  await q('UPDATE users SET recovery_hash = $2, recovery_created_at = now() WHERE id = $1', [user.id, await hashSecret(fresh)]);
  await event(user.id, 'recovery-used', req);
  await startSession(req, res, updated);
  return { user: publicUser(updated), recoveryKey: fresh };
});

// -- the account
route('GET', '/api/me', 'user', async ({ user }) => {
  const [{ devices }] = await q('SELECT count(*)::int AS devices FROM devices WHERE user_id = $1', [user.id]);
  return { user: publicUser(user), devices, email: emailEnabled() };
});
route('PATCH', '/api/account', 'user', async ({ req, user, body }) => {
  const name = body.name === undefined ? user.name : str(body.name, 80);
  const country = body.country === undefined ? user.country : str(body.country, 60);
  if (!name) fail('Enter your name.');
  if (!COUNTRIES.includes(country)) fail('Choose your country.');
  const updated = await one('UPDATE users SET name = $2, country = $3 WHERE id = $1 RETURNING *', [user.id, name, country]);
  await event(user.id, 'profile-updated', req);
  return { user: publicUser(updated) };
});
route('POST', '/api/account/email', 'user', async ({ req, user, body }) => {
  const address = email(body.email);
  if (!(await verifySecret(String(body.password || ''), user.password_hash))) fail('That password isn’t right.', 401);
  if (await one('SELECT 1 FROM users WHERE email = $1 AND id <> $2', [address, user.id])) fail('Another Poly Account uses that email.', 409);
  await sendVerification(req, user, address);
  await event(user.id, 'email-change-requested', req, { email: address });
  return { ok: true, pending: address, emailSent: emailEnabled() };
});
route('POST', '/api/account/resend-verification', 'user', async ({ req, user }) => {
  await limit(`verify:${user.id}`, 3, 60);
  await sendVerification(req, user);
  return { ok: true, emailSent: emailEnabled() };
});
route('POST', '/api/account/password', 'user', async ({ req, res, user, body }) => {
  if (!(await verifySecret(String(body.current || ''), user.password_hash))) fail('Your current password isn’t right.', 401);
  const updated = await setPassword(req, user, body.password, 'settings');
  await startSession(req, res, updated); // this browser stays signed in
  return { user: publicUser(updated) };
});
route('POST', '/api/account/delete', 'user', async ({ req, res, user, body }) => {
  if (!(await verifySecret(String(body.password || ''), user.password_hash))) fail('That password isn’t right.', 401);
  await q('DELETE FROM users WHERE id = $1', [user.id]); // sessions, devices, sync and history go with it
  res.setHeader('Set-Cookie', sessionCookie(req, '', 0));
  return { deleted: true };
});
route('GET', '/api/account/export', 'user', async ({ user, res }) => {
  const data = {
    account: publicUser(user),
    devices: (await q('SELECT * FROM devices WHERE user_id = $1', [user.id])).map(publicDevice),
    sessions: await q('SELECT agent, place, created_at, last_seen FROM sessions WHERE user_id = $1', [user.id]),
    activity: await q('SELECT kind, detail, place, at FROM events WHERE user_id = $1 ORDER BY at DESC LIMIT 500', [user.id]),
    sync: await q('SELECT key, value, updated_at FROM sync_items WHERE user_id = $1', [user.id]),
    exportedAt: new Date().toISOString(),
  };
  res.setHeader('Content-Disposition', 'attachment; filename="poly-account.json"');
  return data;
});
route('GET', '/api/terms/status', 'user', async ({ user }) => ({ ...TERMS, accepted: user.terms_version === TERMS.version, acceptedAt: user.terms_accepted_at }));
route('POST', '/api/terms/accept', 'user', async ({ req, user }) => {
  const updated = await one('UPDATE users SET terms_version = $2, terms_accepted_at = now() WHERE id = $1 RETURNING *', [user.id, TERMS.version]);
  await event(user.id, 'terms-accepted', req, { version: TERMS.version });
  return { user: publicUser(updated) };
});
route('PUT', '/api/preferences', 'user', async ({ req, user, body }) => {
  const cur = prefsOf(user);
  const next = { ...cur };
  if (body.telemetry !== undefined) {
    if (!TELEMETRY.includes(body.telemetry)) fail('Choose Minimal, Standard or Diagnostic.');
    next.telemetry = body.telemetry;
  }
  if (body.communications && typeof body.communications === 'object') {
    for (const k of COMMS) if (typeof body.communications[k] === 'boolean') next.communications[k] = body.communications[k];
    next.communications.security = true; // security alerts and legal notices always go out
  }
  if (body.sync && typeof body.sync === 'object') {
    for (const k of Object.keys(DEFAULT_PREFS.sync)) if (typeof body.sync[k] === 'boolean') next.sync[k] = body.sync[k];
  }
  const updated = await one('UPDATE users SET prefs = $2 WHERE id = $1 RETURNING *', [user.id, JSON.stringify(next)]);
  await event(user.id, 'preferences-updated', req);
  return { user: publicUser(updated) };
});

// -- security and recovery
route('GET', '/api/sessions', 'user', async ({ user, sessionId }) => ({
  sessions: (await q('SELECT id, agent, place, created_at, last_seen FROM sessions WHERE user_id = $1 AND expires_at > now() ORDER BY last_seen DESC', [user.id]))
    .map((s) => ({ id: s.id, agent: s.agent, place: s.place, createdAt: s.created_at, lastSeen: s.last_seen, current: s.id === sessionId })),
}));
route('DELETE', '/api/sessions/:id', 'user', async ({ req, user, params }) => {
  await q('DELETE FROM sessions WHERE id = $1 AND user_id = $2', [params.id, user.id]);
  await event(user.id, 'session-ended', req);
  return { ok: true };
});
route('POST', '/api/sessions/others/revoke', 'user', async ({ req, user, sessionId }) => {
  await q('DELETE FROM sessions WHERE user_id = $1 AND id <> $2', [user.id, sessionId]);
  await event(user.id, 'sessions-revoked', req);
  return { ok: true };
});
route('GET', '/api/security/events', 'user', async ({ user }) => ({
  events: (await q('SELECT kind, detail, place, at FROM events WHERE user_id = $1 ORDER BY at DESC LIMIT 50', [user.id])),
}));
route('POST', '/api/recovery/regenerate', 'user', async ({ req, user, body }) => {
  if (!(await verifySecret(String(body.password || ''), user.password_hash))) fail('That password isn’t right.', 401);
  const key = recoveryKey();
  await q('UPDATE users SET recovery_hash = $2, recovery_created_at = now() WHERE id = $1', [user.id, await hashSecret(key)]);
  await event(user.id, 'recovery-key-created', req);
  await securityAlert(req, user, 'You have a new Poly Account recovery key', 'A new recovery key was made; the old one no longer works.');
  return { recoveryKey: key };
});

// -- devices (the website)
route('GET', '/api/devices', 'user', async ({ user }) => ({
  devices: (await q('SELECT * FROM devices WHERE user_id = $1 ORDER BY last_seen DESC NULLS LAST', [user.id])).map(publicDevice),
}));
async function ownDevice(user, id) {
  if (!/^[0-9a-f-]{36}$/i.test(id)) fail('That computer isn’t in your account.', 404);
  const d = await one('SELECT * FROM devices WHERE id = $1 AND user_id = $2', [id, user.id]);
  if (!d) fail('That computer isn’t in your account.', 404);
  return d;
}
route('GET', '/api/devices/:id', 'user', async ({ user, params }) => {
  const d = await ownDevice(user, params.id);
  const commands = await q('SELECT id, kind, status, detail, created_at, updated_at FROM commands WHERE device_id = $1 ORDER BY created_at DESC LIMIT 20', [d.id]);
  return { device: publicDevice(d), commands };
});
route('PATCH', '/api/devices/:id', 'user', async ({ req, user, params, body }) => {
  const d = await ownDevice(user, params.id);
  const name = body.name === undefined ? d.name : str(body.name, 60);
  if (!name) fail('Give the computer a name.');
  const channel = body.channel === undefined ? d.channel : body.channel;
  if (!releases.CHANNELS.includes(channel)) fail('Choose Stable, Beta or Developer.');
  const updated = await one('UPDATE devices SET name = $2, channel = $3 WHERE id = $1 RETURNING *', [d.id, name, channel]);
  if (name !== d.name) await event(user.id, 'device-renamed', req, { from: d.name, to: name });
  return { device: publicDevice(updated) };
});
route('DELETE', '/api/devices/:id', 'user', async ({ req, user, params }) => {
  const d = await ownDevice(user, params.id);
  await q('DELETE FROM devices WHERE id = $1', [d.id]); // its credential stops working right away
  await event(user.id, 'device-removed', req, { device: d.name });
  return { ok: true };
});
async function queueCommand(req, user, d, kind) {
  if (!COMMANDS.includes(kind)) fail('Unknown action.');
  if (REMOTE_ONLY.has(kind) && !d.remote_management) {
    fail(`Turn on Remote management on “${d.name}” first (Settings › Poly Account on that computer).`, 409);
  }
  await q(`UPDATE commands SET status = 'replaced', updated_at = now() WHERE device_id = $1 AND kind = $2 AND status = 'pending'`, [d.id, kind]);
  const cmd = await one('INSERT INTO commands (device_id, kind) VALUES ($1, $2) RETURNING id, kind, status, created_at', [d.id, kind]);
  await event(user.id, 'remote-command', req, { device: d.name, kind });
  return cmd;
}
route('POST', '/api/devices/:id/commands', 'user', async ({ req, user, params, body }) => {
  const d = await ownDevice(user, params.id);
  return { command: await queueCommand(req, user, d, body.kind) };
});

// -- updates overview
route('GET', '/api/updates', 'user', async ({ user }) => {
  const devices = (await q('SELECT * FROM devices WHERE user_id = $1 ORDER BY name', [user.id])).map(publicDevice);
  const channels = [...new Set(['stable', ...devices.map((d) => d.channel)])];
  const latest = {};
  for (const c of channels) {
    try {
      const r = await releases.latest(c);
      latest[c] = { version: r.version, notes: r.notes, published: r.published };
    } catch (err) {
      latest[c] = { error: err.message };
    }
  }
  return {
    latest,
    devices: devices.map((d) => ({ ...d, updateAvailable: Boolean(latest[d.channel]?.version && releases.newer(latest[d.channel].version, d.version)) })),
  };
});
route('POST', '/api/updates/all', 'user', async ({ req, user }) => {
  const rows = await q('SELECT * FROM devices WHERE user_id = $1 AND remote_management', [user.id]);
  const queued = [];
  for (const d of rows) queued.push((await queueCommand(req, user, d, 'update')).id);
  return { queued: queued.length };
});

// -- linking a computer with a code (the website half)
async function pendingLink(code) {
  const clean = String(code || '').replace(/\D/g, '');
  if (clean.length !== 6) fail('Enter the 6-digit code shown on your computer.');
  const link = await one(`SELECT * FROM device_links WHERE user_code = $1 AND expires_at > now() AND user_id IS NULL ORDER BY created_at DESC LIMIT 1`, [clean]);
  if (!link) fail('That code isn’t right, or it expired. Check the code on your computer.', 404);
  return link;
}
route('POST', '/api/link/lookup', 'user', async ({ user, body }) => {
  await limit(`link:${user.id}`, 15, 15);
  const link = await pendingLink(body.code);
  return { device: { name: deviceName(link.info?.name, link.info), info: cleanInfo(link.info), place: link.place, version: str(link.info?.version, 20) } };
});
route('POST', '/api/link/approve', 'user', async ({ req, user, body }) => {
  await limit(`link:${user.id}`, 15, 15);
  const link = await pendingLink(body.code);
  await q('UPDATE device_links SET user_id = $2 WHERE id = $1', [link.id, user.id]);
  await event(user.id, 'device-link-approved', req, { device: deviceName(link.info?.name, link.info) });
  return { ok: true };
});

// -- PolyOS computers (/api/v1: a stable contract, older PolyOS versions keep using it)
route('POST', '/api/v1/device/start', null, async ({ req, body }) => {
  await limit(`link-start:${sha256(clientIp(req))}`, 20, 60);
  const deviceCode = token('dc_');
  let code = userCode();
  for (let i = 0; i < 5 && await one('SELECT 1 FROM device_links WHERE user_code = $1 AND expires_at > now()', [code]); i += 1) code = userCode();
  const info = { ...cleanInfo(body.info), name: str(body.name, 60), version: str(body.version, 20), arch: str(body.arch, 20) };
  await q(`INSERT INTO device_links (device_code_hash, user_code, info, place, expires_at) VALUES ($1, $2, $3, $4, now() + make_interval(mins => $5))`,
    [sha256(deviceCode), code, JSON.stringify(info), place(req), LINK_MINUTES]);
  return { deviceCode, userCode: code, verificationUrl: `${siteUrl(req)}/link`, interval: 5, expiresIn: LINK_MINUTES * 60 };
});
route('POST', '/api/v1/device/token', null, async ({ req, body }) => {
  const link = await one('SELECT * FROM device_links WHERE device_code_hash = $1', [sha256(str(body.deviceCode, 100))]);
  if (!link || (new Date(link.expires_at) < new Date() && !link.user_id)) return { status: 'expired' };
  if (!link.user_id) return { status: 'pending' };
  if (link.device_id) return { status: 'expired' }; // already handed out once
  const user = await one('SELECT * FROM users WHERE id = $1', [link.user_id]);
  const { device, credential } = await createDevice(req, user, link.info?.name, link.info, link.info);
  await q('UPDATE device_links SET device_id = $2, expires_at = now() WHERE id = $1', [link.id, device.id]);
  return { status: 'approved', ...accountForDevice(user, device, credential) };
});
route('POST', '/api/v1/device/signin', null, async ({ req, body }) => {
  const user = await checkPassword(req, email(body.email), body.password);
  const { device, credential } = await createDevice(req, user, body.name, body.info, body);
  await event(user.id, 'signed-in', req, { agent: 'PolyOS', device: device.name });
  return accountForDevice(user, device, credential);
});
route('POST', '/api/v1/device/register', null, async ({ req, body }) => {
  const { user, recoveryKey: key } = await createUser(req, body);
  const { device, credential } = await createDevice(req, user, body.deviceName, body.info, body);
  return { ...accountForDevice(user, device, credential), recoveryKey: key, emailSent: emailEnabled() };
});
route('POST', '/api/v1/device/checkin', null, async ({ req, body }) => {
  const d = await deviceFromRequest(req);
  const channel = releases.CHANNELS.includes(body.channel) ? body.channel : d.channel;
  const policy = body.policy && typeof body.policy === 'object'
    ? Object.fromEntries(['autoDownload', 'autoInstall', 'askRestart'].map((k) => [k, Boolean(body.policy[k])]).concat([['time', str(body.policy.time, 5)]]))
    : d.policy;
  const remote = body.remoteManagement === undefined ? d.remote_management : Boolean(body.remoteManagement);
  const telemetry = prefsOf({ prefs: d.user_prefs }).telemetry;
  const info = telemetry === 'minimal' ? d.info : { ...d.info, ...cleanInfo(body.info) };
  await q(`UPDATE devices SET last_seen = now(), version = $2, arch = $3, channel = $4, info = $5, remote_management = $6, policy = $7, place = $8 WHERE id = $1`,
    [d.id, str(body.version, 20) || d.version, str(body.arch, 20) || d.arch, channel, JSON.stringify(info),
      remote, JSON.stringify(policy), place(req) || d.place]);
  // Waiting actions from the website; restart, lock and update only while Remote management is on.
  await q(`UPDATE commands SET status = 'expired', updated_at = now() WHERE device_id = $1 AND status = 'pending'
           AND (created_at < now() - interval '1 day' OR (kind <> 'check-updates' AND NOT $2))`, [d.id, remote]);
  const commands = await q(`UPDATE commands SET status = 'sent', updated_at = now() WHERE device_id = $1 AND status = 'pending'
                            RETURNING id, kind, created_at`, [d.id]);
  const [{ rev }] = await q('SELECT coalesce(max(updated_at), to_timestamp(0)) AS rev FROM sync_items WHERE user_id = $1', [d.user_id]);
  return {
    account: { name: d.user_name, email: d.user_email, emailVerified: d.email_verified },
    device: { id: d.id, name: d.name, channel },
    commands,
    terms: { version: TERMS.version, accepted: d.terms_version === TERMS.version },
    sync: { revision: new Date(rev).toISOString(), prefs: prefsOf({ prefs: d.user_prefs }).sync },
    telemetry,
  };
});
route('POST', '/api/v1/device/commands/:id', null, async ({ req, params, body }) => {
  const d = await deviceFromRequest(req);
  const status = ['done', 'failed', 'running'].includes(body.status) ? body.status : 'done';
  await q('UPDATE commands SET status = $3, detail = $4, updated_at = now() WHERE id = $1 AND device_id = $2', [params.id, d.id, status, str(body.detail, 300)]);
  return { ok: true };
});
route('POST', '/api/v1/device/disconnect', null, async ({ req }) => {
  const d = await deviceFromRequest(req);
  await q('DELETE FROM devices WHERE id = $1', [d.id]);
  await event(d.user_id, 'device-removed', req, { device: d.name, by: 'the computer' });
  return { ok: true };
});
const SYNC_KEY = /^[a-z][a-zA-Z0-9._-]{0,63}$/;
route('GET', '/api/v1/sync', null, async ({ req }) => {
  const d = await deviceFromRequest(req);
  const items = await q('SELECT key, value, updated_at FROM sync_items WHERE user_id = $1', [d.user_id]);
  return { items: Object.fromEntries(items.map((i) => [i.key, { value: i.value, updatedAt: i.updated_at }])) };
});
route('PUT', '/api/v1/sync', null, async ({ req, body }) => {
  const d = await deviceFromRequest(req);
  const items = body.items && typeof body.items === 'object' ? Object.entries(body.items) : [];
  if (items.length > 50) fail('Too many settings at once.');
  for (const [key, value] of items) {
    if (!SYNC_KEY.test(key)) fail(`Not a setting name: ${key}`);
    const json = JSON.stringify(value);
    if (json === undefined || json.length > 16 * 1024) fail(`${key} is too large to sync.`);
    await q(`INSERT INTO sync_items (user_id, key, value, device_id, updated_at) VALUES ($1, $2, $3, $4, now())
             ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value, device_id = EXCLUDED.device_id, updated_at = now()`,
    [d.user_id, key, json, d.id]);
  }
  return { ok: true, saved: items.length };
});
// The update check: anyone can ask, with no account and nothing about the computer but its version.
route('GET', '/api/v1/updates/check', null, async ({ url }) => releases.check({
  channel: url.searchParams.get('channel') || 'stable', version: url.searchParams.get('version') || '0.0.0',
  edition: url.searchParams.get('edition') || '',
}));

// ---- the handler --------------------------------------------------------------------------------
function send(res, status, data) {
  if (res.headersSent) return;
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.end(JSON.stringify(data));
}

export async function handle(req, res) {
  const url = new URL(req.url, 'http://localhost');
  // vercel.json rewrites /api/… to this one function as /api/index?__path=/api/…
  const path = (url.searchParams.get('__path') || url.pathname).replace(/\/$/, '');
  url.searchParams.delete('__path');
  try {
    const match = routes.map((r) => ({ r, m: r.method === req.method && r.re.exec(path) })).find((x) => x.m);
    if (!match) {
      const exists = routes.some((r) => r.re.test(path));
      fail(exists ? 'Method not allowed.' : 'Not found.', exists ? 405 : 404);
    }
    const { r, m } = match;
    const params = Object.fromEntries(r.keys.map((k, i) => [k, decodeURIComponent(m[i + 1])]));
    // Changes made with a cookie must come from this site's own pages (custom header = no cross-site forms).
    if (req.method !== 'GET' && (r.auth === 'user' || r.auth === 'csrf') && header(req, 'x-poly') !== '1') {
      fail('Refresh the page and try again.', 403);
    }
    const body = await readBody(req);
    const ctx = { req, res, url, params, body };
    if (r.auth === 'user') {
      const s = await currentSession(req);
      if (!s) fail('Sign in to your Poly Account.', 401);
      ctx.user = s.user;
      ctx.sessionId = s.sessionId;
    }
    const data = await r.fn(ctx);
    if (data !== null) send(res, 200, data);
    else if (!res.headersSent) res.end();
  } catch (err) {
    const status = err.status || 500;
    if (status >= 500) console.error(err);
    send(res, status, { error: status >= 500 && !(err instanceof ApiError) && status !== 503 ? 'Something went wrong on our side. Try again.' : err.message, ...(err.extra || {}) });
  }
}
