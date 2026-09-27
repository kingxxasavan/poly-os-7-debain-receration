// Ask Vara: the PolyOS assistant and agent (chat popup from the Home Menu).
// Vara works in steps (reading files, running commands, rendering models); each shows as a card,
// and anything that changes files or runs programs waits here for Allow or Deny.

import { api, closePopup, on, openSettings } from '../api.js';
import { fill, h, icon } from '../ui.js';

const SUGGESTIONS = [
  'Search the web for today’s top tech news',
  'Remind me in 10 minutes to stretch',
  'Make a 30 mm cube with a 10 mm hole in OpenSCAD',
  'Start a Python project that reads a sensor',
];
const VOICE = { listening: 'Listening…', thinking: 'Working on it…', speaking: 'Speaking…' };

const STATUS = { running: 'Working…', waiting: 'Needs your OK', done: 'Done', failed: 'Didn’t work', denied: 'Declined' };

// A small, safe Markdown subset: ```code blocks```, `inline code`, **bold** and paragraphs.
function markdown(text) {
  const out = [];
  const parts = text.split(/```[^\n]*\n?/);
  parts.forEach((part, i) => {
    if (i % 2) {
      out.push(h('pre.va-code', h('code', part.replace(/\n$/, ''))));
      return;
    }
    for (const para of part.split(/\n{2,}/)) {
      if (!para.trim()) continue;
      const p = h('p');
      for (const bit of para.split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/)) {
        if (!bit) continue;
        if (bit.startsWith('`') && bit.endsWith('`') && bit.length > 2) p.append(h('code', bit.slice(1, -1)));
        else if (bit.startsWith('**') && bit.endsWith('**') && bit.length > 4) p.append(h('b', bit.slice(2, -2)));
        else p.append(bit);
      }
      out.push(p);
    }
  });
  return out;
}

export default function vara(root) {
  root.classList.add('vara');
  const log = h('div.va-log', { role: 'log', 'aria-live': 'polite' });
  const input = h('textarea.va-input', { rows: 1, placeholder: 'Ask Vara anything', autofocus: true, 'aria-label': 'Message Vara' });
  const send = h('button.va-send', { title: 'Send', 'aria-label': 'Send' }, icon('arrowRight'));
  const stopBtn = h('button.icon-btn.round.va-stop', { title: 'Stop', 'aria-label': 'Stop', hidden: true, onclick: stop }, icon('stop'));
  // Vara Voice: push to talk, and what it's doing ("Listening…" with the words as they're heard)
  const mic = h('button.va-mic', { title: 'Talk to Vara (Win+Shift+V)', 'aria-label': 'Talk to Vara', hidden: true,
    onclick: () => api.post('/api/vara/voice/listen', {}).catch(() => {}) }, icon('mic'));
  const voiceBar = h('div.va-voice', { hidden: true, role: 'status' });
  function showVoice(v) {
    mic.hidden = !v || !v.running;
    const label = v && VOICE[v.state];
    voiceBar.hidden = !label;
    mic.classList.toggle('on', v?.state === 'listening');
    if (label) fill(voiceBar, h('span.va-orb', h('i'), h('i'), h('i')), h('b', label), v.text ? h('span', v.text) : null);
  }
  let voice = null;
  const title = h('b', 'Vara');
  api.get('/api/vara/voice').then((v) => { voice = v; showVoice(v); title.textContent = v.name || 'Vara'; }, () => {});
  root.append(
    h('header.va-head',
      h('img.va-logo', { src: '/img/vara.png', alt: '' }),
      h('div.va-title', title, h('small', 'Your PolyOS assistant: voice, web, apps, code, 3D and robots')),
      stopBtn,
      h('button.icon-btn.round', { title: 'Open the HUD (Win+J)', onclick: () => { api.post('/api/hud', { open: true }).catch(() => {}); closePopup(); } }, icon('maximize')),
      h('button.icon-btn.round', { title: 'New chat', onclick: reset }, icon('refresh')),
      h('button.icon-btn.round', { title: 'Vara settings', onclick: () => { openSettings('vara'); closePopup(); } }, icon('settings'))),
    voiceBar,
    log,
    h('div.va-compose', input, mic, send),
  );

  let state = { history: [], busy: false, pending: null };
  let sending = false;
  const open = new Set(); // step cards the person expanded

  function stepCard(item) {
    const expanded = open.has(item.id); // a waiting step's details are in the approval card
    const body = expanded && (item.detail || item.output)
      ? h('div.va-step-body',
        item.detail ? h('pre.va-code', h('code', item.detail)) : null,
        item.output ? h('pre.va-code.out', h('code', item.output)) : null)
      : null;
    return h(`div.va-step.${item.status}`,
      h('button.va-step-head', {
        'aria-expanded': String(expanded),
        onclick: () => { if (open.has(item.id)) open.delete(item.id); else open.add(item.id); render(); },
      },
      icon(item.icon || 'tool'),
      h('span.va-step-title', item.title),
      h('span.va-step-status', STATUS[item.status] || item.status)),
      body);
  }

  function approvalCard(p) {
    const verbs = { write: 'change files', run: 'run a program' };
    return h('div.va-approve', { role: 'alertdialog', 'aria-label': 'Vara needs your approval' },
      h('div.va-approve-head', icon('shield'), h('b', `Vara wants to ${verbs[p.risk] || 'continue'}`)),
      h('p', p.title),
      p.detail ? h('pre.va-code', h('code', p.detail)) : null,
      h('div.va-approve-btns',
        h('button.btn.primary', { onclick: () => decide(p, 'allow') }, 'Allow'),
        h('button.btn', { onclick: () => decide(p, 'always'), title: `Don’t ask again for “${p.label}” in this chat` }, 'Always in this chat'),
        h('button.btn', { onclick: () => decide(p, 'deny') }, 'Deny')));
  }

  function render() {
    const { history, busy, pending } = state;
    stopBtn.hidden = !busy;
    if (!history.length && !busy) {
      fill(log, h('div.va-empty',
        h('img', { src: '/img/vara.png', alt: '' }),
        h('b', 'Hi, I’m Vara.'),
        h('p', 'I can search the web, drive your browser, play music, set reminders and routines, write and run code, make 3D '
          + 'models, work with ROS 2 robots and Arduino boards, and handle PolyOS for you. I ask before I change files or run anything.'
          + (voice?.running ? ' Say “Hey Vera” to talk to me.' : '')),
        h('div.va-chips', SUGGESTIONS.map((s) => h('button.va-chip', { onclick: () => ask(s) }, s)))));
      return;
    }
    const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    fill(log,
      ...history.map((m) => {
        if (m.role === 'step') return stepCard(m);
        if (m.role === 'plan') {
          const done = m.steps.filter((st) => st.status === 'done').length;
          return h('div.va-plan', h('div.va-plan-head', icon('check'), h('b', 'Plan'), h('small', `${done} of ${m.steps.length}`)),
            m.steps.map((st) => h('div.va-plan-step', { class: st.status }, h('i'), h('span', st.step))));
        }
        if (m.role === 'thought') {
          return h('details.va-thought', h('summary', icon('sparkle'), 'Thinking'), h('p', m.content));
        }
        if (m.role === 'user') {
          const badge = { voice: icon('mic'), routine: icon('clock') }[m.source];
          return h('div.va-msg.me', badge ? h('span.va-source', { title: m.source === 'voice' ? 'Spoken' : 'A routine' }, badge) : null, m.content);
        }
        return h('div.va-msg.vara', { class: [m.error ? 'error' : '', m.interim ? 'interim' : ''].join(' ') }, markdown(m.content));
      }),
      pending ? approvalCard(pending) : null,
      busy && !pending ? h('div.va-msg.vara.typing', h('span'), h('span'), h('span')) : null);
    if (nearBottom || pending) log.scrollTop = log.scrollHeight;
  }

  async function refresh() {
    try {
      state = await api.get('/api/vara/history');
    } catch { /* keep what's shown */ }
    render();
  }

  async function ask(text) {
    const message = (text ?? input.value).trim();
    if (!message || state.busy || sending) return;
    input.value = '';
    autosize();
    sending = true;
    state = { ...state, busy: true, history: [...state.history, { role: 'user', content: message }] };
    render();
    log.scrollTop = log.scrollHeight;
    try {
      state = await api.post('/api/vara/chat', { message });
    } catch (err) {
      state = { ...state, busy: false, history: [...state.history, { role: 'assistant', content: err.message, error: true }] };
    }
    sending = false;
    render();
    input.focus();
  }

  async function decide(p, decision) {
    try {
      state = await api.post('/api/vara/approve', { id: p.id, decision });
    } catch { /* already answered elsewhere */ }
    refresh();
  }

  async function stop() {
    state = await api.post('/api/vara/stop', {}).catch(() => state);
    render();
  }

  async function reset() {
    state = await api.post('/api/vara/reset', {}).catch(() => ({ history: [], busy: false, pending: null }));
    open.clear();
    render();
    input.focus();
  }

  function autosize() {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
  }

  input.addEventListener('input', autosize);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      ask();
    }
  });
  send.addEventListener('click', () => ask());

  // Vara works in the background; each step it takes arrives as a "vara" event.
  let queued = false;
  const offs = [
    on('vara', () => {
      if (queued || sending) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; refresh(); });
    }),
    on('connected', refresh),
    on('varaVoice', (e) => { voice = { ...(voice || {}), running: e.state !== 'off', state: e.state, text: e.text }; showVoice(voice); }),
  ];
  refresh();
  return () => offs.forEach((off) => off());
}
