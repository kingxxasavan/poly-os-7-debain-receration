// DOM helpers, theming and time formatting shared by every surface.

import { icons } from './icons.js';

// h('button.btn.primary', { onclick }, 'Label', childNode)
export function h(tag, props, ...children) {
  if (props == null || typeof props !== 'object' || props instanceof Node || Array.isArray(props)) {
    children.unshift(props);
    props = null;
  }
  const [name, ...classes] = tag.split('.');
  const el = document.createElement(name || 'div');
  if (classes.length) el.className = classes.join(' ');
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') el.classList.add(...String(value).split(/\s+/).filter(Boolean));
    else if (key === 'style' && typeof value === 'object') Object.assign(el.style, value);
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
    else if (key === 'html') el.innerHTML = value; // trusted, static SVG markup only
    else if (key === 'value' || key === 'checked' || key === 'disabled') el[key] = value;
    else el.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// replaceChildren() that skips null/false (the native one would print "null").
export function fill(el, ...children) {
  el.replaceChildren(...children.flat().filter((c) => c != null && c !== false));
  return el;
}

// Icons are trusted static markup from icons.js.
export function icon(name, ...args) {
  const def = icons[name];
  const span = document.createElement('span');
  span.className = 'ico';
  span.innerHTML = typeof def === 'function' ? def(...args) : def || '';
  return span;
}

export function applyTheme(state) {
  const root = document.documentElement;
  root.dataset.theme = state.settings.theme === 'light' ? 'light' : 'dark';
  root.style.setProperty('--accent', state.settings.accent);
  root.style.setProperty('--glass-a', String(state.settings.glass / 100));
  root.style.setProperty('--panel-h', `${state.env.panelHeight}px`);
  root.style.setProperty('--dock-h', `${state.env.dockHeight}px`);
  root.style.setProperty('--dock-m', `${state.env.dockMargin}px`);
  root.classList.toggle('composited', !!state.env.composited);
  root.classList.toggle('dev', !!state.env.dev);
  root.classList.toggle('lite', state.settings.performanceProfile === 'light'); // the hardware check's light mode
}

export function fmtTime(date, settings, seconds = settings.showSeconds) {
  return date.toLocaleTimeString([], {
    hour: settings.clock24h ? '2-digit' : 'numeric',
    minute: '2-digit',
    second: seconds ? '2-digit' : undefined,
    hour12: !settings.clock24h,
  });
}

export function fmtDate(date, opts = { weekday: 'long', month: 'long', day: 'numeric' }) {
  return date.toLocaleDateString([], opts);
}

// Calls fn now and then on every second (or minute) boundary. Returns a stop function.
export function clockTicker(fn, wantSeconds) {
  let timer = 0;
  const run = () => {
    fn(new Date());
    const now = Date.now();
    const period = wantSeconds() ? 1000 : 60000;
    timer = setTimeout(run, period - (now % period) + 20);
  };
  run();
  return () => clearTimeout(timer);
}

export function throttle(fn, ms) {
  let last = 0;
  let pending = null;
  return (...args) => {
    const now = Date.now();
    clearTimeout(pending);
    if (now - last >= ms) {
      last = now;
      fn(...args);
    } else {
      pending = setTimeout(() => {
        last = Date.now();
        fn(...args);
      }, ms - (now - last));
    }
  };
}

// Accent from a hue (0-360) at the saturation/lightness the default blue uses.
export function hueToHex(hue) {
  const s = 0.61; // saturation/lightness of PolyOS's #678fd9
  const l = 0.63;
  const k = (n) => (n + hue / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return `#${[f(0), f(8), f(4)].map((v) => Math.round(v * 255).toString(16).padStart(2, '0')).join('')}`;
}

export function hexToHue(hex) {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const max = Math.max(r, g, b);
  const d = max - Math.min(r, g, b);
  if (!d) return 0;
  const hue = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return Math.round((hue * 60 + 360) % 360);
}

export function wifiLevel(signal) {
  if (signal == null) return 3;
  return signal >= 70 ? 3 : signal >= 45 ? 2 : signal >= 20 ? 1 : 0;
}

export function networkIcon(net) {
  if (!net || !net.available) return icon('wifiOff');
  if (net.kind === 'ethernet') return icon('ethernet');
  if (net.kind === 'wifi') return icon('wifi', wifiLevel(net.signal));
  return icon('wifiOff');
}

export function volumeIcon(vol) {
  if (!vol || !vol.available || vol.muted || vol.level === 0) return icon('volumeMute');
  return icon('volume', vol.level < 40 ? 1 : 2);
}

export function networkLabel(net) {
  if (!net || !net.available) return 'Network unavailable';
  if (net.kind === 'ethernet') return `Ethernet${net.name ? ` · ${net.name}` : ''}`;
  if (net.kind === 'wifi') return net.name || 'Wi-Fi';
  if (!net.wifiDevice) return 'Not connected';
  return net.wifiEnabled ? 'Not connected' : 'Wi-Fi off';
}

// Disks are sold in powers of 1000, RAM in powers of 1024 (8 GiB of RAM reads as "8 GB").
export function formatBytes(bytes, base = 1000) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let n = bytes;
  while (n >= base && i < units.length - 1) {
    n /= base;
    i += 1;
  }
  const digits = n >= 100 || i === 0 || Number.isInteger(Math.round(n * 10) / 10) ? 0 : 1;
  return `${n.toFixed(digits)} ${units[i]}`;
}

export function greeting(date) {
  const hour = date.getHours();
  if (hour < 5) return 'Good night';
  if (hour < 12) return 'Good morning';
  if (hour < 18) return 'Good afternoon';
  return 'Good evening';
}

// Search apps by name, keywords, description and category, best matches first.
export function searchApps(apps, query) {
  const q = query.trim().toLowerCase();
  if (!q) return apps;
  const scored = [];
  for (const app of apps) {
    const name = app.name.toLowerCase();
    let score = -1;
    if (name.startsWith(q)) score = 0;
    else if (name.split(/[\s\-_.]+/).some((w) => w.startsWith(q))) score = 1;
    else if (name.includes(q)) score = 2;
    else if ((app.keywords || []).some((k) => k.toLowerCase().startsWith(q))) score = 3;
    else if ((app.description || '').toLowerCase().includes(q)) score = 4;
    else if ((app.categories || []).some((c) => c.toLowerCase().startsWith(q))) score = 5;
    if (score >= 0) scored.push([score, app]);
  }
  scored.sort((a, b) => a[0] - b[0] || a[1].name.localeCompare(b[1].name));
  return scored.map(([, app]) => app);
}

// A file's icon: a page with a colored band naming its kind (Files, and the Start menu's Recommended).
const KIND_STYLE = {
  image: ['#4fb6a8', 'IMG'], video: ['#d9608f', 'VID'], audio: ['#9b7fe0', 'AUD'], text: ['#8a8f9c', 'TXT'],
  code: ['#6fbf73', '</>'], pdf: ['#d95c5c', 'PDF'], archive: ['#b0875a', 'ZIP'], package: ['#b0875a', 'DEB'],
  doc: ['#5b8def', 'DOC'], sheet: ['#3fa86b', 'XLS'], slides: ['#e0894f', 'PPT'], disc: ['#7c8595', 'ISO'],
  font: ['#8a8f9c', 'Aa'], file: ['#7c8595', ''],
};

export function fileSvg(kind, name) {
  const [color, label] = KIND_STYLE[kind] || KIND_STYLE.file;
  const ext = label || (name.includes('.') ? name.split('.').pop().slice(0, 4).toUpperCase() : '');
  return '<svg viewBox="0 0 64 64" aria-hidden="true">'
    + '<path d="M14 6h24l14 14v34a4 4 0 0 1-4 4H14a4 4 0 0 1-4-4V10a4 4 0 0 1 4-4z" fill="#eef0f5"/>'
    + '<path d="M38 6l14 14H42a4 4 0 0 1-4-4z" fill="#c9ced9"/>'
    + `<rect x="10" y="38" width="42" height="14" rx="3" fill="${color}"/>`
    + `<text x="31" y="48.5" text-anchor="middle" font-family="Poppins,Inter,sans-serif" font-size="9" font-weight="700" fill="#fff">${ext.replace(/[<>&]/g, '')}</text></svg>`;
}
