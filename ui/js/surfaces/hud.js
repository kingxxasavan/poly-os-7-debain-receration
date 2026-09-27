// The HUD: the assistant's full-screen interface. It opens when the assistant hears its name (or
// Win+J, or the HUD button in the Vara chat): an arc-reactor circle in the middle that shows whether
// it's listening, thinking or speaking, with the conversation underneath and live panels around it
// (the system, what it knows, what it's doing, what's scheduled). Esc or × closes it; it also closes
// by itself a little while after a spoken conversation ends.

import { api, on } from '../api.js';
import { fill, h, icon } from '../ui.js';

const STATE_LABEL = { off: 'Standing by', idle: 'Online', listening: 'Listening', thinking: 'Working', speaking: 'Speaking' };
const STATUS_DOT = { running: 'run', waiting: 'wait', done: 'ok', failed: 'bad', denied: 'bad' };
const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const SVG = 'http://www.w3.org/2000/svg';

function svg(tag, attrs, ...kids) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  el.append(...kids);
  return el;
}

// The arc reactor: rings that turn at different speeds, tick marks, a glowing core, and a ring of
// "sound" bars that moves while the assistant speaks.
function reactor() {
  const defs = svg('defs', {},
    svg('radialGradient', { id: 'hud-core' },
      svg('stop', { offset: '0%', 'stop-color': '#eafcff' }), svg('stop', { offset: '35%', 'stop-color': '#5fe3ff' }),
      svg('stop', { offset: '70%', 'stop-color': '#0b6fb8', 'stop-opacity': '0.55' }), svg('stop', { offset: '100%', 'stop-color': '#021a33', 'stop-opacity': '0' })),
    svg('filter', { id: 'hud-glow', x: '-50%', y: '-50%', width: '200%', height: '200%' },
      svg('feGaussianBlur', { stdDeviation: '3.2', result: 'b' }),
      svg('feMerge', {}, svg('feMergeNode', { in: 'b' }), svg('feMergeNode', { in: 'SourceGraphic' }))));
  const ring = (cls, r, width, dash, extra = {}) => svg('circle', { class: cls, cx: 250, cy: 250, r, fill: 'none', 'stroke-width': width, 'stroke-dasharray': dash, ...extra });
  const bars = svg('g', { class: 'hud-bars' });
  for (let i = 0; i < 72; i += 1) {
    const a = (i / 72) * Math.PI * 2;
    bars.append(svg('line', { x1: 250 + Math.cos(a) * 128, y1: 250 + Math.sin(a) * 128, x2: 250 + Math.cos(a) * 140, y2: 250 + Math.sin(a) * 140,
      style: `--d:${((i * 37) % 11) / 10}s` }));
  }
  const ticks = svg('g', { class: 'hud-ticks' });
  for (let i = 0; i < 120; i += 1) {
    const a = (i / 120) * Math.PI * 2;
    const long = i % 10 === 0;
    ticks.append(svg('line', { x1: 250 + Math.cos(a) * (long ? 226 : 231), y1: 250 + Math.sin(a) * (long ? 226 : 231),
      x2: 250 + Math.cos(a) * 237, y2: 250 + Math.sin(a) * 237 }));
  }
  return svg('svg', { class: 'hud-reactor', viewBox: '0 0 500 500', 'aria-hidden': 'true' }, defs,
    svg('g', { class: 'spin s1' }, ticks),
    svg('g', { class: 'spin s2' }, ring('arc a1', 212, 3, '180 60 40 60')),
    svg('g', { class: 'spin s3' }, ring('arc a2', 196, 1.2, '2 6')),
    svg('g', { class: 'spin s4' }, ring('arc a3', 180, 9, '70 22 14 22', { filter: 'url(#hud-glow)' })),
    svg('g', { class: 'spin s5' }, ring('arc a4', 162, 2, '120 30 8 30')),
    bars,
    svg('g', { class: 'spin s6' }, ring('arc a5', 112, 4, '40 12', { filter: 'url(#hud-glow)' })),
    svg('circle', { class: 'core-halo', cx: 250, cy: 250, r: 104, fill: 'url(#hud-core)' }),
    svg('circle', { class: 'core', cx: 250, cy: 250, r: 64, fill: 'url(#hud-core)', filter: 'url(#hud-glow)' }));
}

