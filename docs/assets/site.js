// The site's small interactive parts. Every page works without this script; each part below only
// runs where its elements are. Wrapped in a function so it shares no names with a page's own script.
(() => {
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---- header: the menu on phones, a shadow once scrolled, how far down the page you are -------
  const header = document.querySelector('.nav');
  const menu = document.querySelector('.nav-toggle');
  const siteNav = document.getElementById('site-nav');
  if (menu && siteNav) {
    const setOpen = (open) => { siteNav.classList.toggle('open', open); menu.setAttribute('aria-expanded', String(open)); };
    menu.addEventListener('click', () => setOpen(!siteNav.classList.contains('open')));
    siteNav.addEventListener('click', (e) => { if (e.target.closest('a')) setOpen(false); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setOpen(false); });
  }
  const toTop = document.querySelector('.to-top');
  toTop?.addEventListener('click', () => window.scrollTo({ top: 0, behavior: reduced ? 'auto' : 'smooth' }));
  let ticking = false;
  const onScroll = () => {
    ticking = false;
    const y = window.scrollY;
    const max = document.documentElement.scrollHeight - window.innerHeight;
    header?.classList.toggle('scrolled', y > 8);
    header?.style.setProperty('--progress', max > 0 ? String(Math.min(1, y / max)) : '0');
    if (toTop) toTop.hidden = y < 900;
  };
  window.addEventListener('scroll', () => { if (!ticking) { ticking = true; requestAnimationFrame(onScroll); } }, { passive: true });
  onScroll();

  // On the home page, the header link of the section you're reading lights up.
  const sectionLinks = [...document.querySelectorAll('#site-nav a[href^="/#"]')]
    .map((a) => [a, document.getElementById(a.getAttribute('href').slice(2))]).filter(([, el]) => el);
  if (sectionLinks.length && 'IntersectionObserver' in window) {
    const spy = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        const link = sectionLinks.find(([, el]) => el === entry.target)?.[0];
        if (link && entry.isIntersecting) sectionLinks.forEach(([a]) => a.setAttribute('aria-current', String(a === link)));
      }
    }, { rootMargin: '-45% 0px -50% 0px' });
    sectionLinks.forEach(([, el]) => spy.observe(el));
  }

  // ---- things fade in as you reach them (what's already on screen just stays) --------------------
  const reveals = [...document.querySelectorAll('.reveal')];
  if (reveals.length && 'IntersectionObserver' in window && !reduced) {
    const seen = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        entry.target.classList.remove('pending');
        entry.target.classList.add('in');
        seen.unobserve(entry.target);
      }
    }, { rootMargin: '0px 0px -8% 0px' });
    for (const el of reveals) {
      const box = el.getBoundingClientRect();
      if (box.top < window.innerHeight && box.bottom > 0) continue; // visible already: no flicker
      el.classList.add('pending');
      seen.observe(el);
    }
  }

  // ---- the numbers count up when they come into view ------------------------------------------
  const counters = [...document.querySelectorAll('[data-count]')];
  if (counters.length && 'IntersectionObserver' in window && !reduced) {
    const run = (el) => {
      const target = Number(el.dataset.count);
      const suffix = el.dataset.suffix || '';
      const start = performance.now();
      const step = (now) => {
        const t = Math.min(1, (now - start) / 1200);
        el.textContent = `${Math.round(target * (1 - (1 - t) ** 3))}${suffix}`;
        if (t < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    };
    const watch = new IntersectionObserver((entries) => {
      for (const entry of entries) if (entry.isIntersecting) { run(entry.target); watch.unobserve(entry.target); }
    }, { threshold: 0.6 });
    counters.forEach((el) => watch.observe(el));
  }

  // ---- the hero screenshot leans toward the pointer; feature cards glow where you point ---------
  const tilt = document.querySelector('[data-tilt]');
  if (tilt && !reduced && window.matchMedia('(hover: hover)').matches) {
    tilt.addEventListener('pointermove', (e) => {
      const r = tilt.getBoundingClientRect();
      const x = (e.clientX - r.left) / r.width - 0.5;
      const y = (e.clientY - r.top) / r.height - 0.5;
      tilt.classList.add('tracking');
      tilt.style.setProperty('--ry', `${(x * 10).toFixed(2)}deg`);
      tilt.style.setProperty('--rx', `${(-y * 8).toFixed(2)}deg`);
    });
    tilt.addEventListener('pointerleave', () => {
      tilt.classList.remove('tracking');
      tilt.style.removeProperty('--ry');
      tilt.style.removeProperty('--rx');
    });
  }
  document.querySelectorAll('.card').forEach((card) => card.addEventListener('pointermove', (e) => {
    const r = card.getBoundingClientRect();
    card.style.setProperty('--mx', `${e.clientX - r.left}px`);
    card.style.setProperty('--my', `${e.clientY - r.top}px`);
  }));

  // ---- the lightbox: full-size screenshots, with arrows and the keyboard ------------------------
  const box = document.querySelector('.lightbox');
  let lightbox = null;
  if (box) {
    const img = box.querySelector('img');
    const cap = box.querySelector('figcaption');
    let items = [];
    let at = 0;
    let opener = null;
    const show = (i) => {
      at = (i + items.length) % items.length;
      const it = items[at];
      img.src = it.src;
      img.alt = it.title;
      cap.replaceChildren(Object.assign(document.createElement('b'), { textContent: it.title }), it.text);
    };
    const close = () => {
      box.hidden = true;
      document.body.style.overflow = '';
      opener?.focus();
    };
    lightbox = {
      open(list, index, from) {
        items = list;
        opener = from;
        show(index);
        box.hidden = false;
        document.body.style.overflow = 'hidden';
        box.querySelector('.lb-close').focus();
      },
    };
    box.querySelector('.lb-close').addEventListener('click', close);
    box.querySelector('.lb-prev').addEventListener('click', () => show(at - 1));
    box.querySelector('.lb-next').addEventListener('click', () => show(at + 1));
    box.addEventListener('click', (e) => { if (e.target === box) close(); });
    document.addEventListener('keydown', (e) => {
      if (box.hidden) return;
      if (e.key === 'Escape') close();
      if (e.key === 'ArrowLeft') show(at - 1);
      if (e.key === 'ArrowRight') show(at + 1);
    });
  }

  // ---- the tour: tabs, one big screenshot, a row of small ones ---------------------------------
  const tour = document.querySelector('[data-tour]');
  if (tour) {
    const thumbs = [...tour.querySelectorAll('.tour-thumb')];
    const tabs = [...tour.querySelectorAll('[data-tour-tab]')];
    const big = tour.querySelector('[data-tour-img]');
    const title = tour.querySelector('[data-tour-title]');
    const text = tour.querySelector('[data-tour-text]');
    const count = tour.querySelector('[data-tour-count]');
    const strip = tour.querySelector('.tour-strip');
    let list = [];
    let at = 0;
    const item = (a) => ({ src: a.getAttribute('href'), title: a.dataset.title, text: a.dataset.text });
    const select = (i, scroll = true) => {
      at = (i + list.length) % list.length;
      const a = list[at];
      big.classList.add('swap');
      const next = new Image();
      next.onload = next.onerror = () => {
        big.src = next.src;
        big.alt = a.dataset.title;
        big.classList.remove('swap');
      };
      next.src = a.getAttribute('href');
      title.textContent = a.dataset.title;
      text.textContent = a.dataset.text;
      count.textContent = `${at + 1} / ${list.length}`;
      thumbs.forEach((t) => t.setAttribute('aria-current', String(t === a)));
      if (scroll) { // bring the small one into view in the row (phones) or column (wide screens)
        const vertical = strip.scrollHeight > strip.clientHeight + 4 && getComputedStyle(strip).flexDirection === 'column';
        strip.scrollTo(vertical ? { top: a.offsetTop - strip.offsetTop - 8, behavior: reduced ? 'auto' : 'smooth' }
          : { left: a.offsetLeft - strip.offsetLeft - 8, behavior: reduced ? 'auto' : 'smooth' });
      }
      new Image().src = list[(at + 1) % list.length].getAttribute('href'); // the next one, ready
    };
    const pick = (cat, scroll = true) => {
      tabs.forEach((t) => t.setAttribute('aria-selected', String(t.dataset.tourTab === cat)));
      list = thumbs.filter((t) => t.dataset.cat === cat);
      thumbs.forEach((t) => { t.hidden = t.dataset.cat !== cat; });
      strip.scrollLeft = 0;
      strip.scrollTop = 0;
      select(0, scroll);
    };
    tabs.forEach((t) => t.addEventListener('click', () => pick(t.dataset.tourTab)));
    thumbs.forEach((t) => t.addEventListener('click', (e) => { e.preventDefault(); select(list.indexOf(t)); }));
    tour.querySelector('.tour-arrow.prev').addEventListener('click', () => select(at - 1));
    tour.querySelector('.tour-arrow.next').addEventListener('click', () => select(at + 1));
    tour.querySelector('.tour-zoom').addEventListener('click', (e) => {
      if (lightbox) lightbox.open(list.map(item), at, e.currentTarget);
    });
    tour.addEventListener('keydown', (e) => {
      if (e.target.closest('.tour-tabs')) return;
      if (e.key === 'ArrowLeft') { e.preventDefault(); select(at - 1); }
      if (e.key === 'ArrowRight') { e.preventDefault(); select(at + 1); }
    });
    let startX = null; // swipe on phones
    const screen = tour.querySelector('.tour-screen');
    screen.addEventListener('pointerdown', (e) => { if (e.pointerType !== 'mouse') startX = e.clientX; });
    screen.addEventListener('pointerup', (e) => {
      if (startX === null) return;
      const dx = e.clientX - startX;
      startX = null;
      if (Math.abs(dx) > 40) select(at + (dx < 0 ? 1 : -1));
    });
    pick('desktop', false);
  }

  // ---- Download ------------------------------------------------------------------------------
  // The buttons link to this site's own /download/pc and /download/arm64, which the Poly Account API
  // forwards to the newest release file. The release's details (version, sizes) come from the same
  // API (/api/releases/latest), so the site works the same whether the source code is public or not.
  const ARCHES = { amd64: ['pc', 'PC (Intel/AMD)'], arm64: ['arm64', 'ARM64'] };

  function gb(bytes) {
    return `${(bytes / 1e9).toFixed(bytes >= 1e10 ? 0 : 1)} GB`;
  }

  function downloadButton(href, arch, label, primary, detail) {
    const a = document.createElement('a');
    a.className = primary ? 'btn primary big' : 'btn big';
    a.href = href;
    a.dataset.arch = arch;
    a.innerHTML = '<svg class="ico"><use href="#i-download"/></svg>';
    a.append(label);
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

  function showTab(name) {
    guide.querySelectorAll('[data-guide-tab]').forEach((t) => t.setAttribute('aria-selected', String(t.dataset.guideTab === name)));
    guide.querySelectorAll('[data-guide-panel]').forEach((p) => { p.hidden = p.dataset.guidePanel !== name; });
  }

  function openGuide(button) {
    lastDownload = button;
    const label = ARCHES[button.dataset.arch]?.[1] || '';
    const size = button.querySelector('small')?.textContent;
    guide.querySelector('[data-guide-file]').textContent = `PolyOS 7 for ${label}${size ? ` · ${size}` : ''}`;
    guide.querySelectorAll('[data-vm]').forEach((li) => { li.hidden = li.dataset.vm !== button.dataset.arch; });
    showTab(button.dataset.arch === 'arm64' ? 'vm' : 'usb');
    guide.showModal();
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
})();
