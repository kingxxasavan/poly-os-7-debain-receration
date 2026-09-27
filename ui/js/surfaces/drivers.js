// Driver Manager: this computer's maker and model, firmware updates from that maker (fwupd/LVFS), and
// every part (processor, graphics, Wi-Fi, sound, drives, screens...) with the drivers that make it work best.

import { watchJobs, withAdmin } from '../admin.js';
import { api, power } from '../api.js';
import { fill, h, icon } from '../ui.js';

const KIND_ICON = { graphics: 'monitor', wifi: 'wifi', bluetooth: 'bluetooth', audio: 'volume', network: 'ethernet', firmware: 'chip', touch: 'brush', camera: 'camera',
  cpu: 'chip', memory: 'memory', storage: 'disk', screen: 'laptop' };
const KIND_LABEL = { graphics: 'Graphics', wifi: 'Wi-Fi', bluetooth: 'Bluetooth', audio: 'Sound', network: 'Network', firmware: 'Firmware', touch: 'Touch and pen', camera: 'Camera',
  cpu: 'Processor', memory: 'Memory', storage: 'Storage', screen: 'Screen' };

export function mount(root) {
  root.className = 'drivers';
  document.title = 'Driver Manager';
  const list = h('div.dm-list');
  const banner = h('div.dm-banner', { hidden: true });
  const progress = h('div.dm-progress', { hidden: true });
  const scanBtn = h('button.pill-btn', { onclick: () => scan() }, icon('refresh'), 'Scan again');
  const allBtn = h('button.pill-btn.on', { disabled: true }, icon('download'), 'Install all recommended');
  // This computer (maker, model, BIOS) and firmware updates from its maker (fwupd / LVFS)
  const computer = h('section.dm-computer', { hidden: true });
  const firmware = h('section.dm-firmware');
  root.append(
    h('header.dm-hero',
      h('img', { src: '/img/drivers.svg', alt: '' }),
      h('div', h('h1', 'Driver Manager'), h('p', 'PolyOS finds your hardware and installs the drivers that make it work best.')),
      h('div.dm-actions', scanBtn, allBtn)),
    banner, progress, computer, firmware, list,
    h('p.dm-foot', 'Drivers come from Debian’s archive (including its non-free section for NVIDIA, Broadcom and firmware); firmware updates come from your computer’s maker through the Linux Vendor Firmware Service. Installing needs an internet connection.'),
  );

  let result = null;
  let busy = false;

  function card(dev) {
    const needs = dev.missing.length > 0;
    const btn = needs ? h('button.pill-btn', { disabled: busy, onclick: () => install(dev.missing) }, 'Install') : null;
    return h('article.dm-card', { class: needs ? 'needs' : '' },
      h('span.dm-ico', icon(KIND_ICON[dev.kind] || 'chip')),
      h('div.dm-text',
        h('small.dm-kind', KIND_LABEL[dev.kind] || 'Device'),
        h('b', dev.title),
        h('span.dm-state', needs ? icon('download') : icon('check'),
          needs ? 'Recommended driver available' : dev.driver ? `Working (${dev.driver} driver)` : 'Ready'),
        dev.note && needs ? h('p', dev.note) : null,
        needs ? h('div.dm-pkgs', dev.missing.map((p) => h('code', p))) : null),
      btn);
  }

  function renderComputer() {
    const c = result?.computer;
    if (!c || (!c.maker && !c.model)) { computer.hidden = true; return; }
    computer.hidden = false;
    fill(computer, h('span.dm-ico', icon(c.laptop ? 'laptop' : 'monitor')),
      h('div.dm-text', h('small.dm-kind', 'This computer'), h('b', [c.maker, c.model].filter(Boolean).join(' ')),
        c.bios ? h('span.dm-state', `BIOS/UEFI firmware ${c.bios}${c.biosDate ? ` (${c.biosDate})` : ''}`) : null));
  }

  let fw = null;
  function renderFirmware() {
    if (!fw) {
      fill(firmware, h('div.dm-card', h('span.dm-ico', icon('chip')), h('div.dm-text', h('small.dm-kind', 'Firmware'),
        h('b', 'Checking with your computer’s maker…'))));
      return;
    }
    if (!fw.available) {
      fill(firmware, h('p.dm-note', icon('info'), fw.reason || 'Firmware updates aren’t available on this computer.'));
      return;
    }
    const maker = result?.computer?.maker || 'your computer’s maker';
    fill(firmware, fw.updates.length ? h('article.dm-card.needs', h('span.dm-ico', icon('chip')),
      h('div.dm-text', h('small.dm-kind', `Firmware from ${maker}`),
        h('b', fw.updates.length === 1 ? `${fw.updates[0].device} ${fw.updates[0].version}` : `${fw.updates.length} firmware updates`),
        h('span.dm-state', icon('download'), 'Update available'),
        h('p', 'Published by the maker for this exact model (Linux Vendor Firmware Service). Most install while the computer restarts; keep it plugged in.'),
        h('div.dm-pkgs', fw.updates.map((u) => h('code', `${u.device}: ${u.current} → ${u.version}`)))),
      h('button.pill-btn', { disabled: busy, onclick: installFirmware }, 'Update'))
      : h('article.dm-card', h('span.dm-ico', icon('chip')), h('div.dm-text', h('small.dm-kind', `Firmware from ${maker}`),
        h('b', 'Up to date'), h('span.dm-state', icon('check'), 'No newer firmware for this computer right now'))));
  }
  function loadFirmware() {
    fw = null;
    renderFirmware();
    api.get('/api/drivers/firmware').then((r) => { fw = r; renderFirmware(); }, () => { fw = { available: false }; renderFirmware(); });
  }
  async function installFirmware() {
    try {
      await withAdmin(() => api.post('/api/drivers/firmware', {}),
        { title: 'Update firmware', text: 'Enter your password to install firmware from your computer’s maker.' });
    } catch (err) {
      if (!err.cancelled) showError(err.message);
    }
  }

  function render() {
    if (!result) return;
    renderComputer();
    const pending = [...new Set(result.devices.flatMap((d) => d.missing))];
    allBtn.disabled = busy || !pending.length;
    allBtn.onclick = () => install(pending);
    fill(list, result.devices.length ? result.devices.map(card)
      : h('div.dm-empty', icon('check'), h('b', 'Everything is ready'), h('span', 'No extra drivers are needed for this computer.')));
    if (result.secureBoot && result.devices.some((d) => d.missing.some((p) => p.startsWith('nvidia') || p.includes('dkms')))) {
      list.append(h('p.dm-note', icon('lock'), 'Secure Boot is on. After installing NVIDIA or Broadcom drivers, the next start shows a blue “MOK management” screen: choose Enroll MOK and use the password it asks you to set (or turn Secure Boot off).'));
    }
  }

  async function scan() {
    scanBtn.disabled = true;
    fill(list, h('div.dm-empty', h('img.su-spin', { src: '/img/logo-white.svg', alt: '' }), h('span', 'Checking your hardware…')));
    try {
      result = await api.get('/api/drivers');
      render();
    } catch (err) {
      fill(list, h('div.dm-empty', icon('info'), h('b', 'Couldn’t check the hardware'), h('span', err.message)));
    }
    scanBtn.disabled = false;
  }

  async function install(packages) {
    try {
      await withAdmin(() => api.post('/api/drivers/install', { packages }),
        { title: 'Install drivers', text: 'Enter your password to install drivers.' });
    } catch (err) {
      if (!err.cancelled) showError(err.message);
    }
  }

  function showError(text) {
    banner.hidden = false;
    banner.className = 'dm-banner error';
    fill(banner, icon('info'), h('span', text));
  }

  watchJobs((job) => {
    if (job.kind !== 'drivers') return;
    busy = job.state === 'running';
    progress.hidden = !busy;
    fill(progress, h('span', job.message || 'Working…'), h('div.su-progress', h('span', { style: { width: `${Math.max(3, Math.round(job.progress * 100))}%` } })));
    if (job.state === 'failed') showError(job.error);
    if (job.state === 'done') {
      banner.hidden = false;
      banner.className = 'dm-banner ok';
      fill(banner, icon('check'), h('span', job.restart ? 'Drivers installed. Restart to start using them.' : 'Drivers installed.'),
        job.restart ? h('button.pill-btn.on', { onclick: () => power('reboot') }, 'Restart now') : null);
      scan();
      if (job.target === 'firmware') loadFirmware();
    }
    render();
  });
  scan();
  loadFirmware();
}
