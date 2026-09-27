// Gallery filters and a keyboard-friendly lightbox. The page works without this script.

const figures = [...document.querySelectorAll('.gallery figure')];
const tabs = [...document.querySelectorAll('.filters button')];
let visible = figures;

for (const tab of tabs) {
  tab.addEventListener('click', () => {
    const cat = tab.dataset.filter;
    tabs.forEach((t) => t.setAttribute('aria-selected', String(t === tab)));
    figures.forEach((f) => { f.hidden = cat !== 'all' && f.dataset.cat !== cat; });
    visible = figures.filter((f) => !f.hidden);
  });
}

const box = document.querySelector('.lightbox'); // only on the home page
if (box) {
  const boxImg = box.querySelector('img');
  const boxCap = box.querySelector('figcaption');
  let current = 0;
  let opener = null;

  function show(index) {
    current = (index + visible.length) % visible.length;
    const fig = visible[current];
    boxImg.src = fig.dataset.full;
    boxImg.alt = fig.querySelector('b').textContent;
    boxCap.innerHTML = fig.querySelector('figcaption').innerHTML;
  }

  function open(fig) {
    opener = fig;
    show(visible.indexOf(fig));
    box.hidden = false;
    document.body.style.overflow = 'hidden';
    box.querySelector('.lb-close').focus();
  }

  function close() {
    box.hidden = true;
    document.body.style.overflow = '';
    opener?.focus();
  }

  for (const fig of figures) {
    fig.tabIndex = 0;
    fig.setAttribute('role', 'button');
    fig.setAttribute('aria-label', `Open screenshot: ${fig.querySelector('b').textContent}`);
    fig.addEventListener('click', () => open(fig));
    fig.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(fig); } });
  }
  box.querySelector('.lb-close').addEventListener('click', close);
  box.querySelector('.lb-prev').addEventListener('click', () => show(current - 1));
  box.querySelector('.lb-next').addEventListener('click', () => show(current + 1));
  box.addEventListener('click', (e) => { if (e.target === box) close(); });
  document.addEventListener('keydown', (e) => {
    if (box.hidden) return;
    if (e.key === 'Escape') close();
    if (e.key === 'ArrowLeft') show(current - 1);
    if (e.key === 'ArrowRight') show(current + 1);
  });
}

// ---- Download ------------------------------------------------------------------------------
// The buttons link to this site's own /download/pc and /download/arm64, which the Poly Account API
// forwards to the newest release file. The release's details (version, sizes) come from the same
// API (/api/releases/latest), so the site works the same whether the source code is public or not.
const ARCHES = { amd64: ['pc', 'PC (Intel/AMD)'], arm64: ['arm64', 'ARM64'] };

function gb(bytes) {
  return `${(bytes / 1e9).toFixed(bytes >= 1e10 ? 0 : 1)} GB`;
}

function downloadButton(href, arch, text, primary, detail) {
  const a = document.createElement('a');
  a.className = primary ? 'btn primary big' : 'btn big';
  a.href = href;
  a.dataset.arch = arch;
  a.innerHTML = '<svg class="ico"><use href="#i-download"/></svg>';
  a.append(text);
  if (detail) {
    const small = document.createElement('small');
    small.textContent = detail;
    a.append(small);
  }
  return a;
}

async function visitorOnArm() {
  try {
    const hints = await navigator.userAgentData?.getHighEntropyValues(['architecture']);
    return hints?.architecture === 'arm';
  } catch {
    return false;
  }
}

