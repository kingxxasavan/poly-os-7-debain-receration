"""Power modes, screen-off and sleep timers, and camera detection (Settings > Power, Camera app)."""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("polyos.power")

# PolyOS power mode -> power-profiles-daemon profile (the closest one the machine offers).
PROFILES = {"saver": "power-saver", "balanced": "balanced", "performance": "performance", "maximum": "performance"}
MODE_INFO = {
    "saver": ("Power saver", "Slower processor and a dimmer screen, for the longest battery life"),
    "balanced": ("Balanced", "Full speed when you need it, quiet when you don't"),
    "performance": ("Performance", "Keeps the processor fast, uses more power"),
    "maximum": ("Maximum", "Performance, and the screen never turns off or sleeps on its own"),
}
SAVER_BRIGHTNESS = 60  # power saver caps the screen brightness at this percent


def available_profiles() -> list[str]:
    """Profiles power-profiles-daemon offers here ([] when it isn't installed or running)."""
    if not shutil.which("powerprofilesctl"):
        return []
    try:
        out = subprocess.run(["powerprofilesctl", "list"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_profiles(out)


def parse_profiles(text: str) -> list[str]:
    """Profile names from `powerprofilesctl list` ("* balanced:", "  power-saver:", ...)."""
    names = []
    for line in text.splitlines():
        stripped = line.strip().lstrip("*").strip()
        if stripped.endswith(":") and " " not in stripped[:-1] and not line.startswith("    "):
            names.append(stripped[:-1])
    return names


def profile_for(mode: str, offered: list[str]) -> str | None:
    want = PROFILES.get(mode, "balanced")
    if want in offered:
        return want
    return "balanced" if "balanced" in offered else None


def apply_mode(mode: str) -> str | None:
    """Switch the power profile. Returns the profile set, or None when the machine has none."""
    profile = profile_for(mode, available_profiles())
    if profile is None:
        return None
    try:
        subprocess.run(["powerprofilesctl", "set", profile], check=False, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not set power profile %s: %s", profile, exc)
        return None
    return profile


def timers(settings: dict) -> tuple[int, int]:
    """Minutes before the screen turns off and before sleep (0 = never). Maximum mode turns both off."""
    if settings.get("powerMode") == "maximum":
        return 0, 0
    return int(settings.get("screenOff", 0)), int(settings.get("sleepAfter", 0))


def apply_screen_off(minutes: int) -> None:
    """Turn the screen off (DPMS) after this many idle minutes; 0 keeps it on."""
    if not shutil.which("xset"):
        return
    seconds = minutes * 60
    cmds = [["xset", "s", "off"], ["xset", "s", "noblank"]]
    cmds.append(["xset", "+dpms"] if seconds else ["xset", "-dpms"])
    if seconds:
        cmds.append(["xset", "dpms", str(seconds), str(seconds), str(seconds)])
    for cmd in cmds:
        try:
            subprocess.run(cmd, check=False, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return


class _XScreenSaverInfo(ctypes.Structure):
    _fields_ = [("window", ctypes.c_ulong), ("state", ctypes.c_int), ("kind", ctypes.c_int),
                ("til_or_since", ctypes.c_ulong), ("idle", ctypes.c_ulong), ("eventMask", ctypes.c_ulong)]


class IdleClock:
    """Milliseconds since the last keyboard or mouse input, from the X screensaver extension."""

    def __init__(self):
        self._dpy = None
        self._xss = None
        try:
            xlib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("X11") or "libX11.so.6")
            xss = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Xss") or "libXss.so.1")
        except OSError as exc:
            log.info("idle timers unavailable (install libxss1): %s", exc)
            return
        xlib.XOpenDisplay.restype = ctypes.c_void_p
        xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        xlib.XDefaultRootWindow.restype = ctypes.c_ulong
        xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(_XScreenSaverInfo)
        xss.XScreenSaverQueryInfo.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_XScreenSaverInfo)]
        self._dpy = xlib.XOpenDisplay(None)
        if not self._dpy:
            return
        self._root = xlib.XDefaultRootWindow(self._dpy)
        self._info = xss.XScreenSaverAllocInfo()
        self._xss = xss

    def idle_ms(self) -> int | None:
        if not self._dpy or self._xss is None:
            return None
        if not self._xss.XScreenSaverQueryInfo(self._dpy, self._root, self._info):
            return None
        return int(self._info.contents.idle)


def has_ipu6(pci_root: Path = Path("/sys/bus/pci/devices")) -> bool:
    """An Intel IPU6 laptop camera (MIPI): no picture until its driver is installed (Settings > Drivers)."""
    from .drivers import IPU6_IDS
    try:
        devices = list(pci_root.iterdir())
    except OSError:
        return False
    for dev in devices:
        try:
            if (dev / "vendor").read_text().strip() == "0x8086" and (dev / "device").read_text().strip()[2:] in IPU6_IDS:
                return True
        except OSError:
            continue
    return False


def has_camera(sys_root: Path = Path("/sys/class/video4linux")) -> bool:
    """True when a webcam is connected (a V4L2 capture device, not a codec or metadata node)."""
    try:
        nodes = sorted(sys_root.iterdir())
    except OSError:
        return False
    for node in nodes:
        try:
            name = (node / "name").read_text().strip().lower()
            index = int((node / "index").read_text().strip() or 0)
        except (OSError, ValueError):
            continue
        # UVC webcams expose two nodes per camera (index 0 = video, 1 = metadata); skip codecs.
        if index == 0 and not any(word in name for word in ("codec", "decoder", "encoder", "isp", "m2m")):
            return True
    return False


# ---- the lid and the power button (Settings > Power & Performance), done by logind -------------
LOGIND_CONF = Path("/etc/systemd/logind.conf.d/50-polyos.conf")
KEY_ACTIONS = {"suspend": "Sleep", "poweroff": "Shut down", "lock": "Lock", "ignore": "Do nothing"}
KEY_DEFAULTS = {"lid": "suspend", "lidPlugged": "suspend", "button": "poweroff"}


def logind_text(lid: str, lid_plugged: str, button: str) -> str:
    for value in (lid, lid_plugged, button):
        if value not in KEY_ACTIONS:
            raise ValueError("Choose Sleep, Shut down, Lock or Do nothing.")
    return ("# Written by PolyOS (Settings > Power & Performance)\n[Login]\n"
            f"HandleLidSwitch={lid}\nHandleLidSwitchExternalPower={lid_plugged}\nHandleLidSwitchDocked=ignore\n"
            f"HandlePowerKey={button}\n")


def power_keys(path: Path = LOGIND_CONF) -> dict:
    """What closing the lid (on battery, plugged in) and pressing the power button do now."""
    out = dict(KEY_DEFAULTS)
    names = {"HandleLidSwitch": "lid", "HandleLidSwitchExternalPower": "lidPlugged", "HandlePowerKey": "button"}
    try:
        text = path.read_text("utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() in names and value.strip() in KEY_ACTIONS:
            out[names[key.strip()]] = value.strip()
    return out


def has_lid() -> bool:
    return any(Path("/proc/acpi/button/lid").glob("*/state")) or Path("/sys/class/input").exists() and any(
        "Lid Switch" in (p / "name").read_text(errors="ignore") for p in Path("/sys/class/input").glob("input*")
        if (p / "name").exists())
