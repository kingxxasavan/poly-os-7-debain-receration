// Dev harness (python main.py dev): stands in for X11 + openbox around the real UI surfaces.

import { api, connectEvents, on, windowAction, withToken } from './api.js';
import { applyTheme, h, icon } from './ui.js';

const screen = document.getElementById('screen');
const popup = document.getElementById('popup');
const layer = document.getElementById('windows');
const overlay = document.getElementById('overlay');
let state = await api.get('/api/state');
applyTheme(state);
const panelHeight = state.env.panelHeight;
const dockMargin = state.env.dockMargin;

// ---- popup placement (mirrors DesktopShell._show_popup_main) ------------------------
function placePopup(p) {
  if (!p) {
    popup.hidden = true;
    return;
  }
  const W = screen.clientWidth;
  const H = screen.clientHeight;
  popup.classList.toggle('fullscreen', !!p.fullscreen);
  if (p.fullscreen) {
    Object.assign(popup.style, { left: '0px', top: '0px', width: `${W}px`, height: `${H}px` });
  } else if (p.tall) {
    Object.assign(popup.style, { left: '12px', top: '12px', width: `${Math.min(p.width, W - 24)}px`, height: `${H - panelHeight - 16}px` });
  } else {
    const dockX = state.settings.taskbarStyle === 'full' ? 0 : dockMargin;
    const width = Math.min(p.width, W - 24);
    const height = Math.min(p.height, H - panelHeight - 16);
    let x = p.anchorX == null ? 12 : Math.round(dockX + p.anchorX - width / 2);
    if (p.anchorX == null && p.view === 'start' && state.settings.taskbarAlign !== 'left') x = Math.round((W - width) / 2);
    x = Math.max(12, Math.min(x, W - width - 12));
    Object.assign(popup.style, {
      left: `${x}px`, top: `${H - panelHeight - height - 4}px`, width: `${width}px`, height: `${height}px`,
    });
  }
  popup.hidden = false;
  setTimeout(() => popup.contentWindow.focus(), 30);
}

// ---- fake window manager --------------------------------------------------------------
const OWN = { 'polyos-taskmgr.desktop': 'taskmgr', 'polyos-drivers.desktop': 'drivers', 'polyos-store.desktop': 'store',
  'polyos-camera.desktop': 'camera' };
const els = new Map();
const positions = new Map();
let cascade = 0;

function makeWindow(win) {
  const bar = h('div.mock-bar',
    h('img', { src: withToken(win.icon), alt: '' }),
    h('span.mock-title', win.title),
    h('button.mock-btn', { title: 'Minimize', onclick: (e) => { e.stopPropagation(); windowAction(win.xid, 'minimize'); } }, icon('minimize')),
    h('button.mock-btn.close', { title: 'Close', onclick: (e) => { e.stopPropagation(); windowAction(win.xid, 'close'); } }, icon('close')));
  const body = h('div.mock-body');
  if (win.appId === 'polyos-settings.desktop') {
    body.append(h('iframe', { src: `/index.html?surface=settings&page=${encodeURIComponent(win.page || 'appearance')}`, title: 'Settings' }));
  } else if (win.appId === 'polyos-files.desktop') {
    body.append(h('iframe', { src: `/index.html?surface=files&path=${encodeURIComponent(win.page || '')}`, title: 'Files' }));
  } else if (OWN[win.appId]) {
    body.append(h('iframe', { src: `/index.html?surface=${OWN[win.appId]}${win.page ? `&page=${encodeURIComponent(win.page)}` : ''}`, title: win.title,
      allow: 'camera; microphone' }));
  } else {
    const app = state.apps.find((a) => a.id === win.appId);
    body.append(h('div.mock-placeholder', h('img', { src: withToken(win.icon), alt: '' }), h('b', app ? app.name : win.title),
      h('span', 'Stand-in window. On PolyOS this is the real app.')));
  }
  const el = h('div.mock-win', bar, body);
  if (!positions.has(win.xid)) {
    const W = layer.clientWidth;
    const H = layer.clientHeight;
    const settingsApp = win.appId === 'polyos-settings.desktop' || win.appId === 'polyos-files.desktop' || !!OWN[win.appId];
    const w = Math.min(settingsApp ? 1000 : 760, W - 40);
    const hh = Math.min(settingsApp ? 660 : 480, H - 40);
    positions.set(win.xid, { x: Math.max(20, (W - w) / 2 + cascade * 28 - 60), y: Math.max(20, (H - hh) / 2 + cascade * 24 - 40), w, h: hh });
    cascade = (cascade + 1) % 6;
  }
  const pos = positions.get(win.xid);
  Object.assign(el.style, { left: `${pos.x}px`, top: `${pos.y}px`, width: `${pos.w}px`, height: `${pos.h}px` });
  el.addEventListener('pointerdown', () => {
    if (!el.classList.contains('active')) windowAction(win.xid, 'activate');
  });
  bar.addEventListener('pointerdown', (e) => {
    if (e.target.closest('button')) return;
    const start = { x: e.clientX, y: e.clientY, left: el.offsetLeft, top: el.offsetTop };
    bar.setPointerCapture(e.pointerId);
    const move = (ev) => {
      pos.x = start.left + ev.clientX - start.x;
      pos.y = Math.max(0, start.top + ev.clientY - start.y);
      el.style.left = `${pos.x}px`;
      el.style.top = `${pos.y}px`;
    };
    bar.addEventListener('pointermove', move);
    bar.addEventListener('pointerup', () => bar.removeEventListener('pointermove', move), { once: true });
  });
  return el;
}

