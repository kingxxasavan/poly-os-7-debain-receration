// Camera: photos and videos from the webcam, saved to Pictures/Camera. Listed only on
// computers with a camera; Settings > Privacy & security can turn camera and microphone off.

import { api, openSettings, withToken } from '../api.js';
import { fill, h, icon } from '../ui.js';

const TIMERS = [0, 3, 10];
const VIDEO_TYPES = ['video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm', 'video/mp4'];

async function upload(kind, blob) {
  const res = await fetch(withToken(`/api/camera/save?kind=${kind}`), {
    method: 'POST', headers: { 'Content-Type': blob.type.split(';')[0] }, body: blob, cache: 'no-store',
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function friendly(err) {
  if (err.name === 'NotAllowedError' || err.name === 'SecurityError') {
    return 'PolyOS didn’t let the Camera use your webcam. Check Settings > Privacy & security.';
  }
  if (err.name === 'NotFoundError' || err.name === 'OverconstrainedError') return 'No camera was found. Connect one and try again.';
  if (err.name === 'NotReadableError') return 'Another app is using the camera. Close it and try again.';
  return err.message || 'The camera couldn’t start.';
}

export function mount(root, store) {
  root.className = 'camera';
  document.title = 'Camera';
  const video = h('video.cam-video', { autoplay: true, muted: true, playsinline: true });
  video.muted = true;
  const flash = h('div.cam-flash');
  const countdown = h('div.cam-count', { hidden: true });
  const message = h('div.cam-message', { hidden: true });
  const recTime = h('div.cam-rec', { hidden: true });
  const shutter = h('button.cam-shutter', { 'aria-label': 'Take photo' }, h('span'));
  const thumb = h('button.cam-thumb', { title: 'Open in Files', 'aria-label': 'Last capture', hidden: true });
  const modeBtns = ['photo', 'video'].map((m) => h('button.cam-mode', { 'data-mode': m, onclick: () => setMode(m) },
    m === 'photo' ? 'Photo' : 'Video'));
  const timerBtn = h('button.cam-tool', { title: 'Self-timer' }, icon('timer'), h('span', 'Off'));
  const mirrorBtn = h('button.cam-tool.on', { title: 'Mirror preview' }, icon('flip'));
  const switchBtn = h('button.cam-tool', { title: 'Switch camera', hidden: true }, icon('swap'));
  const toast = h('div.cam-toast', { hidden: true });
  const hint = h('div.cam-hint', { hidden: true, role: 'status' });
  let ipu6 = false;
  const IPU6_TEXT = 'This laptop’s camera (Intel IPU6) needs its driver before it shows a picture. Open Settings › Drivers and install the camera driver, then restart.';
  // getUserMedia can wait forever on a camera that never answers
  const answerIn = (promise, ms = 10000) => Promise.race([promise, new Promise((_, reject) => setTimeout(() => {
    promise.then((s) => s.getTracks().forEach((t) => t.stop()), () => {}); // it answered after all: let go of it
    reject(Object.assign(new Error('The camera didn’t answer. Unplug it and plug it back in, or restart, then try again.'), { name: 'TimeoutError' }));
  }, ms))]);
  root.append(
    h('div.cam-stage', video, flash, countdown, recTime, message, hint),
    h('div.cam-top', timerBtn, mirrorBtn, switchBtn),
    h('div.cam-bar', h('div.cam-modes', modeBtns), h('div.cam-center', shutter), h('div.cam-right', thumb)),
    toast,
  );

  let stream = null;
  let mode = 'photo';
  let timer = 0;
  let mirror = true;
  let devices = [];
  let deviceIdx = 0;
  let recorder = null;
  let recStart = 0;
  let recTick = 0;
  let busy = false;
  let lastPath = null;
  let toastTimer = 0;

  const say = (text) => {
    fill(toast, text);
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 3200);
  };

  function showMessage(text, action) {
    fill(message, icon('camera'), h('p', text), action || null);
    message.hidden = false;
    shutter.disabled = true;
  }

  function stop() {
    stream?.getTracks().forEach((t) => t.stop());
    stream = null;
  }

  let starting = null;
  function start() {
    starting ||= open().finally(() => { starting = null; });
    return starting;
  }
  const restart = () => (starting ? starting.then(start) : start());

  async function open() {
    stop();
    const { settings } = store.state;
    if (!settings.cameraAccess) {
      return showMessage('Camera access is turned off.',
        h('button.btn.primary', { onclick: () => openSettings('privacy') }, 'Open privacy settings'));
    }
    if (!navigator.mediaDevices?.getUserMedia) return showMessage('This computer can’t show a camera here.');
    // Ask the system first: with no camera at all, say so instead of showing a black picture
    const status = await api.get('/api/camera').catch(() => null);
    ipu6 = !!status?.ipu6;
    if (status && !status.camera) {
      return showMessage(ipu6 ? IPU6_TEXT : 'No camera was found. Connect one, or check that the laptop’s camera isn’t switched off '
        + '(some laptops have a camera key, often F8 or Fn+F8, or a switch on the side).', h('button.btn', { onclick: start }, 'Try again'));
    }
    const wantAudio = mode === 'video' && settings.micAccess;
    const deviceId = devices[deviceIdx]?.deviceId;
    try {
      stream = await answerIn(navigator.mediaDevices.getUserMedia({
        video: { ...(deviceId ? { deviceId: { exact: deviceId } } : {}), width: { ideal: 1920 }, height: { ideal: 1080 } },
        audio: wantAudio,
      }));
    } catch (err) {
      if (wantAudio && err.name !== 'TimeoutError') { // no microphone: record video only
        try {
          stream = await answerIn(navigator.mediaDevices.getUserMedia({ video: deviceId ? { deviceId: { exact: deviceId } } : true }));
        } catch (err2) {
          return showMessage(friendly(err2), h('button.btn', { onclick: start }, 'Try again'));
        }
      } else {
        return showMessage(friendly(err), h('button.btn', { onclick: start }, 'Try again'));
      }
    }
    video.srcObject = stream;
    message.hidden = true;
    shutter.disabled = false;
    try {
      devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'videoinput');
    } catch { devices = []; }
    switchBtn.hidden = devices.length < 2;
    watchPicture(stream);
  }

  // A camera that opens but sends nothing, or only black: a closed privacy shutter, the camera key
  // turned off, an infrared face camera picked instead of the normal one, or an IPU6 camera
  // without its driver. Try the next camera once, then say what to check.
  let tried = 0;
  function watchPicture(current) {
    const canvas = document.createElement('canvas');
    canvas.width = 32;
    canvas.height = 18;
    const ctx = canvas.getContext('2d');
    let dark = 0;
    const check = setInterval(() => {
      if (stream !== current) return clearInterval(check);
      let brightest = 0; // a closed shutter is black everywhere; a dim room still has something brighter
      if (video.videoWidth) {
        try {
          ctx.drawImage(video, 0, 0, 32, 18);
          const px = ctx.getImageData(0, 0, 32, 18).data;
          for (let i = 0; i < px.length; i += 4) brightest = Math.max(brightest, px[i], px[i + 1], px[i + 2]);
        } catch { brightest = 255; } // can't look: assume it's fine
      }
      dark = brightest < 16 ? dark + 1 : 0;
      if (dark < 8) return; // about 4 seconds of black (or no picture at all)
      clearInterval(check);
      if (devices.length > 1 && tried < devices.length - 1) {
        tried += 1;
        deviceIdx = (deviceIdx + 1) % devices.length;
        say('No picture from that camera. Trying the next one…');
        restart();
        return;
      }
      fill(hint, icon('info'), h('span', ipu6 ? IPU6_TEXT : 'The picture is black. Open the camera’s privacy shutter, or turn the camera on '
        + 'with its key (often F8 or Fn+F8 on Lenovo laptops), then press Try again.'),
      h('button.btn.small', { onclick: () => { hint.hidden = true; tried = 0; restart(); } }, 'Try again'));
      hint.hidden = false;
    }, 500);
  }

  function setMode(next) {
    if (recorder) return;
    mode = next;
    modeBtns.forEach((b) => b.classList.toggle('on', b.dataset.mode === mode));
    root.classList.toggle('video-mode', mode === 'video');
    shutter.setAttribute('aria-label', mode === 'photo' ? 'Take photo' : 'Start recording');
    restart();
  }

  function wait(seconds) {
    return new Promise((resolve) => {
      if (!seconds) return resolve();
      let left = seconds;
      countdown.hidden = false;
      countdown.textContent = String(left);
      const t = setInterval(() => {
        left -= 1;
        if (left <= 0) {
          clearInterval(t);
          countdown.hidden = true;
          resolve();
        } else {
          countdown.textContent = String(left);
        }
      }, 1000);
    });
  }

  function saved(result, blob) {
    lastPath = result.path;
    const url = URL.createObjectURL(blob);
    const prev = thumb.dataset.url;
    if (prev) URL.revokeObjectURL(prev);
    thumb.dataset.url = url;
    fill(thumb, blob.type.startsWith('image/') ? h('img', { src: url, alt: '' }) : h('video', { src: url, muted: true }));
    thumb.hidden = false;
    say(`Saved to Pictures › Camera › ${result.name}`);
  }

  async function takePhoto() {
    const w = video.videoWidth;
    const hh = video.videoHeight;
    if (!w || !hh) return;
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = hh;
    const ctx = canvas.getContext('2d');
    if (mirror) { // save it the way it looked on screen
      ctx.translate(w, 0);
      ctx.scale(-1, 1);
    }
    ctx.drawImage(video, 0, 0, w, hh);
    flash.classList.remove('go');
    void flash.offsetWidth;
    flash.classList.add('go');
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.92));
    if (!blob) throw new Error('The photo couldn’t be made.');
    saved(await upload('photo', blob), blob);
  }

  function startRecording() {
    if (!window.MediaRecorder) throw new Error('Video recording isn’t available on this computer yet.');
    const type = VIDEO_TYPES.find((t) => MediaRecorder.isTypeSupported(t)) || '';
    const chunks = [];
    recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
    recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    recorder.onstop = async () => {
      clearInterval(recTick);
      recTime.hidden = true;
      root.classList.remove('recording');
      const blob = new Blob(chunks, { type: (recorder.mimeType || type || 'video/webm').split(';')[0] });
      recorder = null;
      try {
        saved(await upload('video', blob), blob);
      } catch (err) {
        say(err.message);
      }
    };
    recorder.start(1000);
    recStart = Date.now();
    root.classList.add('recording');
    recTime.hidden = false;
    const tick = () => {
      const s = Math.floor((Date.now() - recStart) / 1000);
      recTime.textContent = `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
    };
    tick();
    recTick = setInterval(tick, 500);
  }

  shutter.addEventListener('click', async () => {
    if (recorder) return recorder.stop();
    if (busy || !stream) return;
    busy = true;
    try {
      await wait(timer);
      if (mode === 'photo') await takePhoto();
      else startRecording();
    } catch (err) {
      say(err.message);
    }
    busy = false;
  });
  timerBtn.addEventListener('click', () => {
    timer = TIMERS[(TIMERS.indexOf(timer) + 1) % TIMERS.length];
    timerBtn.querySelector('span').textContent = timer ? `${timer}s` : 'Off';
    timerBtn.classList.toggle('on', timer > 0);
  });
  mirrorBtn.addEventListener('click', () => {
    mirror = !mirror;
    mirrorBtn.classList.toggle('on', mirror);
    video.classList.toggle('mirror', mirror);
  });
  switchBtn.addEventListener('click', () => {
    deviceIdx = (deviceIdx + 1) % devices.length;
    restart();
  });
  thumb.addEventListener('click', () => {
    if (lastPath) api.post('/api/files/open', { path: lastPath }).catch((err) => say(err.message));
  });
  document.addEventListener('keydown', (e) => {
    if ((e.key === ' ' || e.key === 'Enter') && e.target === document.body) {
      e.preventDefault();
      shutter.click();
    }
  });
  store.subscribe((_s, changed) => {
    if (!changed.has('settings')) return;
    const { cameraAccess } = store.state.settings;
    if (!cameraAccess && stream) {
      recorder?.stop();
      stop();
      start();
    } else if (cameraAccess && !stream && !starting && !message.hidden) {
      start();
    }
  });
  window.addEventListener('pagehide', stop);
  video.classList.add('mirror');
  setMode('photo');
}
