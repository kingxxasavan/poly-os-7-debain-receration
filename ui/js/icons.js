// Line icons on a 24px grid. Stroke uses currentColor so icons follow the text color.

const svg = (body) =>
  `<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="1.8" ` +
  `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;

const speaker = '<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/>';
const dim = (on) => (on ? '' : ' opacity=".28"');

export const icons = {
  search: svg('<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/>'),
  gamepad: svg('<path d="M7.5 7h9a4.5 4.5 0 0 1 4.3 3.2l1.2 4.3a2.6 2.6 0 0 1-4.5 2.4L15.6 15H8.4l-1.9 1.9A2.6 2.6 0 0 1 2 14.5l1.2-4.3A4.5 4.5 0 0 1 7.5 7z"/><path d="M8 9.5v3M6.5 11h3"/><circle cx="15.5" cy="10" r=".9" fill="currentColor" stroke="none"/><circle cx="17.3" cy="12" r=".9" fill="currentColor" stroke="none"/>'),
  code: svg('<path d="m8.5 7.5-4.5 4.5 4.5 4.5"/><path d="m15.5 7.5 4.5 4.5-4.5 4.5"/><path d="m13.5 5-3 14"/>'),
  camera: svg('<path d="M8.5 6.5 10 4.5h4l1.5 2H19a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8.5a2 2 0 0 1 2-2z"/><circle cx="12" cy="12.5" r="3.6"/>'),
  video: svg('<rect x="3" y="6.5" width="12.5" height="11" rx="2.5"/><path d="m15.5 10.5 5-3v9l-5-3"/>'),
  shield: svg('<path d="M12 3.5 19 6v5.5c0 4.3-2.9 7.6-7 9-4.1-1.4-7-4.7-7-9V6z"/><path d="m9 12 2.2 2.2L15.5 10"/>'),
  taskbar: svg('<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M3 15.5h18"/><path d="M9 17.5h6"/>'),
  timer: svg('<circle cx="12" cy="13.5" r="7"/><path d="M12 10v3.5l2.2 2"/><path d="M9.5 3.5h5"/>'),
  mic: svg('<rect x="9" y="3.5" width="6" height="11" rx="3"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0"/><path d="M12 18v2.5"/>'),
  eye: svg('<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>'),
  bolt: svg('<path d="M13 3 5 13.5h6L10 21l8-10.5h-6z"/>'),
  leaf: svg('<path d="M5 19c0-8 5-13.5 15-14-.5 10-6 15-14 15"/><path d="M5 19c3-4 6-6.5 9.5-8.5"/>'),
  flip: svg('<path d="M12 3.5v17"/><path d="M8.5 7 4 17h4.5z"/><path d="M15.5 7 20 17h-4.5z"/>'),
  swap: svg('<path d="M4 9h13.5l-3.5-3.5"/><path d="M20 15H6.5l3.5 3.5"/>'),
  power: svg('<path d="M12 3.5v7.5"/><path d="M6.6 6.9a8 8 0 1 0 10.8 0"/>'),
  lock: svg('<rect x="5" y="10.5" width="14" height="10" rx="2.5"/><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5"/>'),
  logout: svg('<path d="M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4"/><path d="m10 16-4-4 4-4"/><path d="M6 12h10"/>'),
  restart: svg('<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v4.5h-4.5"/>'),
  moon: svg('<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>'),
  settings: svg('<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>'),
  wifi: (level = 3) =>
    svg(
      `<path d="M2.5 9.3a13.5 13.5 0 0 1 19 0"${dim(level >= 3)}/>` +
        `<path d="M5.6 12.6a9 9 0 0 1 12.8 0"${dim(level >= 2)}/>` +
        `<path d="M8.7 15.8a4.6 4.6 0 0 1 6.6 0"${dim(level >= 1)}/>` +
        '<circle cx="12" cy="19" r="1.3" fill="currentColor" stroke="none"/>',
    ),
  wifiOff: svg(
    '<path d="M2.5 9.3a13.5 13.5 0 0 1 19 0" opacity=".28"/><path d="M5.6 12.6a9 9 0 0 1 12.8 0" opacity=".28"/>' +
      '<path d="M8.7 15.8a4.6 4.6 0 0 1 6.6 0" opacity=".28"/><circle cx="12" cy="19" r="1.3" fill="currentColor" stroke="none"/>' +
      '<path d="m3.5 3.5 17 17"/>',
  ),
  ethernet: svg('<rect x="4" y="4" width="16" height="12" rx="2"/><path d="M8.5 16v4h7v-4"/><path d="M9 8v3.5M12 8v3.5M15 8v3.5"/>'),
  volume: (level = 2) =>
    svg(
      speaker +
        `<path d="M15.5 9.5a3.5 3.5 0 0 1 0 5"${dim(level >= 1)}/>` +
        `<path d="M18 7a7 7 0 0 1 0 10"${dim(level >= 2)}/>`,
    ),
  volumeMute: svg(`${speaker}<path d="m16 9.5 5 5M21 9.5l-5 5"/>`),
  battery: (level = 100, charging = false) => {
    const w = Math.max(1.5, (12.5 * Math.min(100, Math.max(0, level))) / 100);
    const fill = charging
      ? '<path d="m11.5 8.8-2.3 3.4h3.6l-2.3 3.4" stroke-width="1.6"/>'
      : `<rect x="4.75" y="9.25" width="${w.toFixed(2)}" height="5.5" rx="1" fill="currentColor" stroke="none"/>`;
    return svg(`<rect x="2.5" y="7" width="17" height="10" rx="2.5"/><path d="M21.5 10.5v3"/>${fill}`);
  },
  sun: svg(
    '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/>',
  ),
  chevronRight: svg('<path d="m9.5 6 6 6-6 6"/>'),
  chevronLeft: svg('<path d="m14.5 6-6 6 6 6"/>'),
  chevronDown: svg('<path d="m6 9.5 6 6 6-6"/>'),
  arrowRight: svg('<path d="M5 12h14M13 6l6 6-6 6"/>'),
  apps: svg(
    [5, 12, 19].flatMap((y) => [5, 12, 19].map((x) => `<circle cx="${x}" cy="${y}" r="1.9" fill="currentColor" stroke="none"/>`)).join(''),
  ),
  chat: svg('<path d="M4 5.5h16a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1h-9l-5 4v-4H4a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1z"/><path d="M8 10.5h8M8 13.5h5"/>'),
  check: svg('<path d="m5 12.5 4.5 4.5L19 7.5"/>'),
  close: svg('<path d="M6 6l12 12M18 6 6 18"/>'),
  pin: svg('<path d="M9 4h6l-1 5 3 3v2H7v-2l3-3z"/><path d="M12 14v6"/>'),
  unpin: svg('<path d="M9 4h6l-1 5 3 3v2H7v-2l3-3z"/><path d="M12 14v6"/><path d="m3.5 3.5 17 17"/>'),
  grid: svg('<rect x="4" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="4" y="13.5" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.5"/>'),
  palette: svg(
    '<path d="M12 3a9 9 0 1 0 0 18c1.2 0 1.8-.9 1.3-2l-.4-.9c-.5-1.1.3-2.1 1.5-2.1H17a4 4 0 0 0 4-4c0-5-4-9-9-9z"/>' +
      '<circle cx="7.5" cy="11" r="1"/><circle cx="10.5" cy="7.5" r="1"/><circle cx="15" cy="8" r="1"/>',
  ),
  monitor: svg('<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>'),
  info: svg('<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.8v.01"/>'),
  user: svg('<circle cx="12" cy="8.5" r="3.5"/><path d="M5 20a7 7 0 0 1 14 0"/>'),
  folder: svg('<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'),
  terminal: svg('<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="m7 10 3 2.5L7 15M12.5 15H17"/>'),
  image: svg('<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><circle cx="9" cy="10" r="1.8"/><path d="m21 16-5-5-9 8.5"/>'),
  plus: svg('<path d="M12 5v14M5 12h14"/>'),
  download: svg('<path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/>'),
  refresh: svg('<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v4.5h-4.5"/>'),
  external: svg('<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>'),
  window: svg('<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M3 9h18"/>'),
  minimize: svg('<path d="M6 12h12"/>'),
  maximize: svg('<rect x="5.5" y="5.5" width="13" height="13" rx="2"/>'),
  disk: svg('<rect x="3.5" y="6" width="17" height="12" rx="2.5"/><path d="M7 14.5h4"/><circle cx="16.5" cy="14.5" r="1" fill="currentColor" stroke="none"/>'),
  bluetooth: svg('<path d="m7 7.5 10 9-5 4.5V3l5 4.5-10 9"/>'),
  widgets: svg('<rect x="3.5" y="3.5" width="7.5" height="9" rx="2"/><rect x="13" y="3.5" width="7.5" height="5" rx="2"/><rect x="13" y="10.5" width="7.5" height="10" rx="2"/><rect x="3.5" y="14.5" width="7.5" height="6" rx="2"/>'),
  usb: svg('<rect x="7" y="9.5" width="10" height="11.5" rx="2.5"/><path d="M9 9.5V3.5h6v6"/><path d="M10.8 6h.01M13.2 6h.01"/>'),
  robot: svg('<rect x="5" y="8" width="14" height="11" rx="3"/><path d="M12 8V4.5M9.5 19v1.5M14.5 19v1.5M3 12.5v3M21 12.5v3"/><circle cx="12" cy="4" r="1"/><circle cx="9.5" cy="13" r="1.1" fill="currentColor" stroke="none"/><circle cx="14.5" cy="13" r="1.1" fill="currentColor" stroke="none"/>'),
  cube: svg('<path d="M12 3.5 19.5 7.7v8.6L12 20.5l-7.5-4.2V7.7z"/><path d="M4.5 7.7 12 12l7.5-4.3M12 12v8.5"/>'),
  chip: svg('<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10 3.5V7M14 3.5V7M10 17v3.5M14 17v3.5M3.5 10H7M3.5 14H7M17 10h3.5M17 14h3.5"/>'),
  laptop: svg('<rect x="5" y="5" width="14" height="10" rx="1.5"/><path d="M2.5 18.5h19"/>'),
  memory: svg('<rect x="3" y="7" width="18" height="9" rx="1.5"/><path d="M7 10v3M11 10v3M15 10v3M6 16v3M10 16v3M14 16v3M18 16v3"/>'),
  activity: svg('<path d="M3 12h4l3-7 4 14 3-7h4"/>'),
  memory: svg('<rect x="3" y="7" width="18" height="10" rx="2"/><path d="M7 7v10M11 7v10M15 7v10M5 20v-3M19 20v-3"/>'),
  bag: svg('<path d="M6 8h12l-1 12H7z"/><path d="M9 8V6.5a3 3 0 0 1 6 0V8"/>'),
  star: svg('<path d="m12 4 2.4 4.9 5.4.8-3.9 3.8.9 5.4-4.8-2.5-4.8 2.5.9-5.4-3.9-3.8 5.4-.8z"/>'),
  globe: svg('<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.5 2.6 3.5 5.4 3.5 8.5s-1 5.9-3.5 8.5c-2.5-2.6-3.5-5.4-3.5-8.5s1-5.9 3.5-8.5z"/>'),
  game: svg('<path d="M7 8h10a4 4 0 0 1 4 4.5l-.6 3.2a2.3 2.3 0 0 1-4 1L15 15H9l-1.4 1.7a2.3 2.3 0 0 1-4-1L3 12.5A4 4 0 0 1 7 8z"/><path d="M8 11v3M6.5 12.5h3M15.5 11.5v.01M17.5 13v.01"/>'),
  music: svg('<path d="M9 18V5.5l11-2V16"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>'),
  clock: svg('<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>'),
  code: svg('<path d="m8.5 7-5 5 5 5M15.5 7l5 5-5 5M13.5 5l-3 14"/>'),
  brush: svg('<path d="M19.5 4.5c-3.5 2-7 5.5-9 9l1.5 1.5c3.5-2 7-5.5 9-9z"/><path d="M10.5 13.5c-2 0-3.5 1.5-3.5 3.5 0 1.2-.8 2-2 2.5 1 .5 2.2.5 3.5.5 2.5 0 4-2 3.5-4.5"/>'),
  briefcase: svg('<rect x="3.5" y="7.5" width="17" height="12" rx="2.5"/><path d="M9 7.5V6a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v1.5M3.5 13h17"/>'),
  tool: svg('<path d="M14.5 6.5a4 4 0 0 0 5 5L11 20a2.1 2.1 0 0 1-3-3l8.5-8.5a4 4 0 0 0-2-2z"/>'),
  trash: svg('<path d="M4.5 7h15M9.5 7V5h5v2M6.5 7l1 13h9l1-13"/>'),
  stop: svg('<circle cx="12" cy="12" r="8.5"/><path d="m8.5 8.5 7 7M15.5 8.5l-7 7"/>'),
  cloud: svg('<path d="M7 18.5h10a4 4 0 0 0 .6-8 5.5 5.5 0 0 0-10.7 1.6A3.3 3.3 0 0 0 7 18.5z"/>'),
  cloudSun: svg('<path d="M9.5 5.2V3.5M4.8 7.3 3.6 6.1M14.2 7.3l1.2-1.2M3.5 12H2"/><path d="M6.4 11.2a3.8 3.8 0 0 1 6.9-2.5"/><path d="M9 20h8.5a3.5 3.5 0 0 0 .5-7 4.8 4.8 0 0 0-9.3 1.3A2.9 2.9 0 0 0 9 20z"/>'),
  rain: svg('<path d="M7 15h10a4 4 0 0 0 .6-8 5.5 5.5 0 0 0-10.7 1.6A3.3 3.3 0 0 0 7 15z"/><path d="m8.5 18-1 2.5M12.5 18l-1 2.5M16.5 18l-1 2.5"/>'),
  snow: svg('<path d="M7 14h10a4 4 0 0 0 .6-8 5.5 5.5 0 0 0-10.7 1.6A3.3 3.3 0 0 0 7 14z"/><path d="M8.5 17.5v.01M12 19v.01M15.5 17.5v.01M10 21v.01M14 21v.01" stroke-width="2.6"/>'),
  storm: svg('<path d="M7 14h10a4 4 0 0 0 .6-8 5.5 5.5 0 0 0-10.7 1.6A3.3 3.3 0 0 0 7 14z"/><path d="m12.5 15.5-2 3.5h3l-2 3.5"/>'),
  fog: svg('<path d="M4 9h16M6 13h12M4 17h16M8 21h8"/>'),
  play: svg('<path d="M8 5.5v13l10.5-6.5z" fill="currentColor"/>'),
  pause: svg('<path d="M8 5.5v13M16 5.5v13" stroke-width="3"/>'),
  sparkle: svg('<path d="M12 3.5 13.8 10 20.5 12l-6.7 2L12 20.5 10.2 14 3.5 12l6.7-2z"/>'),
};
