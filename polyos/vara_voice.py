"""Vara Voice: talk to Vara. "Hey Vera", then ask; Vara answers out loud. "Stop" interrupts.

An optional part of PolyOS (Settings > Vara > Voice, offered when setting up the Developer
edition), installed by `polyos-admin vara-voice install` into VOICE_HOME:

- the microphone is read with pw-record (PipeWire), parec or arecord, 16 kHz mono;
- listening is on this computer: Vosk (offline speech recognition, a small English model) waits
  for the wake words with a tiny grammar, then writes down the request. Nothing is sent anywhere
  until you've said "Hey Vera" and a request, and then only the words go to Vara;
- Vara answers through its usual agent, told the request was spoken (short spoken answers);
- answers, reminders, routines and approval questions come from Vara's announcements and are read
  out with Piper (a natural offline voice) or espeak-ng;
- "stop", "cancel" or "Vera stop" at any moment stops the speech and Vara's current work;
  "yes", "no" or "always" answers Vara's approval question.

It runs as its own process (the shell starts and stops it, following the setting) with the Python
in VOICE_HOME, which has Vosk and Piper, and talks to the shell like polyos-ctl does.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

VOICE_HOME = Path("/opt/polyos/vara-voice")
PYTHON = VOICE_HOME / "bin" / "python3"
VOSK_MODEL = VOICE_HOME / "models" / "vosk"
PIPER_VOICE = VOICE_HOME / "models" / "piper" / "voice.onnx"
RATE = 16000
CHUNK = 4000  # bytes: 0.125 s of 16-bit mono audio

# The model's vocabulary has "vera" (and not "vara"), which sounds the same.
WAKE = ("hey vera", "okay vera", "ok vera", "hi vera", "vera")
STOP = ("stop", "cancel", "never mind", "nevermind", "be quiet", "quiet", "shut up", "that's enough", "enough")
YES = ("yes", "yeah", "yep", "sure", "okay", "ok", "do it", "go ahead", "allow", "please do")
NO = ("no", "nope", "don't", "do not", "deny")
ALWAYS = ("always",)
LISTEN_SECONDS = 9  # how long a request may take after "Hey Vera"
CONFIRM_SECONDS = 20


def installed() -> bool:
    return PYTHON.exists() and (VOSK_MODEL / "conf").is_dir()


def has_words(text: str, phrases) -> bool:
    text = f" {' '.join(text.lower().split())} "
    return any(f" {p} " in text for p in phrases)


def after_wake(text: str) -> str:
    """ "hey vera open firefox" -> "open firefox" (what came after the wake words)."""
    words = text.lower().split()
    for n in range(len(words)):
        if words[n] == "vera":
            return " ".join(words[n + 1:])
    return text.strip()


def speakable(text: str, limit: int = 600) -> str:
    """Markdown and code -> something worth saying out loud."""
    text = re.sub(r"```.*?```", " (The code is on screen.) ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [label](link) -> label
    text = re.sub(r"https?://\S+", "the link on screen", text)
    text = re.sub(r"^\s{0,3}(#{1,6}|[-*+]|\d+[.)])\s+", "", text, flags=re.M)  # headings, list markers
    text = re.sub(r"(\*\*|__|\*|~~)(?=\S)(.+?)(?<=\S)\1", r"\2", text)  # **bold**, *italic*
    text = re.sub(r"[*~>|#]+", " ", text)
    text = " ".join(text.split())
    if len(text) > limit:  # cut at a sentence
        cut = max(text.rfind(". ", 0, limit), text.rfind("? ", 0, limit), text.rfind("! ", 0, limit))
        text = text[:cut + 1] if cut > limit // 3 else text[:limit].rsplit(" ", 1)[0] + "…"
        text += " The rest is on screen."
    return text


def chime(rising: bool = True, rate: int = 22050) -> bytes:
    """A short two-note chime (raw 16-bit mono): up when Vara listens, down when it stops."""
    notes = (660.0, 880.0) if rising else (880.0, 587.0)
    out = bytearray()
    for freq in notes:
        n = int(rate * 0.09)
        for i in range(n):
            env = min(1.0, i / 200, (n - i) / 400)
            out += struct.pack("<h", int(9000 * env * math.sin(2 * math.pi * freq * i / rate)))
    return bytes(out)


# ---- the shell (like polyos-ctl) ------------------------------------------------------------------
class Shell:
    def __init__(self, info_reader=None):
        from . import paths
        self._read = info_reader or paths.read_runtime_info

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 15):
        info = self._read()
        if not info:
            raise ConnectionError("the PolyOS shell isn't running")
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-PolyOS-Token": info["token"], "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return json.loads(res.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read()).get("error") or str(exc.code)
            except ValueError:
                message = str(exc.code)
            raise RuntimeError(message) from None

    def ask(self, text: str):
        return self.call("POST", "/api/vara/chat", {"message": text, "source": "voice"})

    def stop(self):
        return self.call("POST", "/api/vara/stop", {})

    def approve(self, step: str, decision: str):
        return self.call("POST", "/api/vara/approve", {"id": step, "decision": decision})

    def said(self, after: int):
        return self.call("GET", f"/api/vara/said?after={after}")

    def status(self, state: str, text: str = ""):
        try:
            self.call("POST", "/api/vara/voice", {"state": state, "text": text[:300]}, timeout=5)
        except (OSError, RuntimeError):
            pass


# ---- sound in and out ----------------------------------------------------------------------------
def record_command() -> list[str]:
    if shutil.which("pw-record"):
        return ["pw-record", "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"]
    if shutil.which("parec"):
        return ["parec", "--raw", f"--rate={RATE}", "--channels=1", "--format=s16le"]
    return ["arecord", "-q", "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw"]


def play_command(rate: int) -> list[str]:
    if shutil.which("pw-play"):
        return ["pw-play", "--rate", str(rate), "--channels", "1", "--format", "s16", "-"]
    if shutil.which("paplay"):
        return ["paplay", "--raw", f"--rate={rate}", "--channels=1", "--format=s16le"]
    return ["aplay", "-q", "-f", "S16_LE", "-r", str(rate), "-c", "1", "-t", "raw"]


class Speaker:
    """Says things one at a time; stop() cuts it off (barge-in). Piper's voice is loaded once and
    streams into the player as it's made; espeak-ng is the fallback."""

    def __init__(self, voice_path: Path = PIPER_VOICE):
        self.voice_path = voice_path
        self._voice = None
        self._player: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._stopped = threading.Event()
        self._busy = False
        self.current = ""  # what's being said now (so its own words don't count as "stop" or "Vera")
        threading.Thread(target=self._run, name="vara-speaker", daemon=True).start()

    @property
    def speaking(self) -> bool:
        return self._busy or not self._queue.empty()

    def say(self, text: str) -> None:
        text = speakable(text)
        if text:
            self._stopped.clear()
            self._queue.put(text)

    def play(self, raw: bytes, rate: int = 22050) -> None:
        try:
            proc = subprocess.Popen(play_command(rate), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            proc.communicate(raw, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def stop(self) -> None:
        self._stopped.set()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        with self._lock:
            if self._player is not None and self._player.poll() is None:
                self._player.kill()

    def _piper(self):
        if self._voice is None and self.voice_path.exists():
            try:
                from piper import PiperVoice
                self._voice = PiperVoice.load(str(self.voice_path))
            except Exception:  # noqa: BLE001 - no Piper: espeak-ng speaks instead
                self._voice = False
        return self._voice or None

    def _run(self) -> None:
        while True:
            text = self._queue.get()
            if self._stopped.is_set():
                continue
            self._busy, self.current = True, text.lower()
            try:
                self._speak(text)
            except (OSError, ValueError, BrokenPipeError):
                pass
            finally:
                self._busy, self.current = False, ""

    def _speak(self, text: str) -> None:
        voice = self._piper()
        if voice is not None:
            rate = voice.config.sample_rate
            with self._lock:
                self._player = subprocess.Popen(play_command(rate), stdin=subprocess.PIPE,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            player = self._player
            for chunk in voice.synthesize(text):
                if self._stopped.is_set() or player.poll() is not None:
                    break
                player.stdin.write(chunk.audio_int16_bytes)
            try:
                player.stdin.close()
            except (OSError, BrokenPipeError):
                pass
            player.wait(timeout=120)
            return
        espeak = shutil.which("espeak-ng") or shutil.which("espeak")
        if espeak:
            with self._lock:
                self._player = subprocess.Popen([espeak, "-s", "165", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._player.wait(timeout=120)


# ---- the assistant ---------------------------------------------------------------------------------
class Assistant:
    """idle (waiting for "Hey Vera") -> listening (the request) -> waiting (Vara works; "stop" works)
    -> confirming (a yes or no for an approval) -> idle."""

    def __init__(self, shell: Shell, speaker: Speaker, model, recognizer, speak_replies: bool = True, wake: bool = True):
        self.shell = shell
        self.speaker = speaker
        self.model = model
        self.Recognizer = recognizer
        self.speak_replies = speak_replies
        self.wake_enabled = wake
        grammar = sorted({*WAKE, *STOP, "[unk]"})
        self.wake = recognizer(model, RATE, json.dumps(grammar))
        self.answers = recognizer(model, RATE, json.dumps(sorted({*YES, *NO, *ALWAYS, *STOP, "[unk]"})))
        self.request = None
        self.state = "idle"
        self.until = 0.0
        self.step = None  # the approval being answered
        self.last_said = 0

    # sound -> state machine
    def feed(self, chunk: bytes) -> None:
        now = time.monotonic()
        if self.state == "listening":
            done = self.request.AcceptWaveform(chunk)
            if done or now > self.until:
                text = json.loads(self.request.FinalResult() if not done else self.request.Result()).get("text", "")
                self.heard(text)
            else:
                partial = json.loads(self.request.PartialResult()).get("partial", "")
                if partial:
                    self.shell.status("listening", partial)
            return
        if self.state == "confirming":
            if self.answers.AcceptWaveform(chunk):
                self.confirm(json.loads(self.answers.Result()).get("text", ""))
            elif now > self.until:
                self.state = "waiting"
            return
        # idle or waiting: only the wake words and "stop"
        if self.wake.AcceptWaveform(chunk):
            text = json.loads(self.wake.Result()).get("text", "")
        else:
            text = json.loads(self.wake.PartialResult()).get("partial", "")
        if not text:
            return
        own = self.speaker.current  # the microphone hears Vara too: its own words aren't commands
        named = has_words(text, ("vera",)) and not has_words(own, ("vera",))
        if has_words(text, STOP) and (self.speaker.speaking or self.state == "waiting") and \
                (named or not has_words(own, STOP)):
            self.interrupt()
        elif named and self.wake_enabled:
            self.listen()

    def listen(self) -> None:
        """Hey Vera (or the push-to-talk shortcut): a chime, then the request."""
        self.speaker.stop()
        self.wake.Reset()
        self.request = self.Recognizer(self.model, RATE)
        self.state = "listening"
        self.until = time.monotonic() + LISTEN_SECONDS
        self.speaker.play(chime(True))
        self.shell.status("listening")

    def heard(self, text: str) -> None:
        text = after_wake(text) if has_words(text, ("vera",)) else text.strip()
        self.request = None
        if not text or text in ("the", "huh"):
            self.state = "idle"
            self.speaker.play(chime(False))
            self.shell.status("idle")
            return
        if has_words(text, STOP) and len(text.split()) <= 3:
            self.interrupt()
            return
        self.state = "waiting"
        self.shell.status("thinking", text)
        try:
            self.shell.ask(text)
        except (OSError, RuntimeError) as exc:
            self.state = "idle"
            self.shell.status("idle")
            self.speaker.say(str(exc) if "still working" in str(exc) else "I can't reach PolyOS right now.")

    def interrupt(self) -> None:
        self.speaker.stop()
        try:
            self.shell.stop()
        except (OSError, RuntimeError):
            pass
        self.state = "idle"
        self.step = None
        self.wake.Reset()
        self.shell.status("idle")

    def confirm(self, text: str) -> None:
        decision = "always" if has_words(text, ALWAYS) else "allow" if has_words(text, YES) else \
            "deny" if has_words(text, NO) or has_words(text, STOP) else None
        if decision is None:
            return  # didn't catch it; keep waiting until the time runs out
        step, self.step = self.step, None
        self.state = "waiting"
        try:
            self.shell.approve(step, decision)
        except (OSError, RuntimeError):
            pass
        self.speaker.say("Okay." if decision != "deny" else "Okay, I won't.")

    # Vara's announcements -> speech
    def announced(self, item: dict) -> None:
        kind = item.get("kind")
        if kind == "listen":  # the push-to-talk shortcut
            self.listen()
            return
        if kind == "approval":
            self.step = item.get("step")
            self.state = "confirming"
            self.answers.Reset()
            self.until = time.monotonic() + CONFIRM_SECONDS
            self.speaker.say(item.get("text", ""))
            return
        if kind == "reply":
            self.state = "idle"
            self.shell.status("speaking" if self.speak_replies else "idle", item.get("text", ""))
        if self.speak_replies:
            self.speaker.say(item.get("text", ""))

    def poll(self) -> None:
        try:
            got = self.shell.said(self.last_said)
        except (OSError, RuntimeError):
            return
        for item in got.get("items", []):
            self.announced(item)
        self.last_said = got.get("last", self.last_said)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    speak_replies = "--quiet" not in argv
    wake = "--no-wake" not in argv  # push-to-talk only
    try:
        from vosk import KaldiRecognizer, Model, SetLogLevel
    except ImportError:
        print("Vara Voice isn't installed: sudo polyos-admin vara-voice install", file=sys.stderr)
        return 1
    SetLogLevel(-1)
    model = Model(str(VOSK_MODEL))
    shell = Shell()
    assistant = Assistant(shell, Speaker(), model, KaldiRecognizer, speak_replies=speak_replies, wake=wake)
    try:  # start after what's already been said
        assistant.last_said = shell.said(0).get("last", 0)
    except (OSError, RuntimeError):
        pass

    def poller():
        while True:
            assistant.poll()
            time.sleep(1.0)
    threading.Thread(target=poller, name="vara-announcements", daemon=True).start()

    while True:  # the microphone; restarted if it goes away (a headset unplugged)
        mic = subprocess.Popen(record_command(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        shell.status("idle")
        while True:
            chunk = mic.stdout.read(CHUNK)
            if not chunk:
                break
            assistant.feed(chunk)
        mic.kill()
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