let z = 1;
function renderWindows(windows) {
  const alive = new Set(windows.map((w) => w.xid));
  for (const [xid, el] of els) {
    if (!alive.has(xid)) {
      el.remove();
      els.delete(xid);
      positions.delete(xid);
    }
  }
  for (const win of windows) {
    let el = els.get(win.xid);
    if (!el) {
      el = makeWindow(win);
      els.set(win.xid, el);
      layer.append(el);
    }
    el.hidden = win.minimized;
    el.classList.toggle('active', win.active);
    if (win.active) el.style.zIndex = String(++z);
  }
}

// The real shell opens setup full screen until it's finished (the installer on the live USB).
let setupFrame = null;
function syncSetup(settings) {
  if (!settings.setupDone && !setupFrame) {
    setupFrame = h('iframe', { id: 'setup', src: '/index.html?surface=setup', title: 'Setup' });
    screen.append(setupFrame);
  } else if (settings.setupDone && setupFrame) {
    setupFrame.remove();
    setupFrame = null;
  }
}

// The lock screen: the real shell shows it over everything and grabs the keyboard.
let lockFrame = null;
on('lock', (e) => {
  if (e.locked && !lockFrame) {
    lockFrame = h('iframe', { id: 'lock', src: '/index.html?surface=lock', title: 'Lock screen' });
    screen.append(lockFrame);
    setTimeout(() => lockFrame?.contentWindow.focus(), 300);
  } else if (!e.locked && lockFrame) {
    setTimeout(() => { lockFrame?.remove(); lockFrame = null; }, 700);
  }
});
on('popup', (e) => placePopup(e.popup));
on('windows', (e) => {
  state.windows = e.windows;
  renderWindows(e.windows);
});
// Taskbar style and auto-hide (mirrors DesktopShell._layout and the openbox margin).
function syncTaskbar(settings) {
  screen.classList.toggle('tb-full', settings.taskbarStyle === 'full');
  screen.classList.toggle('tb-autohide', settings.taskbarAutoHide);
}
on('settings', (e) => { state.settings = e.settings; applyTheme({ ...state, settings: e.settings }); syncSetup(e.settings); syncTaskbar(e.settings); });
on('power', (e) => {
  const labels = { lock: 'Locked', logout: 'Signed out', suspend: 'Sleeping', reboot: 'Restarting…', poweroff: 'Shutting down…', 'restart-shell': 'Restarting the shell…' };
  overlay.replaceChildren(h('div', labels[e.action] || e.action), h('small', 'Simulated in dev mode. Click to return.'));
  overlay.hidden = false;
});
on('connected', async () => {
  state = await api.get('/api/state');
  renderWindows(state.windows);
  placePopup(state.popup);
});
overlay.addEventListener('click', () => { overlay.hidden = true; });
// Clicking anything outside the popup iframe blurs it, which closes it like the real shell.
renderWindows(state.windows);
syncSetup(state.settings);
syncTaskbar(state.settings);
connectEvents();
