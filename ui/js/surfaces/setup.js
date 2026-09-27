// PolyOS 7 setup, after the Scratch original: "Cryptic Software presents", the pinwheel falls
// into place, the striped 7 slides in, and the crystal backdrop says welcome. Then:
//   live USB:      It's time to get started (install / dual boot / custom) → Terms → Edition
//                  → Account → Appearance → Where to install → installing → restart
//   first sign-in: Wi-Fi → Drivers → your edition's apps → Vara → Tour → done

import { withAdmin, watchJobs } from '../admin.js';
import { api, launch, on, saveSettings, withToken } from '../api.js';
import { VARA_PROVIDERS, packPanel, providerFor, wifiPanel } from '../components.js';
import { fill, formatBytes, h, hexToHue, hueToHex, icon, networkLabel, throttle } from '../ui.js';

const GB = 1000 ** 3;
const TERMS =
  'BY CLICKING “I AGREE”, YOU AGREE TO THE FOLLOWING TERMS AND CONDITIONS: PolyOS 7 for Debian is presented by ' +
  'Cryptic Software and based on PolyOS, created in Scratch by AndrewInput and PIXAPoLY Software. PolyOS is free ' +
  'software: you may use, copy, change and share it under the GNU General Public License, version 3 or later. It is ' +
  'built on Debian GNU/Linux and includes software from many open-source projects, each under its own license; some ' +
  'drivers and firmware are non-free and are covered by their makers’ licenses. The PolyOS logo, colors and PIXAPoLY ' +
  'artwork come from the PolyOS 7 Scratch project and are shared under CC BY-SA 2.0. “Scratch” is a trademark of the ' +
  'Scratch Foundation; PolyOS is not affiliated with or sponsored by the Scratch Foundation, Debian or any app maker. ' +
  'Installing an operating system changes your disk: back up anything important first. THIS SOFTWARE COMES WITH ' +
  'ABSOLUTELY NO WARRANTY, TO THE EXTENT PERMITTED BY APPLICABLE LAW.';
const FALLBACK_ZONES = ['America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles', 'America/Phoenix',
  'America/Anchorage', 'Pacific/Honolulu', 'America/Toronto', 'America/Mexico_City', 'America/Sao_Paulo', 'Europe/London',
  'Europe/Paris', 'Europe/Berlin', 'Europe/Madrid', 'Africa/Lagos', 'Africa/Johannesburg', 'Asia/Dubai', 'Asia/Kolkata',
  'Asia/Shanghai', 'Asia/Tokyo', 'Asia/Seoul', 'Australia/Sydney', 'UTC'];

// PolyOS editions, chosen while installing. Each one's apps download after installing, once online.
export const EDITIONS = [
  ['regular', 'Regular', 'Everything most people need: the PolyOS desktop, Firefox, Files and PolyMarket.', 'star'],
  ['developer', 'Developer', 'Change PolyOS itself, and get coding tools: Git, Python and its libraries, Node.js, VS Code, Docker and Blender.', 'code'],
  ['gaming', 'Gaming', 'Steam, Wine for Windows games, Heroic, Lutris, cloud gaming, drivers and Game Mode, set up for play.', 'gamepad'],
];
// Custom install: what an existing partition can become (value -> [label, mount, erase]).
const ROLES = {
  keep: ['Keep as it is', null, false],
  root: ['PolyOS (/) · erase', '/', true],
  'home-keep': ['Your files (/home) · keep what’s on it', '/home', false],
  'home-format': ['Your files (/home) · erase', '/home', true],
  'storage-keep': ['Extra storage · keep what’s on it', 'storage', false],
  'storage-format': ['Extra storage · erase', 'storage', true],
  efi: ['EFI boot partition (/boot/efi)', '/boot/efi', false],
  swap: ['Swap · erase', 'swap', true],
};
const LINUX_FS = ['ext4', 'ext3', 'ext2', 'btrfs', 'xfs', 'f2fs'];
// partition types the drive screen names (GPT type GUIDs, lowercase as lsblk prints them)
const MSR_GUID = 'e3c9e316-0b5c-4db8-817d-f92df00215ae';
const WINRE_GUID = 'de94bba4-06d1-4d40-a16a-bfd50179d6ac';
const BIOS_BOOT_GUID = '21686148-6449-6e6f-744e-656564454649';
const READABLE_FS = [...LINUX_FS, 'ntfs', 'vfat', 'exfat'];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const reduceMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function timeZones() {
  try {
    const list = Intl.supportedValuesOf('timeZone');
    if (list && list.length) return list;
  } catch { /* older engines */ }
  return FALLBACK_ZONES;
}

function guessZone() {
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return tz && tz !== 'UTC' && tz !== 'Etc/UTC' ? tz : 'America/New_York';
}

function usernameFrom(name) {
  const first = (name.trim().split(/\s+/)[0] || '').normalize('NFKD').replace(/[^\w]/g, '').toLowerCase().replace(/_/g, '');
  const clean = first.replace(/[^a-z0-9]/g, '').slice(0, 24);
  return clean ? (/^[a-z]/.test(clean) ? clean : `u${clean}`) : '';
}

