// Quick settings: Wi-Fi, airplane mode, energy saver, night light, sound, brightness, volume, battery.

import { api, closePopup, openSettings, saveSettings } from '../api.js';
import { slider, wifiPanel } from '../components.js';
import { h, icon, networkIcon, networkLabel, volumeIcon } from '../ui.js';

const WIFI_HEIGHT = 470;

// The Wi-Fi list is the same popup reopened with data.sub = 'wifi' and a taller size.
const reopen = (store, data, height) =>
  api.post('/api/popup', { view: 'quick', anchorX: store.state.popup?.anchorX ?? null, data, height })
    .catch((err) => console.warn(err.message));

export default function quick(root, store, data) {
  if (data.sub === 'wifi') return wifiList(root, store);
  const sys = () => store.state.system;
  const set = () => store.state.settings;
  const save = (patch) => saveSettings(patch).catch((err) => console.warn(err.message));
  const main = h('div.qs');
  root.append(main);

  // ---- toggles, like Windows 11: a button each, its name underneath ------------------
  function toggle(label, onClick, more) {
    const btn = h('button.qt-btn', { onclick: onClick, 'aria-pressed': 'false' });
    const el = h('div.qt', h('div.qt-pill', btn, more || null), h('span.qt-label', label));
    el.set = (on, ico, disabled = false, sub = label) => {
      el.classList.toggle('on', !!on);
      el.classList.toggle('disabled', !!disabled);
      btn.setAttribute('aria-pressed', String(!!on));
      btn.title = sub;
      btn.replaceChildren(ico);
      el.lastChild.textContent = sub;
    };
    return el;
  }
  const wifiMore = h('button.qt-more', { title: 'Choose a network', onclick: () => reopen(store, { sub: 'wifi' }, WIFI_HEIGHT) }, icon('chevronRight'));
  const wifi = toggle('Wi-Fi', () => {
    const net = sys().network;
    if (net.wifiDevice) api.post('/api/wifi/enabled', { enabled: !net.wifiEnabled }).catch((err) => console.warn(err.message));
  }, wifiMore);
  const airplane = toggle('Airplane mode', () => save({ airplaneMode: !set().airplaneMode }));
  const saver = toggle('Energy saver', () => save({ powerMode: set().powerMode === 'saver' ? 'balanced' : 'saver' }));
  const night = toggle('Night light', () => save({ nightLight: !set().nightLight }));
  const sound = toggle('Sound', () => api.post('/api/volume', { toggleMute: true }));

  // ---- sliders -------------------------------------------------------------------
  const briSlider = slider({ min: 5, label: 'Brightness', onInput: (v) => api.post('/api/brightness', { level: v }) });
  const briRow = h('div.qs-slider', h('span.icon-btn.static', icon('sun')), briSlider);
  const volSlider = slider({ label: 'Volume', onInput: (v) => api.post('/api/volume', { level: v }) });
  const volBtn = h('button.icon-btn', { title: 'Mute', onclick: () => api.post('/api/volume', { toggleMute: true }) });
  const volRow = h('div.qs-slider', volBtn, volSlider);

  // ---- battery -------------------------------------------------------------------
  const battery = h('div.qs-battery');
  const footer = h('div.qs-footer', battery);

  main.append(h('div.qs-toggles', wifi, airplane, saver, night, sound), briRow, volRow, footer);

  function render() {
    const { network, volume, brightness, battery: bat } = sys();
    const s = set();
    const wifiOn = network.available && network.wifiDevice && network.wifiEnabled;
    wifi.set(wifiOn || network.kind === 'ethernet', networkIcon(network), !network.wifiDevice && network.kind !== 'ethernet',
      network.kind === 'ethernet' ? 'Ethernet' : wifiOn && network.name ? network.name : 'Wi-Fi');
    wifiMore.hidden = !network.wifiDevice;
    airplane.set(s.airplaneMode, icon('airplane'));
    saver.set(s.powerMode === 'saver', icon('leaf'));
    night.set(s.nightLight, icon('nightLight'));
    sound.set(volume.available && !volume.muted, volumeIcon(volume), !volume.available, volume.muted ? 'Muted' : 'Sound');

    volRow.hidden = !volume.available;
    volBtn.replaceChildren(volumeIcon(volume));
    volSlider.set(volume.muted ? 0 : volume.level);
    briRow.hidden = !brightness.available;
    briSlider.set(brightness.level);

    footer.hidden = !bat.present;
    battery.replaceChildren(
      ...(bat.present
        ? [icon('battery', bat.level, bat.charging), h('span', `${bat.level}%`), h('small', bat.charging ? 'Charging' : bat.plugged ? 'Plugged in' : 'On battery')]
        : []),
    );
  }

  const unsubscribe = store.subscribe((_s, changed) => {
    if (changed.has('system') || changed.has('settings')) render();
  });
  render();
  return unsubscribe;
}

function wifiList(root, store) {
  const wifi = wifiPanel(store, { compact: true });
  root.append(h('div.qs-sub',
    h('div.qs-sub-head',
      h('button.icon-btn', { title: 'Back', onclick: () => reopen(store, {}, null) }, icon('chevronLeft')),
      h('span', 'Wi-Fi')),
    h('div.qs-sub-body', wifi.el),
    h('div.qs-sub-foot',
      h('button.link-btn', { onclick: () => { openSettings('network'); closePopup(); } }, 'More Wi-Fi settings'))));
  return store.subscribe((_s, changed) => {
    if (changed.has('system')) wifi.update();
  });
}