async function showRelease() {
  const buttons = document.querySelector('[data-release-buttons]');
  if (!buttons) return;
  const [release, onArm] = await Promise.all([
    fetch('/api/releases/latest').then((r) => (r.ok ? r.json() : null), () => null),
    visitorOnArm(),
  ]);
  if (!release) {
    if (onArm) buttons.prepend(buttons.querySelector('[data-arch="arm64"]'));
    return;
  }
  const assets = Object.entries(release.assets || {}).map(([name, a]) => ({ name, url: a.url, size: a.size }));
  const builds = Object.entries(ARCHES).map(([arch, [slug, label]]) => {
    const iso = assets.find((a) => a.name === `polyos-${arch}.iso`);
    const parts = assets.filter((a) => a.name.startsWith(`polyos-${arch}.iso.part`)).sort((a, b) => a.name.localeCompare(b.name));
    return { arch, label, iso, parts, href: `/download/${slug}`, size: iso ? iso.size : parts.reduce((n, p) => n + p.size, 0) };
  }).filter((b) => b.iso || b.parts.length || release.oneFile?.[b.arch]);
  if (!builds.length) return;
  if (builds.length > 1 && onArm) builds.reverse(); // ARM64 first on ARM computers
  const date = new Date(release.published).toLocaleDateString([], { year: 'numeric', month: 'long', day: 'numeric' });
  document.querySelector('[data-release-version]').textContent = `v${release.version}`;
  document.querySelector('[data-release-meta]').textContent =
    `Live USB and installer · ${builds.map((b) => b.label).join(' and ')} · released ${date}`;
  // /download/<arch> gives one file even when GitHub has parts (a link set on Vercel, or SourceForge)
  buttons.replaceChildren(...builds.flatMap((b, i) => (b.iso || release.oneFile?.[b.arch]
    ? [downloadButton(b.href, b.arch, `Download for ${b.label}`, i === 0, b.size ? gb(b.size) : '')]
    : b.parts.map((p, n) => downloadButton(p.url, b.arch, `${b.label}, part ${n + 1} of ${b.parts.length}`,
      i === 0 && n === 0, gb(p.size))))));
  if (builds.length < 2) document.querySelector('.dl-which')?.setAttribute('hidden', '');
  if (builds.some((b) => !b.iso && !release.oneFile?.[b.arch])) {
    document.querySelector('[data-release-note]').innerHTML = 'Some downloads come in parts. Download them all, then join them: '
      + '<code>cat polyos-*.part* &gt; polyos.iso</code> (Linux, macOS) or <code>copy /b part0+part1 polyos.iso</code> (Windows).';
  }
  const link = document.querySelector('[data-release-sums]');
  if (link && !release.assets.SHA256SUMS) link.hidden = true;
}
showRelease();

// ---- the install guide, shown when a download starts ------------------------------------------
const guide = document.querySelector('.guide');
let lastDownload = null;

function openGuide(button) {
  lastDownload = button;
  const label = ARCHES[button.dataset.arch]?.[1] || '';
  const size = button.querySelector('small')?.textContent;
  guide.querySelector('[data-guide-file]').textContent = `PolyOS 7 for ${label}${size ? ` · ${size}` : ''}`;
  guide.querySelectorAll('[data-vm]').forEach((li) => { li.hidden = li.dataset.vm !== button.dataset.arch; });
  showTab(button.dataset.arch === 'arm64' ? 'vm' : 'usb');
  guide.showModal();
}

function showTab(name) {
  guide.querySelectorAll('[data-guide-tab]').forEach((t) => t.setAttribute('aria-selected', String(t.dataset.guideTab === name)));
  guide.querySelectorAll('[data-guide-panel]').forEach((p) => { p.hidden = p.dataset.guidePanel !== name; });
}

if (guide) {
  document.querySelector('[data-release-buttons]')?.addEventListener('click', (e) => {
    const button = e.target.closest('a[data-arch]');
    if (button) openGuide(button); // the link itself carries on and starts the download
  });
  guide.querySelectorAll('[data-guide-tab]').forEach((t) => t.addEventListener('click', () => showTab(t.dataset.guideTab)));
  guide.querySelector('.guide-close').addEventListener('click', () => guide.close());
  guide.addEventListener('click', (e) => { if (e.target === guide) guide.close(); });
  guide.querySelector('[data-guide-retry]').addEventListener('click', (e) => {
    e.preventDefault();
    if (lastDownload) window.location.href = lastDownload.href;
  });
}
