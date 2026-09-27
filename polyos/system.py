"""Linux system integration through standard Debian tools.

Audio uses wpctl (PipeWire) or pactl, networking uses NetworkManager, brightness uses
brightnessctl, and power actions go through systemd-logind / LightDM. Parsers are
separate functions so they can be unit-tested on any OS.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("polyos.system")


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def run(args: list[str], timeout: float = 5.0, input: str | None = None) -> tuple[int, str]:
    """Run a command; return (returncode, stdout on success or stderr on failure)."""
    try:
        proc = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, f"{args[0]} is not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{args[0]} timed out"
    out = proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)
    return proc.returncode, out.strip()


def spawn(args: list[str], cwd: str | None = None) -> None:
    """Start a detached program and reap it in the background."""
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    threading.Thread(target=proc.wait, daemon=True).start()


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _read(path: Path, default: str = "") -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return default


def _clean_error(text: str) -> str:
    text = (text or "").strip().splitlines()[-1] if text.strip() else "Unknown error"
    return re.sub(r"^Error:\s*", "", text)


# ---- audio --------------------------------------------------------------------------

_WPCTL_RE = re.compile(r"Volume:\s*([\d.]+)(\s*\[MUTED\])?")
_PERCENT_RE = re.compile(r"(\d+)%")


def parse_wpctl_volume(text: str) -> tuple[int, bool] | None:
    m = _WPCTL_RE.search(text)
    if not m:
        return None
    return round(float(m.group(1)) * 100), bool(m.group(2))


def parse_pactl_volume(text: str) -> int | None:
    m = _PERCENT_RE.search(text)
    return int(m.group(1)) if m else None


class Audio:
    WP_SINK = "@DEFAULT_AUDIO_SINK@"
    PA_SINK = "@DEFAULT_SINK@"

    def _wpctl(self) -> dict | None:
        if not have("wpctl"):
            return None
        rc, out = run(["wpctl", "get-volume", self.WP_SINK], 2)
        parsed = parse_wpctl_volume(out) if rc == 0 else None
        if parsed is None:
            return None
        return {"available": True, "level": clamp(parsed[0], 0, 100), "muted": parsed[1], "tool": "wpctl"}

    def _pactl(self) -> dict | None:
        if not have("pactl"):
            return None
        rc, out = run(["pactl", "get-sink-volume", self.PA_SINK], 2)
        rc2, mute = run(["pactl", "get-sink-mute", self.PA_SINK], 2)
        level = parse_pactl_volume(out) if rc == 0 else None
        if level is None or rc2 != 0:
            return None
        return {"available": True, "level": clamp(level, 0, 100), "muted": "yes" in mute.lower(), "tool": "pactl"}

    def get(self) -> dict:
        state = self._wpctl() or self._pactl() or {"available": False, "level": 0, "muted": False, "tool": None}
        return state

    def set(self, level=None, delta=None, muted=None, toggle_mute=False) -> dict:
        cur = self.get()
        if not cur["available"]:
            raise RuntimeError("No audio output device found")
        if delta is not None:
            level = cur["level"] + delta
        if level is not None:
            level = clamp(int(level), 0, 100)
            if cur["muted"] and muted is None and not toggle_mute and level > 0:
                muted = False  # changing the volume unmutes, like every other desktop
        if toggle_mute:
            muted = not cur["muted"]
        wp = cur["tool"] == "wpctl"
        if level is not None:
            args = (["wpctl", "set-volume", self.WP_SINK, f"{level / 100:.2f}"] if wp
                    else ["pactl", "set-sink-volume", self.PA_SINK, f"{level}%"])
            rc, out = run(args, 3)
            if rc:
                raise RuntimeError(_clean_error(out))
        if muted is not None:
            flag = "1" if muted else "0"
            args = (["wpctl", "set-mute", self.WP_SINK, flag] if wp
                    else ["pactl", "set-sink-mute", self.PA_SINK, flag])
            rc, out = run(args, 3)
            if rc:
                raise RuntimeError(_clean_error(out))
        return self.get()


# ---- network (NetworkManager) ---------------------------------------------------------

def split_terse(line: str) -> list[str]:
    """Split one line of `nmcli -t` output (':' separated, '\\:' escaped)."""
    fields, cur, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    fields.append("".join(cur))
    return fields


def parse_wifi_list(text: str, known: set[str]) -> list[dict]:
    """Parse `nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY device wifi list`."""
    nets: dict[str, dict] = {}
    for line in text.splitlines():
        fields = split_terse(line)
        if len(fields) < 4:
            continue
        in_use, ssid, signal, security = fields[:4]
        if not ssid:
            continue  # hidden network
        security = "" if security in ("", "--") else security
        net = {
            "ssid": ssid,
            "signal": int(signal) if signal.isdigit() else 0,
            "security": security,
            "secure": bool(security),
            "active": in_use.strip() == "*",
            "known": ssid in known,
        }
        prev = nets.get(ssid)
        if prev is None or net["active"] or (not prev["active"] and net["signal"] > prev["signal"]):
            nets[ssid] = net
    return sorted(nets.values(), key=lambda n: (not n["active"], not n["known"], -n["signal"], n["ssid"].casefold()))


class Network:
    NM = "org.freedesktop.NetworkManager"
    NM_PATH = "/org/freedesktop/NetworkManager"

    def status(self) -> dict:
        base = {"available": False, "kind": "none", "name": None, "signal": None,
                "wifiDevice": None, "wifiEnabled": False}
        if not have("nmcli"):
            return base
        rc, out = run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"], 3)
        if rc:
            return base
        wifi_dev = wifi = eth = None
        for line in out.splitlines():
            dev, typ, state, conn = (split_terse(line) + ["", "", "", ""])[:4]
            if typ == "wifi":
                wifi_dev = wifi_dev or dev
                if state == "connected":
                    wifi = conn
            elif typ == "ethernet" and state == "connected":
                eth = conn
        _, radio = run(["nmcli", "radio", "wifi"], 2)
        base.update(available=True, wifiDevice=wifi_dev, wifiEnabled=radio.strip() == "enabled")
        if eth:
            base.update(kind="ethernet", name=eth)
        elif wifi:
            base.update(kind="wifi", name=wifi, signal=self._active_signal())
        return base

    def _active_signal(self) -> int | None:
        rc, out = run(["nmcli", "-t", "-f", "IN-USE,SIGNAL", "device", "wifi", "list", "--rescan", "no"], 3)
        for line in out.splitlines() if rc == 0 else []:
            fields = split_terse(line)
            if len(fields) >= 2 and fields[0].strip() == "*" and fields[1].isdigit():
                return int(fields[1])
        return None

    def _known(self) -> set[str]:
        rc, out = run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], 3)
        known = set()
        for line in out.splitlines() if rc == 0 else []:
            fields = split_terse(line)
            if len(fields) >= 2 and fields[1] in ("802-11-wireless", "wifi"):
                known.add(fields[0])
        return known

    def wifi_list(self, rescan: bool = True) -> list[dict]:
        if not have("nmcli"):
            raise RuntimeError("NetworkManager is not installed")
        base = ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan"]
        rc, out = run(base + ["yes" if rescan else "auto"], 20)
        if rc and rescan:  # NM refuses back-to-back scans; fall back to cached results
            rc, out = run(base + ["auto"], 20)
        if rc:
            raise RuntimeError(_clean_error(out))
        return parse_wifi_list(out, self._known())

    def set_wifi(self, enabled: bool) -> None:
        rc, out = run(["nmcli", "radio", "wifi", "on" if enabled else "off"], 5)
        if rc:
            raise RuntimeError(_clean_error(out))

    def forget(self, ssid: str) -> None:
        rc, out = run(["nmcli", "connection", "delete", "id", ssid], 10)
        if rc:
            raise RuntimeError(_clean_error(out))

    def connect(self, ssid: str, password: str | None = None) -> None:
        nets = {n["ssid"]: n for n in self.wifi_list(rescan=False)}
        net = nets.get(ssid)
        known = ssid in self._known()
        if known and not password:
            rc, out = run(["nmcli", "--wait", "25", "connection", "up", "id", ssid], 30)
            if rc:
                raise RuntimeError(_clean_error(out))
            return
        if net is not None and not net["secure"]:
            rc, out = run(["nmcli", "--wait", "25", "device", "wifi", "connect", ssid], 30)
            if rc:
                raise RuntimeError(_clean_error(out))
            return
        if not password:
            raise RuntimeError("This network needs a password")
        if known:
            self.forget(ssid)  # the saved password was wrong or changed
        self._connect_secure(ssid, password, (net or {}).get("security", ""))

    def _connect_secure(self, ssid: str, password: str, security: str) -> None:
        """Create the profile over D-Bus so the password never appears in a process list."""
        if "802.1X" in security:
            raise RuntimeError("Enterprise (802.1X) networks need the advanced network editor")
        if "WEP" in security:
            raise RuntimeError("WEP networks are not supported")
        wpa3_only = "WPA3" in security and "WPA2" not in security and "WPA1" not in security
        if not wpa3_only and not 8 <= len(password) <= 63:
            raise RuntimeError("Wi-Fi passwords are 8 to 63 characters")
        iface = self.status().get("wifiDevice")
        if not iface:
            raise RuntimeError("No Wi-Fi adapter found")

        from gi.repository import Gio, GLib  # only available on the real system

        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        flags = Gio.DBusCallFlags.NONE

        def call(path, iface_name, method, params, reply, timeout=10000, call_flags=flags):
            result = bus.call_sync(self.NM, path, iface_name, method, params,
                                   GLib.VariantType(reply) if reply else None, call_flags, timeout, None)
            return result.unpack() if result is not None else None

        (device,) = call(self.NM_PATH, self.NM, "GetDeviceByIpIface", GLib.Variant("(s)", (iface,)), "(o)")
        profile = {
            "connection": {"id": GLib.Variant("s", ssid), "type": GLib.Variant("s", "802-11-wireless")},
            "802-11-wireless": {"ssid": GLib.Variant("ay", ssid.encode()), "mode": GLib.Variant("s", "infrastructure")},
            "802-11-wireless-security": {
                "key-mgmt": GLib.Variant("s", "sae" if wpa3_only else "wpa-psk"),
                "psk": GLib.Variant("s", password),
            },
        }
        try:
            conn_path, active_path = call(
                self.NM_PATH, self.NM, "AddAndActivateConnection",
                GLib.Variant("(a{sa{sv}}oo)", (profile, device, "/")), "(oo)",
                timeout=30000, call_flags=Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION)
        except GLib.Error as exc:
            raise RuntimeError(exc.message.split(":")[-1].strip() or "Could not add the network") from None

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                (state,) = call(active_path, "org.freedesktop.DBus.Properties", "Get",
                                GLib.Variant("(ss)", (self.NM + ".Connection.Active", "State")), "(v)", 3000)
            except GLib.Error:
                state = None  # the active connection vanished: activation failed
            if state == 2:  # NM_ACTIVE_CONNECTION_STATE_ACTIVATED
                return
            if state is None or state == 4:  # DEACTIVATED
                break
        try:  # remove the failed profile so the next attempt starts clean
            call(conn_path, self.NM + ".Settings.Connection", "Delete", None, None)
        except GLib.Error:
            pass
        raise RuntimeError("Could not connect. Check the password and try again.")


# ---- battery and backlight -------------------------------------------------------------

def battery() -> dict:
    base = Path("/sys/class/power_supply")
    levels, statuses, ac_online = [], [], False
    for dev in sorted(base.iterdir()) if base.is_dir() else []:
        kind = _read(dev / "type")
        if kind == "Battery" and _read(dev / "present", "1") == "1" and _read(dev / "scope") != "Device":
            cap = _read(dev / "capacity")
            if cap.isdigit():
                levels.append(int(cap))
                statuses.append(_read(dev / "status"))
        elif kind == "Mains" and _read(dev / "online") == "1":
            ac_online = True
    if not levels:
        return {"present": False}
    charging = "Charging" in statuses
    return {
        "present": True,
        "level": round(sum(levels) / len(levels)),
        "charging": charging,
        "plugged": ac_online or charging or all(s == "Full" for s in statuses),
    }


class Backlight:
    def _device(self) -> Path | None:
        base = Path("/sys/class/backlight")
        devices = [d for d in base.iterdir() if (d / "max_brightness").exists()] if base.is_dir() else []
        if not devices:
            return None
        return max(devices, key=lambda d: int(_read(d / "max_brightness", "0") or 0))

    def get(self) -> dict:
        dev = self._device()
        if dev is None:
            return {"available": False, "level": 0}
        cur = int(_read(dev / "brightness", "0") or 0)
        top = max(1, int(_read(dev / "max_brightness", "1") or 1))
        return {"available": True, "level": round(100 * cur / top)}

    def set(self, level=None, delta=None) -> dict:
        dev = self._device()
        if dev is None:
            raise RuntimeError("This display has no adjustable backlight")
        if delta is not None:
            level = self.get()["level"] + delta
        level = clamp(int(level if level is not None else 100), 5, 100)  # never fully dark
        if have("brightnessctl"):
            rc, out = run(["brightnessctl", "-q", "-d", dev.name, "set", f"{level}%"], 3)
            if rc:
                raise RuntimeError(_clean_error(out))
        else:
            top = int(_read(dev / "max_brightness", "1") or 1)
            try:
                (dev / "brightness").write_text(str(round(top * level / 100)))
            except PermissionError:
                raise RuntimeError("Install brightnessctl to change the brightness") from None
        return self.get()


# ---- power, programs, info --------------------------------------------------------------

POWER_COMMANDS = {
    "lock": [["dm-tool", "lock"], ["light-locker-command", "-l"], ["loginctl", "lock-session"],
             ["xdg-screensaver", "lock"]],
    "suspend": [["systemctl", "suspend"]],
    "reboot": [["systemctl", "reboot"]],
    "poweroff": [["systemctl", "poweroff"]],
}


def power(action: str) -> None:
    last = f"No way to {action} on this system"
    for cmd in POWER_COMMANDS[action]:
        if have(cmd[0]):
            rc, out = run(cmd, 15)
            if rc == 0:
                return
            last = _clean_error(out)
    raise RuntimeError(last)


def run_default(what: str) -> None:
    home = str(Path.home())
    candidates = {
        "terminal": [["x-terminal-emulator"], ["xfce4-terminal"], ["xterm"]],
        "files": [["xdg-open", home], ["thunar", home]],
        "browser": [["x-www-browser"], ["google-chrome"], ["chromium"], ["firefox-esr"], ["xdg-open", "https://www.debian.org"]],
    }[what]
    for cmd in candidates:
        if have(cmd[0]):
            spawn(cmd)
            return
    raise RuntimeError(f"No {what} program is installed")


def run_command(command: str) -> None:
    """The Run dialog: start a program, or open a URL, file or folder with its default app."""
    command = command.strip()
    if not command:
        raise RuntimeError("Type a command to run")
    expanded = os.path.expanduser(command)
    if re.match(r"^[a-z][a-z0-9+.-]*://", command, re.I) or os.path.exists(expanded):
        spawn(["xdg-open", expanded])
        return
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise RuntimeError(f"Could not read that command: {exc}") from None
    if not have(argv[0]) and not (os.path.isfile(argv[0]) and os.access(argv[0], os.X_OK)):
        raise RuntimeError(f"PolyOS can't find “{argv[0]}”")
    spawn(argv)


def os_release() -> dict:
    out = {}
    for line in _read(Path("/etc/os-release")).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            out[key] = value.strip().strip('"')
    return out


def sysinfo() -> dict:
    cpu = platform.processor() or platform.machine()
    for line in _read(Path("/proc/cpuinfo")).splitlines():
        if line.startswith("model name"):
            cpu = line.split(":", 1)[1].strip()
            break
    else:  # ARM: /proc/cpuinfo has no model name; the board or VM name is the useful part
        board = _read(Path("/sys/firmware/devicetree/base/model")).strip("\x00\n ")
        if platform.machine() in ("aarch64", "arm64"):
            cpu = f"ARM64 ({board})" if board else "ARM64 processor"
    mem_kb = 0
    for line in _read(Path("/proc/meminfo")).splitlines():
        if line.startswith("MemTotal:"):
            mem_kb = int(line.split()[1])
            break
    disk = shutil.disk_usage("/")
    uptime = _read(Path("/proc/uptime"), "0").split()[0]
    return {
        "os": os_release().get("PRETTY_NAME", platform.system()),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "cpu": cpu,
        "cores": os.cpu_count(),
        "memoryBytes": mem_kb * 1024,
        "diskTotal": disk.total,
        "diskFree": disk.free,
        "uptime": int(float(uptime or 0)),
        "hostname": socket.gethostname(),
    }


# ---- display scale ------------------------------------------------------------------------

_XRANDR_RE = re.compile(
    r"^(\S+) connected( primary)? (\d+)x(\d+)\+\d+\+\d+[^\n]*?(\d+)mm x (\d+)mm", re.M)


def parse_xrandr_dpi(text: str) -> float | None:
    """DPI of the primary (else first) connected output from `xrandr --query`."""
    first = None
    for m in _XRANDR_RE.finditer(text):
        width_px, width_mm = int(m.group(3)), int(m.group(5))
        if width_mm <= 0:
            continue
        dpi = width_px / (width_mm / 25.4)
        if m.group(2):
            return dpi
        if first is None:
            first = dpi
    return first


def auto_scale(dpi: float | None) -> int:
    return 2 if dpi and dpi >= 170 else 1


# ---- Settings > Sound: output and input devices (pactl, which PipeWire answers too) ----------

def _percent(volume) -> int:
    """pactl JSON volume ({"front-left": {"value_percent": "65%"}, ...}) -> the average percent."""
    values = []
    for channel in (volume or {}).values():
        try:
            values.append(int(str(channel.get("value_percent", "0")).rstrip("%")))
        except (ValueError, AttributeError):
            pass
    return round(sum(values) / len(values)) if values else 0


def parse_pactl_devices(text: str, inputs: bool = False) -> list[dict]:
    """`pactl --format=json list sinks|sources` -> [{name, description, level, muted}] (no monitors)."""
    try:
        items = json.loads(text or "[]")
    except ValueError:
        return []
    out = []
    for item in items if isinstance(items, list) else []:
        name = item.get("name") or ""
        if not name:
            continue
        if inputs and (name.endswith(".monitor") or item.get("monitor_of_sink") not in (None, "", "n/a")):
            continue  # "Monitor of ..." captures an output, not a microphone
        out.append({"name": name, "description": item.get("description") or name,
                    "level": _percent(item.get("volume")), "muted": bool(item.get("mute"))})
    return out


def sound_devices() -> dict:
    if not have("pactl"):
        return {"available": False, "outputs": [], "inputs": [], "defaultOutput": None, "defaultInput": None}
    outputs = parse_pactl_devices(run(["pactl", "--format=json", "list", "sinks"], 5)[1])
    inputs = parse_pactl_devices(run(["pactl", "--format=json", "list", "sources"], 5)[1], inputs=True)
    rc1, sink = run(["pactl", "get-default-sink"], 3)
    rc2, source = run(["pactl", "get-default-source"], 3)
    return {"available": True, "outputs": outputs, "inputs": inputs,
            "defaultOutput": sink.strip() if rc1 == 0 else None, "defaultInput": source.strip() if rc2 == 0 else None}


def set_default_device(kind: str, name: str) -> None:
    known = sound_devices()["outputs" if kind == "output" else "inputs"]
    if not any(d["name"] == name for d in known):
        raise ValueError("That sound device isn't connected.")
    rc, out = run(["pactl", "set-default-sink" if kind == "output" else "set-default-source", name], 5)
    if rc != 0:
        raise ValueError(out.strip() or "That device couldn't be chosen.")


def set_input(level: int | None = None, muted: bool | None = None) -> None:
    if level is not None:
        run(["pactl", "set-source-volume", "@DEFAULT_SOURCE@", f"{clamp(level, 0, 150)}%"], 5)
    if muted is not None:
        run(["pactl", "set-source-mute", "@DEFAULT_SOURCE@", "1" if muted else "0"], 5)