// Falling characters in blue, far in the background (paused when motion is reduced or hidden).
function rain(canvas) {
  const ctx = canvas.getContext('2d');
  const glyphs = 'アイウエオカキクケコサシスセソタチツテトナニヌネノ0123456789ABCDEF<>/=+*'.split('');
  let cols = [];
  const size = 16;
  function resize() {
    canvas.width = innerWidth;
    canvas.height = innerHeight;
    cols = Array.from({ length: Math.ceil(canvas.width / size) }, () => Math.random() * -60);
    ctx.font = `${size - 2}px ui-monospace, monospace`;
  }
  resize();
  addEventListener('resize', resize);
  let last = 0;
  function frame(t) {
    if (!document.hidden && t - last > 55) {
      last = t;
      ctx.fillStyle = 'rgba(0, 6, 16, 0.16)';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      cols.forEach((y, i) => {
        const bright = Math.random() > 0.975;
        ctx.fillStyle = bright ? 'rgba(170, 240, 255, 0.55)' : 'rgba(40, 170, 255, 0.22)';
        ctx.fillText(glyphs[(Math.random() * glyphs.length) | 0], i * size, y * size);
        cols[i] = y * size > canvas.height && Math.random() > 0.97 ? 0 : y + 1;
      });
    }
    if (!REDUCED) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

function panel(title, ...kids) {
  return h('section.hud-panel', h('header', h('span', title), h('i')), ...kids);
}
function meter(label, value, unit = '%') {
  const pct = value == null ? 0 : Math.max(0, Math.min(100, value));
  return h('div.hud-meter', h('div.hud-meter-row', h('span', label), h('b', value == null ? '—' : `${Math.round(value)}${unit}`)),
    h('div.hud-meter-bar', h('span', { style: `width:${pct}%` })));
}
const stat = (label, value) => h('div.hud-stat', h('span', label), h('b', String(value ?? '—')));
const uptime = (s) => (s == null ? '—' : `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`);

export function mount(root) {
  root.className = 'hud';
  document.body.classList.add('hud-page');
  const canvas = h('canvas.hud-rain');
  const clock = h('div.hud-clock');
  const date = h('div.hud-date');
  const stateEl = h('div.hud-state');
  const nameEl = h('div.hud-name');
  const heard = h('div.hud-heard');
  const system = h('div');
  const knowledge = h('div');
  const activity = h('div');
  const schedule = h('div');
  const you = h('p.hud-you');
  const reply = h('p.hud-reply');
  const input = h('input.hud-input', { placeholder: 'Type a command…', autocomplete: 'off', 'aria-label': 'Command' });
  const mic = h('button.hud-mic', { title: 'Talk', 'aria-label': 'Talk', onclick: () => api.post('/api/vara/voice/listen', {}).catch(() => {}) }, icon('mic'));
  const close = () => api.post('/api/hud', { open: false }).catch(() => {});

  root.append(
    canvas, h('div.hud-grid'), h('div.hud-vignette'),
    h('div.hud-top',
      h('div.hud-time', clock, date, h('small', 'All systems nominal')),
      h('button.hud-close', { title: 'Close (Esc)', 'aria-label': 'Close', onclick: close }, icon('close'))),
    h('div.hud-left', panel('System', system), panel('Knowledge', knowledge)),
    h('div.hud-center', h('div.hud-reactor-wrap', reactor(), h('div.hud-center-text', nameEl, stateEl)), heard),
    h('div.hud-right', panel('Activity', activity), panel('Schedule', schedule)),
    h('div.hud-bottom', you, reply, h('form.hud-command', { onsubmit: (e) => { e.preventDefault(); send(); } }, input, mic)),
  );
  rain(canvas);

  let data = null;
  let voice = { state: 'idle', text: '' };
  let quietSince = Date.now();
  let lastState = 'idle';

  function tick() {
    const now = new Date();
    clock.textContent = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    date.textContent = now.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
  }

  function showState() {
    const s = voice.state === 'idle' && data?.busy ? 'thinking' : voice.state || 'idle';
    root.dataset.state = s;
    stateEl.textContent = STATE_LABEL[s] || s;
    heard.textContent = s === 'listening' ? (voice.text ? `“${voice.text}”` : `Say something to ${data?.name || 'me'}…`) : '';
    if (s !== lastState) {
      lastState = s;
      quietSince = Date.now();
    }
  }

  function render() {
    if (!data) return;
    nameEl.textContent = data.name.toUpperCase().split('').join(' ');
    document.title = `${data.name} · HUD`;
    const bat = data.battery;
    const net = data.network;
    fill(system,
      meter('Processor', data.cpu), meter('Memory', data.memory),
      bat && bat.present !== false && bat.level != null ? meter(bat.charging ? 'Battery · charging' : 'Battery', bat.level) : null,
      stat('Network', net ? (net.kind === 'none' || !net.kind ? 'Offline' : net.name || net.kind) : '—'),
      stat('Uptime', uptime(data.uptime)), stat('Cores', data.cores));
    const k = data.knowledge;
    fill(knowledge, stat('Memories', k.notes), stat('Preferences', k.preferences), stat('Documents indexed', k.documents),
      stat('Tools', k.tools), stat('Skills', k.skills),
      stat('Voice', data.voice.running ? `“Hey ${data.wake[0].toUpperCase()}${data.wake.slice(1)}”` : 'Off'));
    const planDone = (data.plan || []).filter((st) => st.status === 'done').length;
    fill(activity, data.pending ? h('div.hud-alert', icon('shield'), h('span', `Needs your OK: ${data.pending.title}`)) : null,
      data.plan && data.plan.length ? h('div.hud-plan', h('div.hud-plan-head', h('span', 'Plan'), h('b', `${planDone}/${data.plan.length}`)),
        data.plan.map((st) => h('div.hud-step', h('i', { class: { done: 'ok', in_progress: 'run' }[st.status] || '' }), h('span', st.step)))) : null,
      data.steps.length ? data.steps.slice().reverse().map((st) => h('div.hud-step', h('i', { class: STATUS_DOT[st.status] || '' }), h('span', st.title)))
        : h('p.hud-empty', 'Nothing running. Ask for something.'));
    const when = (t) => new Date(t * 1000).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' });
    fill(schedule, data.scheduled.length ? data.scheduled.map((it) => h('div.hud-step',
      icon(it.kind === 'routine' ? 'refresh' : 'clock'), h('span', it.text), h('small', when(it.at))))
      : h('p.hud-empty', 'No reminders or routines.'));
    you.textContent = data.you ? `You: ${data.you}` : '';
    reply.textContent = data.reply ? data.reply.replace(/```[\s\S]*?```/g, '[code]').replace(/[*`#]/g, '').slice(0, 400) : '';
    showState();
  }

  async function refresh() {
    try {
      data = await api.get('/api/hud');
      voice = { ...voice, state: data.voice.running ? (data.voice.state || 'idle') : voice.state };
    } catch { /* keep what's shown */ }
    render();
  }

  async function send() {
    const message = input.value.trim();
    if (!message) return;
    input.value = '';
    try {
      await api.post('/api/vara/chat', { message });
    } catch (err) {
      reply.textContent = err.message;
    }
    refresh();
  }

  const offs = [
    on('varaVoice', (e) => { voice = { state: e.state, text: e.text }; showState(); }),
    on('vara', () => refresh()),
  ];
  addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  tick();
  refresh();
  const timers = [
    setInterval(tick, 1000),
    setInterval(refresh, 2500),
    // after a spoken conversation, the HUD steps aside on its own
    setInterval(() => {
      const quiet = root.dataset.state === 'idle' && !data?.busy && document.activeElement !== input && !input.value;
      if (quiet && Date.now() - quietSince > 15000) close();
    }, 1000),
  ];
  input.focus();
  return () => { offs.forEach((off) => off()); timers.forEach(clearInterval); };
}