export function mount(root, store) {
  const live = !!store.state.env.live;
  root.className = 'setup';
  const bg = h('div.su-bg');
  const scrim = h('div.su-scrim');
  const intro = h('div.su-intro');
  const card = h('section.su-card');
  const pills = h('div.su-pills');
  const stage = h('div.su-stage', card);
  root.append(bg, scrim, intro, stage, pills);
  bg.style.backgroundImage = `url("${withToken('/wallpaper/builtin/polyos-crystal.jpg')}")`;

  const plan = {
    mode: 'pick', disk: null, size: null, hostname: '', timezone: guessZone(), edition: 'regular',
    pick: null, // the drive screen: { kind: 'part' | 'space', disk, device | start }
    wipe: {}, roles: {}, // custom mode: drive -> what it's erased for; partition -> ROLES key
    user: { fullName: '', username: '', password: '' },
    appearance: { theme: store.state.settings.theme || 'dark', accent: store.state.settings.accent },
    vara: false, // the Developer edition's question: Vara, the voice assistant, installed after PolyOS
    varaConfig: null, // { provider, endpoint, model, apiKey } for Vara's AI, if given here
  };
  let probe = null;        // disks from /api/install/probe (fetched while the person fills in the rest)
  let probeError = null;
  let usernameEdited = false;
  let hostnameEdited = false;
  let installJob = null;
  let progressEls = null; // the install progress screen, updated in place
  let wifi = null;

  // ---- intro ---------------------------------------------------------------------------
  async function playIntro() {
    const skip = { done: false };
    const skipper = (e) => {
      if (e.type === 'keydown' && !['Enter', ' ', 'Escape'].includes(e.key)) return;
      skip.done = true;
    };
    root.addEventListener('pointerdown', skipper);
    document.addEventListener('keydown', skipper);
    const wait = async (ms) => {
      const end = Date.now() + (reduceMotion() ? ms / 4 : ms);
      while (Date.now() < end && !skip.done) await sleep(40);
    };
    const name = (store.state.user.fullName || '').split(' ')[0];
    const presents = h('div.in-presents', h('span', 'Cryptic Software'), h('small', 'presents'));
    const logo = h('img.in-logo', { src: '/img/logo-white.svg', alt: '' });
    const seven = h('img.in-seven', { src: '/img/seven.svg', alt: '' });
    const mark = h('div.in-mark', logo, seven);
    const hello = h('div.in-hello', h('h1', 'Welcome to PolyOS 7'),
      h('p', live ? 'We’re glad you’re here.' : `We’re glad you’re here${name ? `, ${name}` : ''}.`));
    fill(intro, presents, mark, hello);
    root.classList.add('intro');
    if (live) {
      presents.classList.add('show');
      await wait(2300);
      presents.classList.remove('show');
      await wait(700);
    }
    mark.classList.add('fall');
    await wait(1100);
    mark.classList.add('landed', 'seven');
    await wait(1300);
    root.classList.add('crystal');
    mark.classList.add('up');
    await wait(900);
    hello.classList.add('show');
    await wait(1400);
    hello.classList.add('second');
    await wait(1700);
    root.removeEventListener('pointerdown', skipper);
    document.removeEventListener('keydown', skipper);
    root.classList.add('crystal');
    root.classList.remove('intro');
    intro.classList.add('gone');
    setTimeout(() => intro.remove(), 700);
  }

  // ---- steps ---------------------------------------------------------------------------
  // Everything is asked on the USB drive, before installing, so the installed PolyOS starts straight
  // to the desktop: drivers and edition apps install by themselves after the restart (firststart.py).
  const installSteps = [start, check, terms, edition, account, appearance, connect, polyAccount, target, installing];
  // Only for a PolyOS installed some other way (the advanced installer): the same questions, once.
  const welcomeSteps = [check, connect, polyAccount, drivers, editionApps, done];
  const steps = live ? installSteps : welcomeSteps;
  let step = 0;

  function go(n) {
    step = Math.max(0, Math.min(steps.length - 1, n));
    wifi = null;
    progressEls = null;
    card.classList.remove('enter', 'light');
    void card.offsetWidth;
    card.classList.add('enter');
    card.classList.toggle('light', live && plan.appearance.theme === 'light' && steps[step] === appearance);
    fill(card, ...steps[step](), h('img.su-seven', { src: '/img/seven.svg', alt: '' }));
    const count = live ? installSteps.length - 1 : steps.length;
    fill(pills, ...Array.from({ length: count }, (_, i) => h('span', { class: i < step ? 'done' : i === step ? 'on' : '' })));
    pills.hidden = live && step >= count;
    card.querySelector('[autofocus]')?.focus();
  }

  const head = (title, sub) => [h('h1', title), sub ? h('p.su-sub', sub) : null];
  const back = () => h('button.su-back', { onclick: () => go(step - 1), title: 'Back' }, icon('chevronLeft'), 'Back');
  const next = (label = 'Next', onclick = () => go(step + 1), opts = {}) =>
    h('button.su-next', { onclick, disabled: opts.disabled, class: opts.primary ? 'primary' : '' }, label);
  const nav = (...right) => h('div.su-nav', step > 0 ? back() : h('span'), h('div.su-nav-right', ...right));
  const option = (title, sub, trailing, onclick) => h('button.su-option', { onclick },
    h('span.su-option-text', h('b', title), h('small', sub)), trailing);

  // live USB --------------------------------------------------------------------------
  function start() {
    const extra = [];
    extra.push(h('button.su-link', { onclick: tryFirst }, 'Try PolyOS first'));
    if (store.state.env.installer) {
      extra.push(h('span.su-dot', '·'), h('button.su-link', { onclick: () => launch(store.state.env.installer) }, 'Advanced installer'));
    }
    return [
      ...head('It’s time to get started.', 'Pick an option to continue installation or dual boot.'),
      h('div.su-options',
        option('Install PolyOS 7', 'Choose a drive or partition, like Windows Setup.', h('img', { src: '/img/logo-white.svg', alt: '' }),
          () => { plan.mode = 'pick'; go(1); }),
        option('Dual boot', 'Next to Windows or Linux: PolyOS makes room by itself.', h('span.su-dual', icon('window'), icon('window')),
          () => { plan.mode = 'alongside'; go(1); })),
      h('div.su-foot', ...extra),
    ];
  }

  function terms() {
    return [
      ...head('Terms and conditions', 'Agree to the terms and conditions to continue installation.'),
      h('div.su-terms', { tabindex: '0' }, h('p', TERMS),
        h('small', '*PolyOS™ is a trademark of PIXAPoLY Software. The full license texts are in /usr/share/common-licenses on the installed system.')),
      nav(next('I Agree', () => go(step + 1), { primary: true })),
    ];
  }

  // The Developer edition asks one more thing: Vara, the voice and agent assistant ("vara" view),
  // then its AI provider ("varaKey"). Everyone else goes straight on.
  let editionView = 'pick';
  function edition() {
    if (plan.edition === 'developer' && editionView === 'vara') return varaOffer();
    if (plan.edition === 'developer' && editionView === 'varaKey') return varaKey();
    const cards = EDITIONS.map(([id, name, text, ico]) => h('button.su-edition', {
      class: plan.edition === id ? 'on' : '', role: 'radio', 'aria-checked': String(plan.edition === id),
      onclick: () => { plan.edition = id; go(step); },
    }, h('span.su-edition-ico', icon(ico)), h('b', name), h('small', text), plan.edition === id ? h('span.su-edition-check', icon('check')) : null));
    return [
      ...head('How will you use PolyOS?', 'Pick an edition. You can add the others later in Settings.'),
      h('div.su-editions', { role: 'radiogroup', 'aria-label': 'Edition' }, cards),
      plan.edition === 'regular' ? null : h('p.su-note', icon('info'),
        'Its apps download by themselves after installing, once you’re online.'),
      nav(next('Next', () => {
        if (plan.edition === 'developer') { editionView = 'vara'; go(step); } else { plan.vara = false; go(step + 1); }
      })),
    ];
  }

  // inside this step, Back goes to the step's previous screen
  const subNav = (prev, ...right) => h('div.su-nav', h('button.su-back', { onclick: prev, title: 'Back' }, icon('chevronLeft'), 'Back'),
    h('div.su-nav-right', ...right));

  function varaOffer() {
    const choose = (yes) => { plan.vara = yes; editionView = yes ? 'varaKey' : 'pick'; if (yes) go(step); else go(step + 1); };
    return [
      h('div.su-vara-head', h('img', { src: '/img/vara.png', alt: '' }),
        h('div', ...head('Would you like Vara?', 'Our assistant, for the Developer edition: voice and agent, working for you around the clock.'))),
      h('ul.su-bullets',
        h('li', h('b', 'Talk to it: '), '“Hey Vera”, then ask. Vara answers out loud, and “stop” interrupts it.'),
        h('li', h('b', 'It does things: '), 'drives your browser, opens apps, plays music, changes settings, searches the web and reads pages.'),
        h('li', h('b', 'It remembers: '), 'what matters to you, plus reminders and routines that run on their own.'),
        h('li', h('b', 'It builds: '), 'code, 3D models and robot projects with you, and asks before changing anything.')),
      h('div.su-options',
        option('Yes, set up Vara', 'Installs after PolyOS (about 300 MB, with its own web browser). Speech is recognized on this computer; it only listens for “Hey Vera”.',
          h('span.su-dual', icon('mic')), () => choose(true)),
        option('Not now', 'Vara stays a chat in the dock. Settings › Vara adds the voice any time.',
          h('span.su-dual', icon('chat')), () => choose(false))),
      subNav(() => { editionView = 'pick'; go(step); }),
    ];
  }

  let varaProvider = VARA_PROVIDERS[0];
  function varaKey() {
    const key = h('input.su-input', { type: 'password', placeholder: 'Paste your API key', autocomplete: 'off', 'aria-label': 'API key',
      value: plan.varaConfig?.apiKey || '' });
    const label = h('span');
    const hint = h('small');
    const chips = h('div.su-chips', VARA_PROVIDERS.map((p) => h('button.su-chip', { onclick: () => pick(p) }, p.label)));
    function pick(p) {
      varaProvider = p;
      label.textContent = `${p.label} API key`;
      hint.textContent = `${p.keyHint} Other providers are in Settings › Vara.`;
      chips.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.textContent === p.label));
    }
    pick(varaProvider);
    const done = (withKey) => {
      plan.varaConfig = withKey && key.value.trim() ? { provider: providerFor(varaProvider.endpoint), endpoint: varaProvider.endpoint,
        model: varaProvider.model, apiKey: key.value.trim() } : null;
      editionView = 'pick';
      go(step + 1);
    };
    return [
      ...head('Connect Vara’s brain', 'Vara thinks with the AI model you choose, using your own key. Simple things like “open Firefox” or “volume 40” work without one.'),
      chips,
      h('label.su-field.wide', label, key, hint),
      subNav(() => { editionView = 'vara'; go(step); }, h('button.su-link', { onclick: () => done(false) }, 'Add it later'),
        next('Next', () => done(true), { primary: true })),
    ];
  }

  function account() {
    const err = h('div.su-error', { hidden: true });
    const name = h('input.su-input', { value: plan.user.fullName, placeholder: 'Your name', autocomplete: 'name', autofocus: true, maxlength: 80 });
    const user = h('input.su-input', { value: plan.user.username, placeholder: 'username', autocomplete: 'username', spellcheck: 'false', maxlength: 32 });
    const pass = h('input.su-input', { type: 'password', value: plan.user.password, placeholder: 'Type a password', autocomplete: 'new-password' });
    const confirm = h('input.su-input', { type: 'password', value: plan.user.password, placeholder: 'Type it again', autocomplete: 'new-password' });
    const host = h('input.su-input', { value: plan.hostname, placeholder: 'computer-name', spellcheck: 'false', maxlength: 63 });
    const zone = h('select.su-input', timeZones().map((z) => h('option', { value: z, selected: z === plan.timezone }, z.replace(/_/g, ' '))));
    name.addEventListener('input', () => {
      plan.user.fullName = name.value;
      if (!usernameEdited) user.value = plan.user.username = usernameFrom(name.value);
      if (!hostnameEdited) host.value = plan.hostname = user.value ? `${user.value}-polyos` : '';
    });
    user.addEventListener('input', () => {
      usernameEdited = true;
      user.value = user.value.toLowerCase().replace(/[^a-z0-9_-]/g, '');
      plan.user.username = user.value;
      if (!hostnameEdited) host.value = plan.hostname = user.value ? `${user.value}-polyos` : '';
    });
    host.addEventListener('input', () => {
      hostnameEdited = true;
      host.value = host.value.toLowerCase().replace(/[^a-z0-9-]/g, '');
      plan.hostname = host.value;
    });
    zone.addEventListener('change', () => { plan.timezone = zone.value; });
    const nextBtn = next('Next', () => {
      const problem = !plan.user.fullName.trim() ? 'Enter your name.'
        : !/^[a-z_][a-z0-9_-]{0,31}$/.test(plan.user.username) ? 'Usernames start with a letter and use lowercase letters, numbers, - and _.'
          : pass.value !== confirm.value ? 'The passwords don’t match.'
            : !/^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/.test(plan.hostname) ? 'Computer names use letters, numbers and hyphens.' : null;
      if (problem) {
        err.textContent = problem;
        err.hidden = false;
        return;
      }
      plan.user.password = pass.value;
      go(step + 1);
    });
    const blank = h('p.su-note', { hidden: !!plan.user.password }, icon('info'),
      'With no password, PolyOS signs you in automatically and anyone using this computer can change it.');
    pass.addEventListener('input', () => { blank.hidden = !!pass.value; });
    const field = (label, input, hint) => h('label.su-field', h('span', label), input, hint ? h('small', hint) : null);
    return [
      ...head('Create your account', 'Choose a name and a password for your desktop. If you want, you can leave the password blank.'),
      h('div.su-form',
        field('Your name', name),
        field('Username', user, 'For signing in. Lowercase, no spaces.'),
        field('Password', pass),
        field('Confirm password', confirm),
        field('Computer name', host),
        field('Time zone', zone)),
      blank, err,
      nav(nextBtn),
    ];
  }

  function appearance() {
    const pick = async (theme) => {
      plan.appearance.theme = theme;
      card.classList.toggle('light', theme === 'light');
      darkBtn.classList.toggle('on', theme === 'dark');
      lightBtn.classList.toggle('on', theme === 'light');
      await saveSettings({ theme }).catch(() => {});
    };
    const darkBtn = h('button.su-theme.dark', { class: plan.appearance.theme === 'dark' ? 'on' : '', onclick: () => pick('dark'), 'aria-label': 'Dark' }, icon('moon'), h('span', 'Dark'));
    const lightBtn = h('button.su-theme.light', { class: plan.appearance.theme === 'light' ? 'on' : '', onclick: () => pick('light'), 'aria-label': 'Light' }, icon('sun'), h('span', 'Light'));
    const hue = h('input.range.hue-range.su-hue', { type: 'range', min: 0, max: 359, value: hexToHue(plan.appearance.accent), 'aria-label': 'Accent color' });
    const send = throttle((v) => saveSettings({ accent: v }).catch(() => {}), 200);
    hue.addEventListener('input', () => {
      plan.appearance.accent = hueToHex(Number(hue.value));
      document.documentElement.style.setProperty('--accent', plan.appearance.accent);
      send(plan.appearance.accent);
    });
    return [
      ...head('Appearance', 'Customize whether dark or light theme should be used, and UI accent color. This can be changed anytime.'),
      h('div.su-themes', darkBtn, lightBtn),
      h('div.su-accent', h('span', 'Accent color'), hue),
      nav(next()),
    ];
  }

  function loadProbe() {
    if (probe || probeError === 'loading') return;
    probeError = 'loading';
    api.get('/api/install/probe').then((res) => {
      probe = res;
      probeError = null;
      if (steps[step] === target) go(step);
    }, (err) => {
      probeError = err.message;
      if (steps[step] === target) go(step);
    });
  }

  function target() {
    const erase = plan.mode === 'erase';
    const title = erase ? 'Where should PolyOS go?' : 'Make room for PolyOS';
    const sub = erase ? 'Choose the disk to install PolyOS on. Everything on it will be replaced.'
      : 'PolyOS will sit next to your other system. You’ll pick which one to start each time you turn on the computer.';
    if (!probe) {
      if (probeError && probeError !== 'loading') {
        return [...head(title, sub), h('div.su-error', probeError),
          nav(next('Try again', () => { probeError = null; loadProbe(); go(step); }))];
      }
      loadProbe();
      return [...head(title, sub), h('div.su-wait', h('img.su-spin', { src: '/img/logo-white.svg', alt: '' }), 'Looking at your disks…'), nav(h('span'))];
    }
    if (plan.mode === 'custom') return customTarget();
    if (plan.mode === 'pick') return drivesScreen();
    const usable = probe.disks.filter((d) => (erase ? d.canErase : d.alongside.possible));
    if (!usable.length) {
      const reasons = probe.disks.filter((d) => !d.isLive).map((d) => h('li', h('b', `${d.model} (${formatBytes(d.size)})`), ': ',
        erase ? 'too small or read-only.' : d.alongside.reason));
      return [...head(title, erase ? 'PolyOS couldn’t find a disk to install on.' : 'PolyOS can’t fit next to your other system yet.'),
        h('ul.su-reasons', reasons.length ? reasons : [h('li', 'No internal disk was found.')]),
        nav(erase ? h('span') : h('button.su-link', { onclick: () => { plan.mode = 'pick'; go(step); } }, 'Choose a partition or space'),
          erase ? null : next('Fresh install instead', () => { plan.mode = 'erase'; go(step); })),
        erase ? null : bitlockerMessage()];
    }
    if (!usable.some((d) => d.path === plan.disk)) {
      const internal = usable.filter((d) => !d.removable);
      plan.disk = (erase ? (internal[0] || usable[0]) : usable[0]).path;
      plan.size = null;
    }
    const disk = () => usable.find((d) => d.path === plan.disk);
    const understood = h('input', { type: 'checkbox' });
    const install = next('Install', () => startInstall(), { primary: true, disabled: true });
    understood.addEventListener('change', () => { install.disabled = !understood.checked; });
    const list = h('div.su-disks', usable.map((d) => h('button.su-disk', {
      class: d.path === plan.disk ? 'on' : '',
      onclick: () => { plan.disk = d.path; plan.size = null; go(step); },
    }, h('span.su-disk-ico', icon(d.transport === 'usb' || d.removable ? 'download' : 'disk')),
    h('span.su-disk-text', h('b', d.model), h('small', `${formatBytes(d.size)} · ${d.transport ? d.transport.toUpperCase() : 'disk'}${d.oses.length ? ` · ${d.oses.join(', ')}` : ''}`)),
    d.path === plan.disk ? icon('check') : null)));
    const body = [list];
    const d = disk();
    if (erase) {
      body.push(h('p.su-warn', icon('info'), d.oses.length
        ? `${d.oses.join(' and ')} and all files on ${d.model} will be erased.`
        : `All files on ${d.model} will be erased.`));
      body.push(h('label.su-check', understood, h('span', 'I understand that this disk will be erased.')));
    } else {
      const opt = d.alongside;
      plan.size = plan.size || opt.suggested;
      const other = d.oses[0] || 'Your other system';
      const range = h('input.range.su-size', { type: 'range', min: opt.minBytes, max: opt.maxBytes, step: GB, value: plan.size, 'aria-label': 'Space for PolyOS' });
      const labels = h('div.su-split-labels');
      const bar = h('div.su-split-bar', h('span.su-split-other'), h('span.su-split-poly'));
      const total = opt.kind === 'shrink' ? (d.partitions.find((p) => p.path === opt.partition)?.size || opt.maxBytes) : opt.maxBytes;
      const paint = () => {
        plan.size = Number(range.value);
        const pct = Math.max(8, Math.min(92, (plan.size / total) * 100));
        bar.style.setProperty('--poly', `${pct}%`);
        range.style.setProperty('--pct', `${((plan.size - opt.minBytes) * 100) / Math.max(1, opt.maxBytes - opt.minBytes)}%`);
        fill(labels,
          h('span', h('b', other), opt.kind === 'shrink' ? ` keeps ${formatBytes(total - plan.size)}` : ' stays as it is'),
          h('span', h('b', 'PolyOS'), ` gets ${formatBytes(plan.size)}`));
      };
      range.addEventListener('input', paint);
      paint();
      body.push(h('div.su-split', bar, labels, range));
      body.push(h('label.su-check', understood, h('span', 'I’ve backed up my important files.')));
      if (opt.kind === 'shrink') body.push(h('p.su-note', icon('info'), `${other} will be shrunk to make room. This can take a while.`));
    }
    if (probe.uefi && probe.secureBoot) body.push(h('p.su-note', icon('lock'), 'Secure Boot is on. PolyOS supports it.'));
    // Made a partition for PolyOS in Windows (a D: drive, say)? Pick it on the drive screen instead.
    const pickInstead = erase ? h('span') : h('button.su-link', { onclick: () => { plan.mode = 'pick'; go(step); } }, 'Choose a partition instead (like a D: drive)');
    return [...head(title, sub), ...body, nav(pickInstead, install), erase ? null : bitlockerMessage()];
  }

  // ---- "Where do you want to install PolyOS?": every drive's partitions and unallocated space,
  // with Delete and New (applied right away, like Windows Setup); pick one and install there.
  let driveBusy = null;   // "Deleting…" while a change is being made
  let driveError = null;
  let newFormFor = null;  // the unallocated space whose "New" size form is open
  let confirm = null;     // { text, detail, action, label }: the delete / erase question

  function partType(p) {
    if (p.esp) return 'System (EFI)';
    if (p.parttype === MSR_GUID) return 'Microsoft reserved';
    if (p.parttype === WINRE_GUID) return 'Recovery';
    if (p.parttype === BIOS_BOOT_GUID) return 'BIOS boot';
    if (p.fstype === 'swap') return 'Swap';
    const fs = { ntfs: 'NTFS', vfat: 'FAT32', exfat: 'exFAT' }[p.fstype] || p.fstype || 'Unformatted';
    return p.os ? `${p.os} · ${fs}` : fs;
  }

  function driveRows() {
    const drives = probe.disks.filter((d) => !d.isLive);
    return drives.map((d, n) => {
      const items = [
        ...d.partitions.map((p) => ({ kind: 'part', key: p.path, disk: d, n, p, start: p.start ?? 0, bytes: p.size,
          name: `Drive ${n} Partition ${p.number}${p.label || p.os ? `: ${p.label || p.os}` : ''}`, type: partType(p), install: p.install })),
        ...d.free.map((r) => ({ kind: 'space', key: `${d.path}@${r.start}`, disk: d, n, start: r.start, bytes: r.bytes,
          name: `Drive ${n} Unallocated Space`, type: '', install: r.install })),
      ].sort((a, b) => a.start - b.start);
      return { d, n, items };
    });
  }

  function pickedItem(groups) {
    const all = groups.flatMap((g) => g.items);
    const pk = plan.pick;
    if (pk) {
      const found = all.find((it) => it.disk.path === pk.disk && (pk.kind === 'part'
        ? (pk.device ? it.p?.path === pk.device : it.kind === 'part' && it.start >= pk.start && !it.p.esp)
        : it.kind === 'space' && it.start <= pk.start && pk.start < it.start + it.bytes / (it.disk.sector || 512) + 1));
      if (found) return found;
    }
    // nothing chosen yet: the biggest unallocated space PolyOS fits in, preferring internal drives
    const spaces = all.filter((it) => it.kind === 'space' && it.install?.possible)
      .sort((a, b) => (a.disk.removable - b.disk.removable) || (b.bytes - a.bytes));
    return spaces[0] || null;
  }

  async function changeDrive(label, body, hint) {
    driveBusy = label;
    driveError = null;
    newFormFor = null;
    confirm = null;
    go(step);
    try {
      await withAdmin(() => api.post('/api/install/disk', body),
        { title: 'Change your drives', text: 'Enter the password to change partitions.' });
      plan.pick = hint;
    } catch (err) {
      if (!err.cancelled) driveError = err.message;
    }
    driveBusy = null;
    probe = null;
    loadProbe();
    go(step);
  }

  function drivesScreen() {
    const title = 'Where do you want to install PolyOS?';
    const sub = 'Choose a partition or unallocated space. Delete and New change your drives right away.';
    if (driveBusy) {
      return [...head(title, sub), h('div.su-wait', h('img.su-spin', { src: '/img/logo-white.svg', alt: '' }), driveBusy), nav(h('span'))];
    }
    const groups = driveRows();
    const sel = pickedItem(groups);
    if (sel) plan.pick = sel.kind === 'part' ? { kind: 'part', disk: sel.disk.path, device: sel.p.path } : { kind: 'space', disk: sel.disk.path, start: sel.start };
    const select = (it) => { plan.pick = it.kind === 'part' ? { kind: 'part', disk: it.disk.path, device: it.p.path } : { kind: 'space', disk: it.disk.path, start: it.start }; newFormFor = null; driveError = null; go(step); };

    const table = h('div.su-ptable', { role: 'listbox', 'aria-label': 'Drives and partitions' },
      h('div.su-prow.su-phead', { 'aria-hidden': 'true' }, h('span', 'Name'), h('span', 'Total size'), h('span', 'Type')),
      groups.length ? groups.flatMap(({ d, n, items }) => [
        h('div.su-pdrive', icon(d.transport === 'usb' || d.removable ? 'usb' : 'disk'),
          h('b', `Drive ${n}`), h('span', `${d.model} · ${formatBytes(d.size)}${d.oses.length ? ` · ${d.oses.join(', ')}` : ''}`)),
        ...items.map((it) => h('button.su-prow', {
          role: 'option', 'aria-selected': String(it === sel), class: [it === sel ? 'on' : '', it.kind, it.install?.possible ? '' : 'no'].join(' '),
          onclick: () => select(it), ondblclick: () => { select(it); if (it.install?.possible) installHere(it); },
        }, h('span.su-pname', icon(it.kind === 'space' ? 'plus' : 'disk'), it.name), h('span', formatBytes(it.bytes)), h('span', it.type))),
      ]) : h('p.su-note', 'No drive was found. Connect one, then press Refresh.'));

    const canDelete = sel && sel.kind === 'part';
    const canNew = sel && sel.kind === 'space' && !sel.install?.reason?.includes('running from');
    const tools = h('div.su-ptools',
      h('button.su-ptool', { onclick: () => { probe = null; loadProbe(); go(step); } }, icon('refresh'), 'Refresh'),
      h('button.su-ptool', { disabled: !canDelete, onclick: () => askDelete(sel) }, icon('trash'), 'Delete'),
      h('button.su-ptool', { disabled: !canNew, onclick: () => { newFormFor = sel.key; go(step); } }, icon('plus'), 'New'),
      h('button.su-link.su-padv', { onclick: () => { plan.mode = 'custom'; go(step); } }, 'Advanced setup'));

    let form = null;
    if (sel && newFormFor === sel.key) {
      const maxGb = Math.max(1, Math.floor(sel.bytes / GB));
      const size = h('input.su-input.su-psize', { type: 'number', min: 1, max: maxGb, value: maxGb, 'aria-label': 'Size in GB' });
      form = h('div.su-pnew', h('span', 'Size:'), size, h('span', `GB of ${maxGb} GB`),
        h('button.su-next.primary', { onclick: () => {
          const gb = Math.min(maxGb, Math.max(1, Number(size.value) || maxGb));
          changeDrive('Creating the partition…', { action: 'new', disk: sel.disk.path, start: sel.start, size: gb === maxGb ? sel.bytes : gb * GB },
            { kind: 'part', disk: sel.disk.path, start: sel.start });
        } }, 'Apply'),
        h('button.su-link', { onclick: () => { newFormFor = null; go(step); } }, 'Cancel'));
    }

    let status;
    if (driveError) status = h('div.su-error', driveError);
    else if (!sel) status = h('p.su-note', icon('info'), 'Select a partition or unallocated space for PolyOS.');
    else if (!sel.install?.possible) status = h('p.su-warn', icon('info'), sel.install?.reason || 'PolyOS can’t be installed here.');
    else if (sel.kind === 'space') {
      status = h('p.su-note.ok', icon('check'), sel.disk.partitions.length
        ? `PolyOS will use this unallocated space (${formatBytes(sel.bytes)}). Nothing else on the drive changes.${sel.install.newEsp ? ' It also adds a 512 MB EFI system partition.' : ''}`
        : `PolyOS will set up this whole drive (${formatBytes(sel.bytes)}).`);
    } else {
      status = h('p.su-warn', icon('info'), `Everything on this partition will be erased${sel.p.os ? `, including ${sel.p.os}` : ''}.`);
    }

    const overlay = confirm ? h('div.su-confirm', { role: 'alertdialog', 'aria-label': confirm.text },
      h('div.su-confirm-box', h('b', confirm.text), h('p', confirm.detail),
        h('div.su-confirm-btns', h('button.su-link', { onclick: () => { confirm = null; go(step); } }, 'Cancel'),
          h('button.su-next.primary.danger', { onclick: confirm.action }, confirm.label)))) : null;

    const install = next('Next', () => sel && installHere(sel), { primary: true, disabled: !sel?.install?.possible });
    setTimeout(() => table.querySelector('.su-prow.on')?.scrollIntoView({ block: 'nearest' }), 0); // the chosen row stays in sight
    if (probe.uefi && probe.secureBoot) tools.append(h('span.su-psb', icon('lock'), 'Secure Boot is on. PolyOS supports it.'));
    return [...head(title, sub), table, tools, form, status, nav(install), overlay || bitlockerMessage()];
  }

  // The system message when a drive is encrypted with BitLocker: PolyOS can't go next to Windows
  // until it's turned off (it can't shrink the partition, and Windows would ask for its recovery key).
  let bitlockerSeen = false;
  function bitlockerMessage() {
    if (!probe?.bitlocker?.length || bitlockerSeen) return null;
    const drives = probe.bitlocker.map((v) => `${v.label ? `“${v.label}”, ` : ''}the ${formatBytes(v.size)} partition on ${v.model}`).join('; ');
    return h('div.su-confirm', { role: 'alertdialog', 'aria-label': 'BitLocker is on' },
      h('div.su-confirm-box.su-sysmsg',
        h('div.su-sysmsg-head', icon('lock'), h('b', 'BitLocker is on')),
        h('p', `PolyOS can’t be installed next to Windows while BitLocker encrypts ${probe.bitlocker.length > 1 ? 'these partitions' : 'this partition'}: ${drives}. Turn it off first:`),
        h('ol.su-steps',
          h('li', 'Start Windows. Make sure you have your BitLocker recovery key (aka.ms/myrecoverykey).'),
          h('li', 'Open Settings › Privacy & security › Device encryption and turn it off, or Control Panel › BitLocker Drive Encryption › Turn off BitLocker, for every drive.'),
          h('li', 'Wait until Windows says decryption is complete. It can take an hour or more; keep the computer plugged in.'),
          h('li', 'Turn off Fast Startup too (Control Panel › Power Options › Choose what the power buttons do), then shut down.'),
          h('li', 'Start from this USB drive again and choose Dual boot.')),
        h('p', 'Installing PolyOS on a whole drive (erasing it) or on the encrypted partition itself still works.'),
        h('div.su-confirm-btns', h('button.su-next.primary', { onclick: () => { bitlockerSeen = true; go(step); } }, 'OK'))));
  }

  function askDelete(it) {
    const what = it.p.os ? ` This deletes ${it.p.os}.` : '';
    const system = it.p.esp ? ' Other systems on this computer may stop starting.' : '';
    confirm = { text: `Delete ${it.name}?`, label: 'Delete',
      detail: `Everything stored on this partition (${formatBytes(it.bytes)}) will be lost.${what}${system}`,
      action: () => changeDrive('Deleting the partition…', { action: 'delete', disk: it.disk.path, number: it.p.number },
        { kind: 'space', disk: it.disk.path, start: it.start }) };
    go(step);
  }

  function installHere(it) {
    if (it.kind === 'part') {
      confirm = { text: `Erase ${it.name} and install PolyOS there?`, label: 'Erase and install',
        detail: `Everything on it (${formatBytes(it.bytes)}${it.p.os ? `, including ${it.p.os}` : ''}) will be erased. Other partitions stay as they are.`,
        action: () => { confirm = null; startInstall(); } };
      go(step);
      return;
    }
    startInstall();
  }

  // what the drive screen's choice means for the installer
  function pickPayload() {
    const pk = plan.pick;
    const d = probe.disks.find((x) => x.path === pk.disk);
    if (pk.kind === 'part') return { mode: 'custom', disk: pk.disk, wipe: {}, mounts: [{ device: pk.device, mount: '/', format: true }] };
    if (!d || !d.partitions.length) return { mode: 'erase', disk: pk.disk };
    return { mode: 'space', disk: pk.disk, start: pk.start };
  }

  // Custom: every drive and partition, and what each one becomes.
  function customLayout() {
    const wipe = {};
    const mounts = [];
    const names = new Set();
    const storageName = (label) => {
      const base = (label || 'data').toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^[-_]+|[-_]+$/g, '').slice(0, 24) || 'data';
      let name = base;
      for (let i = 2; names.has(name); i += 1) name = `${base}-${i}`;
      names.add(name);
      return `/mnt/${name}`;
    };
    for (const d of probe.disks) {
      const use = plan.wipe[d.path];
      if (use) wipe[d.path] = use === 'storage' ? storageName(d.model) : use;
    }
    for (const d of probe.disks) {
      if (plan.wipe[d.path]) continue;
      for (const p of d.partitions) {
        const role = plan.roles[p.path];
        if (!role || role === 'keep') continue;
        const [, where, erase] = ROLES[role];
        mounts.push({ device: p.path, mount: where === 'storage' ? storageName(p.label || p.os) : where, format: erase });
      }
    }
    return { wipe, mounts };
  }

  function customProblem({ wipe, mounts }) {
    const targets = [...Object.values(wipe), ...mounts.filter((m) => m.mount !== 'swap').map((m) => m.mount)];
    const roots = targets.filter((t) => t === '/').length;
    if (!roots) return 'Choose a drive or partition for PolyOS itself.';
    if (roots > 1) return 'Only one drive or partition can hold PolyOS.';
    const twice = targets.find((t, i) => targets.indexOf(t) !== i);
    if (twice) return `Two places are set to ${twice}.`;
    if (mounts.filter((m) => m.mount === 'swap').length > 1) return 'Choose at most one swap partition.';
    if (probe.uefi && !Object.values(wipe).includes('/') && !mounts.some((m) => m.mount === '/boot/efi')
        && !probe.disks.some((d) => !wipe[d.path] && d.partitions.some((p) => p.esp))) {
      return 'This computer needs an EFI boot partition. Choose one, or erase a whole drive for PolyOS.';
    }
    return null;
  }

  function customTarget() {
    const disks = probe.disks.filter((d) => !d.isLive && !d.readonly);
    const summary = h('ul.su-summary');
    const err = h('div.su-error', { hidden: true });
    const understood = h('input', { type: 'checkbox' });
    const install = next('Install', () => startInstall(), { primary: true, disabled: true });
    const refresh = () => {
      const layout = customLayout();
      const problem = customProblem(layout);
      err.textContent = problem || '';
      err.hidden = !problem;
      const lines = [];
      for (const d of disks) {
        const use = layout.wipe[d.path];
        if (use) lines.push(h('li.erase', icon('trash'), `Erase ${d.model} (${formatBytes(d.size)}) for ${use === '/' ? 'PolyOS' : use}`));
        for (const p of d.partitions) {
          const m = layout.mounts.find((x) => x.device === p.path);
          const name = `${p.label || p.os || p.path.replace('/dev/', '')} (${formatBytes(p.size)})`;
          if (m) lines.push(h('li', { class: m.format ? 'erase' : 'use' }, icon(m.format ? 'trash' : 'check'),
            `${m.format ? 'Erase' : 'Use'} ${name} as ${m.mount === '/' ? 'PolyOS (/)' : m.mount}`));
          else if (!use && (p.os || p.fstype)) lines.push(h('li.keep', icon('lock'), `Keep ${name}${p.os ? `: ${p.os}` : ''}`));
        }
      }
      const rank = (li) => (li.classList.contains('erase') ? 0 : li.classList.contains('use') ? 1 : 2);
      lines.sort((x, y) => rank(x) - rank(y)); // erasing first, so it can't be missed
      fill(summary, lines.length ? lines : h('li.keep', 'Nothing is chosen yet.'));
      install.disabled = !!problem || !understood.checked;
    };
    understood.addEventListener('change', refresh);
    const cards = disks.map((d) => {
      const whole = h('select.su-input.su-role', { 'aria-label': `Use ${d.model} for` },
        [['', d.partitions.length ? 'Choose per partition' : 'Don’t use'], ['/', 'Erase · install PolyOS here'],
          ['/home', 'Erase · your files (/home)'], ['storage', 'Erase · extra storage']]
          .map(([v, t]) => h('option', { value: v, selected: (plan.wipe[d.path] || '') === v, disabled: v === '/' && !d.canErase }, t)));
      whole.addEventListener('change', () => {
        if (whole.value) plan.wipe[d.path] = whole.value;
        else delete plan.wipe[d.path];
        go(step);
      });
      const parts = plan.wipe[d.path] ? [] : d.partitions.map((p) => {
        const fs = p.fstype || '';
        const allowed = Object.keys(ROLES).filter((r) => r === 'keep'
          || (r === 'root' && p.size >= probe.minBytes && !p.esp)
          || (r === 'home-keep' && LINUX_FS.includes(fs))
          || (r === 'home-format' && !p.esp)
          || (r === 'storage-keep' && READABLE_FS.includes(fs) && !p.esp)
          || (r === 'storage-format' && !p.esp)
          || (r === 'efi' && p.esp)
          || (r === 'swap' && !p.esp && p.size <= 64 * GB));
        const select = h('select.su-input.su-role', { 'aria-label': `Use ${p.path} for` },
          allowed.map((r) => h('option', { value: r, selected: (plan.roles[p.path] || 'keep') === r }, ROLES[r][0])));
        select.addEventListener('change', () => { plan.roles[p.path] = select.value; refresh(); });
        return h('div.su-part',
          h('span.su-part-text', h('b', p.label || p.os || p.path.replace('/dev/', '')),
            h('small', [formatBytes(p.size), fs || 'empty', p.os, p.esp ? 'EFI' : null].filter(Boolean).join(' · '))),
          select);
      });
      return h('div.su-drive',
        h('div.su-drive-head', h('span.su-disk-ico', icon(d.transport === 'usb' || d.removable ? 'download' : 'disk')),
          h('span.su-disk-text', h('b', d.model), h('small', `${formatBytes(d.size)}${d.oses.length ? ` · ${d.oses.join(', ')}` : ''}`)),
          whole),
        ...parts);
    });
    refresh();
    return [
      ...head('Choose drives and partitions', 'Decide what each drive is for. Anything you leave as “Keep” isn’t touched.'),
      h('div.su-drives', cards),
      h('div.su-plan', h('b', 'What will happen'), summary),
      err,
      h('label.su-check', understood, h('span', 'I’ve backed up my files and checked the list above.')),
      nav(install),
    ];
  }

  async function startInstall() {
    const payload = { mode: plan.mode, disk: plan.disk, hostname: plan.hostname, timezone: plan.timezone,
      user: plan.user, appearance: plan.appearance, edition: plan.edition,
      vara: plan.edition === 'developer' && plan.vara, varaConfig: plan.edition === 'developer' && plan.vara ? plan.varaConfig : null,
      profile: profile || hardware?.profile || null, background: hardware?.background || null, drivers: recommended || [],
      ...(plan.mode === 'alongside' ? { size: plan.size } : {}),
      ...(plan.mode === 'custom' ? customLayout() : {}),
      ...(plan.mode === 'pick' ? pickPayload() : {}) };
    installJob = { state: 'running', progress: 0, message: 'Getting ready for installation…' };
    go(steps.indexOf(installing));
    try {
      installJob = await withAdmin(() => api.post('/api/install/start', { plan: payload }));
    } catch (err) {
      installJob = { state: 'failed', error: err.message };
    }
    go(steps.indexOf(installing));
  }

  function installing() {
    const job = installJob || { state: 'running', progress: 0, message: 'Getting ready for installation…' };
    if (job.state === 'done') {
      const restart = h('button.su-next.primary', {
        onclick: () => {
          restart.disabled = true;
          restart.textContent = 'Restarting…';
          api.post('/api/install/restart', {}).catch((err) => { restart.disabled = false; restart.textContent = err.message; });
        },
      }, 'Restart now');
      const later = plan.edition !== 'regular' || (recommended && recommended.length);
      const usb = h('div.su-remove', { role: 'status' }, icon('usb'),
        h('span', h('b', 'Leave the USB drive in and click Restart now.'),
          h('small', 'Remove it once the screen goes dark. Your computer starts PolyOS from its own drive, not the USB.')));
      return [
        h('div.su-center',
          h('img.su-done-logo', { src: '/img/logo-white.svg', alt: '' }),
          h('h1', 'PolyOS 7 is installed.'),
          h('p.su-sub', `Everything is set up${plan.user.fullName ? `, ${plan.user.fullName.split(' ')[0]}` : ''}. After the restart, sign in and you’re on your desktop.`
            + (later ? ' Your apps and drivers finish installing in the background once you’re online.' : '')),
          usb,
          restart),
      ];
    }
    if (job.state === 'failed') {
      return [
        ...head('The installation didn’t finish', 'Nothing was changed if PolyOS stopped before formatting. Your USB drive still works.'),
        h('div.su-error', job.error || 'Something went wrong.'),
        h('p.su-note', icon('info'), 'Details are in /var/log/polyos-installer.log.'),
        h('div.su-nav', h('button.su-back', { onclick: () => { installJob = null; go(steps.indexOf(target)); } }, icon('chevronLeft'), 'Back'),
          h('div.su-nav-right', next('Try again', startInstall, { primary: true }))),
      ];
    }
    const els = { title: h('h1'), msg: h('p.su-sub.su-live'), bar: h('span'), pct: h('small.su-pct') };
    setTimeout(() => { progressEls = els; paintProgress(job); }, 0);
    paintProgress(job, els);
    return [
      h('div.su-center',
        h('img.su-spin.big', { src: '/img/logo-white.svg', alt: '' }),
        els.title, els.msg, h('div.su-progress', els.bar), els.pct,
        h('p.su-note', 'Keep the computer plugged in and don’t remove the USB drive.')),
    ];
  }

  function paintProgress(job, els = progressEls) {
    if (!els) return;
    const pct = Math.round((job.progress || 0) * 100);
    els.title.textContent = pct < 3 ? 'Getting ready for installation…' : 'Installing PolyOS 7…';
    els.msg.textContent = job.message || '';
    els.bar.style.width = `${Math.max(2, pct)}%`;
    els.pct.textContent = `${pct}%`;
  }

  async function tryFirst() {
    root.classList.add('leaving');
    await api.post('/api/setup/done', {}).catch(() => root.classList.remove('leaving'));
  }

  // first sign-in -----------------------------------------------------------------------
  function connect() {
    const net = store.state.system.network;
    let body;
    if (net.kind === 'ethernet' || (net.kind === 'wifi' && net.name)) {
      body = h('div.su-status', icon('check'), h('span', `You’re online: ${networkLabel(net)}.`));
    } else if (net.available && net.wifiDevice) {
      wifi = wifiPanel(store);
      body = h('div.su-wifi', wifi.el);
    } else {
      body = h('div.su-status', icon('wifiOff'), h('span', 'No network adapter was found. The Driver Manager (next) can help.'));
    }
    return [...head('Get connected', live ? 'Optional. Wi-Fi you join here keeps working after installing, so drivers and apps can download.'
      : 'Connect to the internet for drivers, apps and updates.'), body,
      nav(h('button.su-link', { onclick: () => go(step + 1) }, 'Skip'), next())];
  }

  // Poly Account: optional. Connect to Poly services (sign in, create an account, or a code), or
  // use PolyOS locally; either way nothing else changes, and Settings › Poly Account can switch later.
  let pa = { view: 'choose', status: null, countries: null };
  function polyAccount() {
    const field = (label, input, hint) => h('label.su-field', h('span', label), input, hint ? h('small', hint) : null);
    const err = h('div.su-error', { hidden: true });
    const fail = (x) => { err.textContent = x.message; err.hidden = false; };
    const view = (v) => { pa.view = v; go(step); };
    const site = () => (pa.status?.server || '').replace(/^https?:\/\//, '');
    // inside this step, Back goes to the step's previous screen
    const paNav = (prev, ...right) => h('div.su-nav', h('button.su-back', { onclick: prev, title: 'Back' }, icon('chevronLeft'), 'Back'),
      h('div.su-nav-right', ...right));
    if (!pa.status) {
      api.get('/api/polyaccount').then((s) => { pa.status = s; if (s.connected && pa.view === 'choose') pa.view = 'connected'; go(step); },
        () => { pa.status = { server: '' }; go(step); });
      return [...head('Connect to Poly?'), h('div.su-wait', h('img.su-spin', { src: '/img/logo-white.svg', alt: '' }), 'One moment…')];
    }
    const refresh = (s) => { pa.status = s; if (s.connected) pa.view = 'connected'; go(step); };
    const offline = !store.state.system.network.kind || store.state.system.network.kind === 'none';
    if (pa.view === 'choose' && offline) {
      return [...head('Connect to Poly services?', 'Optional. PolyOS works just the same without an account.'),
        h('div.su-status', icon('wifiOff'), h('span', 'You’re offline, so PolyOS will be set up to use locally. You can connect a Poly Account any time in Settings › Poly Account.')),
        nav(h('span'), next('Next', () => go(step + 1), { primary: true }))];
    }
    if (pa.view === 'choose') {
      return [...head('Connect to Poly services?', 'Optional. PolyOS works just the same without an account.'),
        h('div.su-options',
          option('Connect to Poly services', 'Device management, account recovery, sync and account emails, with a Poly Account.',
            h('span.su-dual', icon('globe')), () => view('have')),
          option('Use PolyOS locally', 'No online account. Updates still install from Settings › Updates.', h('span.su-dual', icon('user')), () => view('local'))),
        nav(h('span'))];
    }
    if (pa.view === 'local') {
      return [...head('You’re using PolyOS locally', 'No online account needed, and nothing will nag you about one.'),
        h('ul.su-bullets', h('li', 'PolyOS updates itself from Settings › Updates; no account needed.'),
          h('li', `Or download the newest version any time from ${site() || 'the PolyOS website'}/download.`),
          h('li', 'Want an account later? Settings › Poly Account.')),
        paNav(() => view('choose'), next('Continue', () => go(step + 1), { primary: true }))];
    }
    if (pa.view === 'have') {
      return [...head('Connect to Poly', 'Do you already have a Poly Account?'),
        h('div.su-options',
          option('Sign in', 'With your Poly Account email and password.', h('span.su-dual', icon('user')), () => view('signin')),
          option('Create a Poly Account', 'Name, email, password and country. That’s all.', h('span.su-dual', icon('plus')), () => view('create'))),
        h('div.su-foot', h('button.su-link', { onclick: () => view('code') }, 'Use a code instead'), h('span.su-dot', '·'),
          h('button.su-link', { onclick: () => view('local') }, 'Use PolyOS locally')),
        paNav(() => view('choose'))];
    }
    if (pa.view === 'signin') {
      const email = h('input.su-input', { type: 'email', placeholder: 'you@example.com', autocomplete: 'email', autofocus: true });
      const password = h('input.su-input', { type: 'password', placeholder: 'Password', autocomplete: 'current-password' });
      const btn = next('Sign in', null, { primary: true });
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        try { refresh(await api.post('/api/polyaccount/signin', { email: email.value, password: password.value })); } catch (x) { fail(x); btn.disabled = false; }
      });
      return [...head('Sign in to Poly', 'Your password connects this computer once. PolyOS doesn’t keep it.'),
        h('div.su-form.one', field('Email', email), field('Password', password)), err,
        h('div.su-foot', h('button.su-link', { onclick: () => view('code') }, 'Use a code instead'), h('span.su-dot', '·'),
          h('button.su-link', { onclick: () => view('create') }, 'Create an account')),
        paNav(() => view('have'), btn)];
    }
    if (pa.view === 'code') {
      const l = pa.status.link;
      if (!l || l.status === 'expired') {
        api.post('/api/polyaccount/link').then((s) => { pa.status = s; go(step); }, fail);
        return [...head('Connect with a code'), h('div.su-wait', h('img.su-spin', { src: '/img/logo-white.svg', alt: '' }), 'Getting a code…'), err,
          paNav(() => view('have'))];
      }
      const off = on('polyaccount', () => api.get('/api/polyaccount').then((s) => { if (s.connected) { off(); refresh(s); } }));
      return [...head('Enter this code on the website', `On your phone or another computer, go to ${site()}/link, sign in, and enter:`),
        h('div.su-code', `${l.userCode.slice(0, 3)} ${l.userCode.slice(3)}`),
        h('p.su-note', icon('info'), 'This screen moves on by itself when you’re done. The code works for 10 minutes.'),
        paNav(() => { off(); api.post('/api/polyaccount/link/cancel'); pa.status.link = null; view('have'); })];
    }
    if (pa.view === 'create') {
      if (!pa.countries) {
        api.get('/api/polyaccount/countries').then((r) => { pa.countries = r.countries; go(step); }, (x) => { pa.countries = ['Other']; fail(x); go(step); });
        return [...head('Create your Poly Account'), h('div.su-wait', h('img.su-spin', { src: '/img/logo-white.svg', alt: '' }), 'One moment…')];
      }
      const name = h('input.su-input', { value: (live ? plan.user.fullName : store.state.user.fullName) || '', placeholder: 'Your name',
        autocomplete: 'name', maxlength: 80 });
      const email = h('input.su-input', { type: 'email', placeholder: 'you@example.com', autocomplete: 'email' });
      const password = h('input.su-input', { type: 'password', placeholder: '10 characters or more', autocomplete: 'new-password' });
      const confirmPw = h('input.su-input', { type: 'password', placeholder: 'Type it again', autocomplete: 'new-password' });
      const guess = Intl.DateTimeFormat().resolvedOptions().timeZone?.startsWith('America/') ? 'United States' : '';
      const country = h('select.su-input', h('option', { value: '' }, 'Choose…'), pa.countries.map((c) => h('option', { value: c, selected: c === guess }, c)));
      const terms = h('input', { type: 'checkbox' });
      const privacy = h('input', { type: 'checkbox' });
      const btn = next('Create account', null, { primary: true });
      btn.addEventListener('click', async () => {
        err.hidden = true;
        if (password.value !== confirmPw.value) return fail(new Error('The passwords don’t match.'));
        if (!terms.checked || !privacy.checked) return fail(new Error('Agree to the Terms of Service and acknowledge the Privacy Policy to continue.'));
        btn.disabled = true;
        try {
          const r = await api.post('/api/polyaccount/register', { name: name.value, email: email.value, password: password.value, country: country.value, acceptTerms: true });
          refresh(r);
        } catch (x) { fail(x); btn.disabled = false; }
      });
      return [...head('Create your Poly Account', `Terms: ${site()}/terms · Privacy: ${site()}/privacy`),
        h('div.su-form', field('Name', name), field('Email', email), field('Password', password), field('Confirm password', confirmPw), field('Country', country)),
        h('label.su-check', terms, h('span', 'I agree to the Terms of Service')),
        h('label.su-check', privacy, h('span', 'I acknowledge the Privacy Policy')), err,
        paNav(() => view('have'), btn)];
    }
    // connected
    const a = pa.status.account || {};
    return [...head(`Connected, ${(a.name || '').split(' ')[0] || 'welcome'}`, `This computer is part of ${a.email || 'your Poly Account'}.`),
      h('ul.su-bullets', live ? h('li', 'It stays connected after PolyOS is installed.') : null,
        h('li', `Manage it at ${site()}/account.`), h('li', 'Poly Sync keeps your settings the same on your computers.'),
        h('li', 'Remote management stays off until you turn it on in Settings › Poly Account.')),
      nav(next('Next', () => go(step + 1), { primary: true }))];
  }

  // The hardware check: is this PC a good fit, and if it's on the slower side, a lighter PolyOS for it.
  let hardware = null;
  let profile = null; // what the person picked; null = what the check recommends
  let recommended = null; // driver packages this PC needs: installed after the restart, once online
  let needsDrivers = []; // ...and what they're for, in words
  function check() {
    const box = h('div.su-drivers.su-hw', h('div.su-wait', h('img.su-spin', { src: '/img/logo-white.svg', alt: '' }), 'Checking your processor, memory and graphics…'));
    const verdict = h('div');
    const PART_ICONS = { model: 'laptop', cpu: 'chip', ram: 'memory', gpu: 'monitor' };
    const badge = (status) => (status === 'good' ? h('span.su-ok', icon('check'))
      : h('span.su-badge', { class: status === 'low' ? 'warn' : '' }, status === 'low' ? 'Light mode' : 'OK'));
    function render() {
      const chosen = profile || hardware.profile;
      fill(box, hardware.items.map((it) => h('div.su-driver',
        h('span.su-driver-ico', icon(PART_ICONS[it.id] || 'chip')),
        h('span.su-driver-text', h('b', `${it.label}: ${it.value}`), h('small', it.note)), badge(it.status))));
      const choices = hardware.profile === 'full' ? null : h('div.su-chips', { role: 'radiogroup', 'aria-label': 'How PolyOS runs' },
        [[hardware.profile, hardware.profile === 'light' ? 'Light mode (recommended)' : 'Smooth mode (recommended)'], ['full', 'Everything on']]
          .map(([id, label]) => h('button.su-chip', { role: 'radio', 'aria-checked': String(chosen === id), class: chosen === id ? 'on' : '',
            onclick: () => { profile = id; render(); } }, label)));
      fill(verdict, h('div.su-status', { class: hardware.supported ? '' : 'warn' },
        icon(hardware.supported ? (hardware.profile === 'full' ? 'check' : 'bolt') : 'info'),
        h('span', h('b', hardware.summary), h('br'), h('small', chosen === 'full' && hardware.profile !== 'full'
          ? 'Everything on: blur, glass and animations. It may feel slower on this computer.' : hardware.profileText))), choices);
    }
    const driverNote = h('div');
    const showDrivers = () => fill(driverNote, recommended && recommended.length ? h('p.su-note', icon('info'),
      `Recommended drivers for this PC${needsDrivers.length ? ` (${needsDrivers.join(', ')})` : ''}. `
      + `${live ? 'They install by themselves after installing, once you’re online.' : 'The next steps install them.'}`) : null);
    const load = hardware ? Promise.resolve(hardware) : Promise.all([api.get('/api/hardware'), sleep(900)]).then(([r]) => r);
    load.then((r) => { hardware = r; render(); }, (err) => fill(box, h('div.su-error', err.message)));
    if (recommended) showDrivers();
    else {
      api.get('/api/drivers').then((res) => {
        recommended = [...new Set(res.devices.flatMap((d) => d.missing))];
        needsDrivers = [...new Set(res.devices.filter((d) => d.missing.length).map((d) => d.title))].slice(0, 4);
        showDrivers();
      }, () => { recommended = []; });
    }
    // Carry on: set PolyOS up for this PC (on the USB too, so trying it out is smooth as well).
    const apply = () => {
      const chosen = profile || hardware?.profile;
      const same = hardware && chosen === hardware.current;
      (chosen && !same ? api.post('/api/hardware', { profile: chosen }) : Promise.resolve()).catch(() => {}).finally(() => go(step + 1));
    };
    return [...head('Checking your computer', live ? 'PolyOS looks at this PC to make sure it runs well here.'
      : 'PolyOS sets itself up for this PC’s processor, memory and graphics.'), box, verdict, driverNote, nav(next('Next', apply))];
  }

  function drivers() {
    const list = h('div.su-drivers', h('div.su-wait', h('img.su-spin', { src: '/img/logo-white.svg', alt: '' }), 'Checking your hardware…'));
    const status = h('p.su-note', { hidden: true });
    const installBtn = next('Install drivers', null, { primary: true, disabled: true });
    let missing = [];
    api.get('/api/drivers').then((res) => {
      missing = [...new Set(res.devices.flatMap((d) => d.missing))];
      fill(list, ...res.devices.map((d) => h('div.su-driver',
        h('span.su-driver-ico', icon({ graphics: 'monitor', wifi: 'wifi', bluetooth: 'bluetooth', audio: 'volume' }[d.kind] || 'chip')),
        h('span.su-driver-text', h('b', d.title), h('small', d.missing.length ? `Recommended: ${d.missing.join(', ')}` : 'Ready')),
        d.missing.length ? h('span.su-badge', 'Update') : h('span.su-ok', icon('check')))));
      if (!res.devices.length) fill(list, h('div.su-status', icon('check'), h('span', 'Everything is ready. No extra drivers needed.')));
      installBtn.disabled = !missing.length;
      if (!missing.length) installBtn.textContent = 'All set';
    }, (err) => fill(list, h('div.su-error', err.message)));
    installBtn.addEventListener('click', async () => {
      installBtn.disabled = true;
      status.hidden = false;
      status.textContent = 'Starting…';
      try {
        await withAdmin(() => api.post('/api/drivers/install', { packages: missing }),
          { title: 'Install drivers', text: 'Enter your password to install drivers.' });
        const { off } = watchJobs((job) => {
          if (job.kind !== 'drivers') return;
          status.textContent = job.state === 'running' ? `${job.message} ${Math.round(job.progress * 100)}%`
            : job.state === 'done' ? `Drivers installed.${job.restart ? ' Restart when you’re done setting up.' : ''}`
              : job.error;
          if (job.state !== 'running') {
            off();
            installBtn.textContent = job.state === 'done' ? 'Installed' : 'Try again';
            installBtn.disabled = job.state === 'done';
          }
        });
      } catch (err) {
        status.textContent = err.cancelled ? '' : err.message;
        installBtn.disabled = false;
      }
    });
    return [...head('Drivers', 'PolyOS checks your graphics, Wi-Fi and other hardware and installs what works best.'),
      list, status, nav(h('button.su-link', { onclick: () => go(step + 1) }, 'Skip'), installBtn, next())];
  }

  // The edition chosen while installing: offer its apps now that PolyOS is online.
  function editionApps() {
    const chosen = store.state.settings.edition;
    const pack = chosen === 'regular' ? null : chosen;
    const [, name, text] = EDITIONS.find((e) => e[0] === chosen) || EDITIONS[0];
    if (!pack) {
      const more = (id, title, sub, ico) => h('button.su-option', { onclick: () => { extra = id; go(step); } },
        h('span.su-option-text', h('b', title), h('small', sub)), h('span.su-dual', icon(ico)));
      if (extra) {
        const panel = packPanel(extra, { compact: true, external: true });
        return [...head(extra === 'gaming' ? 'Gaming apps' : 'Developer tools', 'Pick what to install. You can add more in Settings anytime.'),
          panel, nav(h('button.su-link', { onclick: () => { extra = null; go(step); } }, 'Back to choices'),
            installAndContinue(panel, extra))];
      }
      return [...head('Add more to PolyOS?', 'Optional: set up gaming or coding now. Both are in Settings later too.'),
        h('div.su-options', more('gaming', 'Gaming', 'Steam, Wine, Heroic and cloud gaming', 'gamepad'),
          more('developer', 'Developer', 'Git, Python libraries, Node.js, VS Code and Docker', 'code')),
        nav(next('Skip'))];
    }
    const online = store.state.system.network.kind && store.state.system.network.kind !== 'none';
    const panel = packPanel(pack, { compact: true, external: true, onDone: () => saveSettings({ editionSetup: true }).catch(() => {}) });
    return [
      ...head(`Your ${name} edition`, text),
      online ? null : h('p.su-note', icon('wifiOff'), 'You’re offline. Go back to connect, or set this up later in Settings.'),
      panel,
      h('p.su-note', icon('info'), 'The apps download while you finish setting up, and appear on your desktop when they’re ready.'),
      nav(h('button.su-link', { onclick: () => go(step + 1) }, 'Later'), installAndContinue(panel, pack)),
    ];
  }
  let extra = null;
  let packJob = null; // the edition's apps, installing in the background while setup carries on

  // "Install and continue": start installing what's ticked, then go on (nothing ticked just goes on)
  function installAndContinue(panel, pack) {
    const button = next('Install and continue', async () => {
      button.disabled = true;
      button.textContent = 'Starting…';
      const started = await panel.start();
      if (started) {
        packJob = packJob || { kind: 'pack', target: pack, state: 'running', progress: 0, message: 'Starting…' };
        go(step + 1);
      } else {
        button.disabled = false;
        button.textContent = 'Install and continue';
      }
    }, { primary: true });
    return button;
  }

  let appsEl = null; // the "You're all set" apps line, updated in place as they download
  function paintApps() {
    if (!appsEl || !packJob) return;
    const running = packJob.state === 'running';
    fill(appsEl, h('span', running ? `Your apps are downloading in the background: ${Math.round((packJob.progress || 0) * 100)}%`
      : packJob.state === 'done' ? 'Your apps are installed and on your desktop.' : packJob.error || 'Some apps didn’t install. Try again in Settings.'),
    running ? h('div.su-progress', h('span', { style: { width: `${Math.max(3, Math.round((packJob.progress || 0) * 100))}%` } })) : null);
  }
  function done() {
    appsEl = packJob ? h('div.su-apps', { role: 'status' }) : null;
    paintApps();
    return [
      h('div.su-center',
        h('img.su-done-logo', { src: '/img/logo-white.svg', alt: '' }),
        h('h1', 'You’re all set.'),
        h('p.su-sub', 'Enjoy PolyOS 7. Everything you chose here is in Settings.'),
        appsEl,
        h('button.su-next.primary', { onclick: finish }, 'Start using PolyOS')),
    ];
  }

  async function finish() {
    root.classList.add('leaving');
    try {
      await api.post('/api/setup/done', {});
    } catch (err) {
      root.classList.remove('leaving');
      console.warn(err.message);
    }
  }

  // ---- live updates ---------------------------------------------------------------------
  watchJobs((job) => {
    if (job.kind === 'pack' && packJob) {
      packJob = job;
      if (steps[step] === done) paintApps();
      return;
    }
    if (job.kind !== 'install') return;
    installJob = job;
    if (steps[step] !== installing) return;
    if (job.state === 'running') paintProgress(job);
    else go(step);
  });
  store.subscribe((_s, changed) => {
    if (changed.has('system')) wifi?.update();
  });

  if (live) loadProbe();
  playIntro().then(() => go(0));
}
