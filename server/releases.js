// PolyOS releases: the ISOs, the update packages and their signed manifest live on GitHub
// Releases of RELEASES_REPO (a public repository just for downloads, so the source can be
// private). The website's download buttons and the update check both read it through here.

const DEFAULT_REPO = 'kingxxasavan/poly-os-7-debain-receration';
const CACHE_MS = 5 * 60 * 1000;
const cache = new Map();

export function releasesRepo() {
  return process.env.RELEASES_REPO || DEFAULT_REPO;
}

export const CHANNELS = ['stable', 'beta', 'developer'];

async function github(path) {
  const hit = cache.get(path);
  if (hit && Date.now() - hit.at < CACHE_MS) return hit.data;
  const headers = { Accept: 'application/vnd.github+json', 'User-Agent': 'PolyOS-website' };
  if (process.env.GITHUB_TOKEN) headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
  const res = await fetch(`https://api.github.com/repos/${releasesRepo()}${path}`, { headers });
  if (!res.ok) throw Object.assign(new Error(`Release information isn't available right now (${res.status}).`), { status: 502 });
  const data = await res.json();
  cache.set(path, { at: Date.now(), data });
  return data;
}

function summarize(release) {
  const assets = Object.fromEntries((release.assets || []).map((a) => [a.name, { url: a.browser_download_url, size: a.size }]));
  return {
    version: String(release.tag_name || '').replace(/^v/, ''),
    name: release.name || release.tag_name,
    notes: String(release.body || '').split('<!-- files -->')[0].trim(), // the notes, not the download table
    published: release.published_at,
    prerelease: Boolean(release.prerelease),
    assets,
  };
}

// stable: the newest full release; beta and developer: the newest release of any kind.
export async function latest(channel = 'stable') {
  if (channel === 'stable') {
    try {
      return summarize(await github('/releases/latest'));
    } catch {
      return stableFromManifest(); // GitHub's API is limited per address; the release's own files aren't
    }
  }
  const list = await github('/releases?per_page=10');
  const release = list.find((r) => !r.draft);
  if (!release) throw Object.assign(new Error('No releases yet.'), { status: 404 });
  return summarize(release);
}

export async function history(count = 10) {
  let list;
  try {
    list = await github(`/releases?per_page=${Math.min(30, count)}`);
  } catch {
    const { assets, ...rel } = await stableFromManifest();
    return [{ ...rel, files: Object.keys(assets) }];
  }
  return list.filter((r) => !r.draft).map(summarize).map(({ assets, ...r }) => ({ ...r, files: Object.keys(assets) }));
}

export function versionTuple(v) {
  const m = /^v?(\d+)\.(\d+)\.(\d+)/.exec(String(v || ''));
  return m ? m.slice(1).map(Number) : [0, 0, 0];
}

export function newer(a, b) {
  const x = versionTuple(a);
  const y = versionTuple(b);
  for (let i = 0; i < 3; i += 1) if (x[i] !== y[i]) return x[i] > y[i];
  return false;
}

// Where GitHub itself sends "the newest release's file" (no API call, so no API rate limit).
export function latestFileUrl(name) {
  return `https://github.com/${releasesRepo()}/releases/latest/download/${name}`;
}

// Without the API (GitHub limits it per address, and Vercel's addresses are shared): the stable
// release's own manifest says its version and packages.
async function stableFromManifest() {
  const res = await fetch(latestFileUrl('polyos-update.json'), { headers: { 'User-Agent': 'PolyOS-website' } });
  if (!res.ok) throw Object.assign(new Error('Release information isn’t available right now.'), { status: 502 });
  const manifest = await res.json();
  const assets = { 'polyos-update.json': { url: latestFileUrl('polyos-update.json'), size: 0 },
    'polyos-update.json.sig': { url: latestFileUrl('polyos-update.json.sig'), size: 0 } };
  for (const p of manifest.packages || []) assets[p.file] = { url: latestFileUrl(p.file), size: p.size || 0 };
  return { version: String(manifest.version || ''), name: `PolyOS v${manifest.version}`, notes: manifest.notes || '',
    published: manifest.published || null, prerelease: false, assets };
}

// Which editions a release is for, from its signed manifest's "editions" ([] = everyone).
// Edition releases (e.g. Developer-only) are published as GitHub pre-releases, so the website's
// Download buttons and older PolyOS versions never see them.
export const EDITIONS = ['regular', 'developer', 'gaming'];
const manifests = new Map();
async function releaseEditions(rel) {
  const url = rel.assets['polyos-update.json']?.url;
  if (!url) return [];
  const hit = manifests.get(url);
  if (hit && Date.now() - hit.at < CACHE_MS) return hit.editions;
  let editions = [];
  try {
    const res = await fetch(url, { headers: { 'User-Agent': 'PolyOS-website' } });
    if (res.ok) {
      const m = await res.json();
      editions = Array.isArray(m.editions) ? m.editions.filter((e) => EDITIONS.includes(e)) : [];
    }
  } catch { /* unknown: treat as everyone's */ }
  manifests.set(url, { at: Date.now(), editions });
  return editions;
}

// The newest release this computer should get: its channel's, and for its edition (or everyone's).
async function latestFor(channel, edition) {
  const list = (await github('/releases?per_page=20')).filter((r) => !r.draft).map(summarize);
  for (const rel of list) {
    const editions = await releaseEditions(rel);
    const forMe = !editions.length || editions.includes(edition);
    if (!forMe) continue;
    if (channel === 'stable' && rel.prerelease && !editions.length) continue; // a beta, not an edition release
    return rel;
  }
  throw Object.assign(new Error('No releases yet.'), { status: 404 });
}

// What /api/v1/updates/check answers: only what the updater needs, nothing about the asker. PolyOS
// 1.2.2 and later say their edition; older versions don't, and get everyone's releases only.
export async function check({ channel = 'stable', version = '0.0.0', edition = '' } = {}) {
  const wanted = CHANNELS.includes(channel) ? channel : 'stable';
  let rel;
  if (EDITIONS.includes(edition)) {
    try {
      rel = await latestFor(wanted, edition);
    } catch (err) {
      if (wanted !== 'stable') throw err;
      rel = await latest('stable'); // GitHub's API is busy: everyone's newest release still works
    }
  } else {
    rel = await latest(wanted);
  }
  const manifest = rel.assets['polyos-update.json'];
  const available = newer(rel.version, version);
  return {
    update_available: available && Boolean(manifest),
    version: rel.version,
    notes: rel.notes,
    published: rel.published,
    manifest: manifest?.url || null,
    signature: rel.assets['polyos-update.json.sig']?.url || null,
    size: Object.entries(rel.assets).filter(([n]) => n.endsWith('.deb')).reduce((t, [, a]) => t + (a.size || 0), 0),
    iso_only: available && !manifest,
    mandatory: false,
  };
}
