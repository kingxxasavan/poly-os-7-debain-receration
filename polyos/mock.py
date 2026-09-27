"""Simulated backend for `python main.py dev`: the whole UI in a normal browser, on any OS."""

from __future__ import annotations

import copy
import json
import os
import random
import shutil
import threading
import time
from pathlib import Path

from . import __version__, installer, paths, startup, store
from .files import kind_of
from .backend import DISPLAY_NAMES, DOCK_HEIGHT, DOCK_MARGIN, PANEL_HEIGHT, Backend, app_hidden, supersede_browsers
from .core import ApiError, EventBus, Settings, bundled_icon, icon_names, letter_icon
from .files import P
from .privileged import NeedPassword

GB = 1000 ** 3
OWN_APPS = {"polyos-settings.desktop": "settings", "polyos-files.desktop": "files", "polyos-taskmgr.desktop": "taskmgr",
            "polyos-drivers.desktop": "drivers", "polyos-store.desktop": "store", "polyos-camera.desktop": "camera",
            "polyos-install.desktop": "setup"}

_APPS = [
    ("google-chrome.desktop", "Google Chrome", "Access the Internet", "Network;WebBrowser"),
    ("chromium.desktop", "Chromium", "Access the Internet", "Network;WebBrowser"),  # kept for cloud gaming
    ("polyos-files.desktop", "Files", "Browse and organize your files", "System;FileManager"),
    ("thunar.desktop", "Thunar File Manager", "Browse the filesystem", "System;FileManager"),
    ("xfce4-terminal.desktop", "Terminal", "Use the command line", "System;TerminalEmulator"),
    ("org.xfce.mousepad.desktop", "Mousepad", "Simple text editor", "Utility;TextEditor"),
    ("polyos-settings.desktop", "Settings", "Personalize and configure PolyOS", "Settings;System"),
    ("polyos-taskmgr.desktop", "Task Manager", "See and end running apps", "System;Monitor"),
    ("polyos-drivers.desktop", "Driver Manager", "Install drivers for your hardware", "System;Settings"),
    ("polyos-store.desktop", "PolyMarket", "Get trusted apps", "System;PackageManager"),
    ("polyos-camera.desktop", "Camera", "Take photos and videos", "AudioVideo;Video;Photography"),
    ("pavucontrol.desktop", "Volume Control", "Adjust the volume level", "AudioVideo;Settings"),
    ("nm-connection-editor.desktop", "Advanced Network Configuration", "Manage network connections", "Settings"),
    ("htop.desktop", "Htop", "Show system processes", "System;Monitor"),
    ("org.gnome.Calculator.desktop", "Calculator", "Perform calculations", "Utility;Calculator"),
    ("libreoffice-writer.desktop", "LibreOffice Writer", "Create and edit documents", "Office"),
    ("gimp.desktop", "GNU Image Manipulation Program", "Create images and edit photographs", "Graphics"),
    ("vlc.desktop", "VLC media player", "Play movies and music", "AudioVideo"),
    ("blender.desktop", "Blender", "3D modeling, animation and rendering", "Graphics"),
    ("code.desktop", "Visual Studio Code", "Code editing. Redefined.", "Development"),
    ("arandr.desktop", "ARandR", "Arrange displays", "Settings"),
    ("xfce4-screenshooter.desktop", "Screenshot", "Take screenshots", "Utility"),
    ("org.xfce.ristretto.desktop", "Ristretto Image Viewer", "Look at your images", "Graphics"),
    ("steam.desktop", "Steam", "Application for managing and playing games", "Game"),
]

# the Icon= names these apps' .desktop files use, so the preview shows the same icons as a real system
_ICONS = {"google-chrome.desktop": "google-chrome", "thunar.desktop": "org.xfce.thunar",
          "xfce4-terminal.desktop": "org.xfce.terminal", "pavucontrol.desktop": "multimedia-volume-control",
          "nm-connection-editor.desktop": "preferences-system-network", "code.desktop": "com.visualstudio.code",
          "arandr.desktop": "preferences-desktop-display", "xfce4-screenshooter.desktop": "org.xfce.screenshooter"}

# what a real PolyOS image ships of the apps above (the rest stand for apps installed later)
MOCK_BASE = {"google-chrome.desktop", "chromium.desktop", "xfce4-terminal.desktop", "org.xfce.mousepad.desktop", "pavucontrol.desktop",
             "nm-connection-editor.desktop", "htop.desktop", "org.gnome.Calculator.desktop", "arandr.desktop",
             "xfce4-screenshooter.desktop", "org.xfce.ristretto.desktop", "thunar.desktop"}


class MockBackend(Backend):
    dev = True

    def __init__(self, settings: Settings, bus: EventBus, live: bool = False, home: Path | None = None):
        home = Path(home) if home else paths.ROOT / "build" / "dev-home"
        seed_home(home)
        super().__init__(settings, bus, home=home)
        self.live = live
        self._lock = threading.Lock()
        apps = list(_APPS)
        if live:
            apps.append(("polyos-install.desktop", "Install PolyOS 7", "Install PolyOS on this computer, or next to Windows", "System"))
            apps.append(("install-debian.desktop", "PolyOS Advanced Installer", "Install PolyOS with manual partitioning", "System"))
        self._apps = sorted(
            ({"id": i, "name": DISPLAY_NAMES.get(i, n), "description": d, "categories": c.split(";"), "keywords": [],
              "icon": f"/icon/app/{i}", "hidden": app_hidden(i, MOCK_BASE)} for i, n, d, c in apps),
            key=lambda a: a["name"].casefold())
        supersede_browsers(self._apps, MOCK_BASE)
        self._admin_ready = live  # the live USB's account needs no password; "polyos" unlocks the mock
        if live:
            self.show_install_app()
        if os.environ.get("POLYOS_SERVER") and not live:  # a local website (npm run dev): real Poly Account check-ins
            self.start_account_loop(first_delay=2, every=15)
        self._store_installed = {"chrome"}
        self._driver_state: set[str] = {"firmware-iwlwifi", "firmware-sof-signed"}
        self._procs_seed = random.Random(7)
        self._windows: list[dict] = []
        self._next_xid = 0x3a00001
        self._system = {
            "volume": {"available": True, "level": 45, "muted": False},
            "brightness": {"available": True, "level": 70},
            "battery": {"present": True, "level": 76, "charging": False, "plugged": False},
            "network": {"available": True, "kind": "wifi", "name": "PolyNet", "signal": 78,
                        "wifiDevice": "wlan0", "wifiEnabled": True},
        }
        self._wifi = [
            {"ssid": "PolyNet", "signal": 78, "security": "WPA2", "secure": True, "active": True, "known": True},
            {"ssid": "Library Guest", "signal": 64, "security": "", "secure": False, "active": False, "known": False},
            {"ssid": "Panthers-5G", "signal": 52, "security": "WPA2 WPA3", "secure": True, "active": False, "known": False},
            {"ssid": "DIRECT-roku-219", "signal": 31, "security": "WPA2", "secure": True, "active": False, "known": False},
        ]

    # ---- apps and windows ------------------------------------------------------------
    def apps(self):
        return self._apps

    def windows(self):
        with self._lock:
            return copy.deepcopy(self._windows)

    def _publish_windows(self):
        self.bus.publish("windows", windows=self.windows())

    def _app(self, app_id):
        app = next((a for a in self._apps if a["id"] == app_id), None)
        if app is None:
            raise ApiError("app not found", 404)
        return app

    def launch(self, app_id):
        if app_id in OWN_APPS:
            return self.open_app(OWN_APPS[app_id])
        app = self._app(app_id)
        self.note_launch(app_id)
        self._add_window(app["id"], app["name"])

    def _add_window(self, app_id, title, **extra):
        with self._lock:
            for w in self._windows:
                w["active"] = False
            xid, self._next_xid = self._next_xid, self._next_xid + 0x100
            self._windows.append({"xid": xid, "title": title, "appId": app_id, "active": True,
                                  "minimized": False, "icon": f"/icon/app/{app_id}", **extra})
        self.popup_closed()
        self._publish_windows()

    def window_action(self, xid, action):
        with self._lock:
            win = next((w for w in self._windows if w["xid"] == xid), None)
            if win is None:
                raise ApiError("window not found", 404)
            if action == "toggle":
                action = "minimize" if win["active"] and not win["minimized"] else "activate"
            if action == "activate":
                for w in self._windows:
                    w["active"] = w is win
                win["minimized"] = False
            elif action == "minimize":
                win.update(minimized=True, active=False)
            elif action == "fullscreen":
                win["fullscreen"] = not win.get("fullscreen")
            elif action in ("close", "kill"):
                self._windows.remove(win)
        self._publish_windows()

    def open_app(self, name, page=None):
        if name == "files":  # every call opens a new Files window, like the real shell
            self.note_launch("polyos-files.desktop")
            return self._add_window("polyos-files.desktop", "Files", page=page or P(self.files.home))
        if name == "setup":
            return self.update_settings({"setupDone": False})
        app_id = next(k for k, v in OWN_APPS.items() if v == name)
        titles = {"settings": "Settings", "taskmgr": "Task Manager", "drivers": "Driver Manager", "store": "PolyMarket",
                  "camera": "Camera"}
        if name != "settings":
            self.note_launch(app_id)
        with self._lock:
            win = next((w for w in self._windows if w["appId"] == app_id), None)
        if win is None:
            self._add_window(app_id, titles[name], page=page or ("appearance" if name == "settings" else ""))
        else:
            self.window_action(win["xid"], "activate")
            if page:
                self.bus.publish("navigate", surface="settings", page=page)

    def finish_setup(self):
        self.update_settings({"setupDone": True})

    # ---- login screen (password for every mock account: "polyos") ------------------------
    def greeter_state(self):
        user = self.user()
        return {
            "users": [{"name": user["name"], "displayName": user["fullName"], "loggedIn": False},
                      {"name": "andrew", "displayName": "Andrew", "loggedIn": False}],
            "sessions": [{"key": "polyos", "name": "PolyOS"}, {"key": "xfce", "name": "Xfce Session"}],
            "defaultSession": "polyos", "selectedUser": user["name"], "lock": self.greeter_lock,
            "hideUsers": False, "hostname": "polyos",
            "can": {"shutdown": True, "restart": True, "suspend": True},
        }

    greeter_lock = False

    def greeter_login(self, user, password, session):
        time.sleep(0.6)
        if password != "polyos":
            raise ApiError("The password entered is incorrect. Please try again.", 403)
        self.bus.publish("power", action="login")
        return {"ok": True}

    def greeter_power(self, action):
        self.bus.publish("power", action={"shutdown": "poweroff", "restart": "reboot"}.get(action, action))

    def windows_installed(self):
        return True  # the preview shows the dual-boot Power menu

    def restart_to_windows(self):
        self.bus.publish("power", action="reboot")
        return {"ok": True}

    # ---- Settings: lid and power button, usage, storage clean-up (simulated) ----------------------
    def power_keys(self):
        from . import power
        return {**getattr(self, "_keys", power.KEY_DEFAULTS), "hasLid": True, "actions": power.KEY_ACTIONS}

    def power_keys_set(self, lid, plugged, button):
        from . import power
        power.logind_text(lid, plugged, button)
        if not self._admin_ready:
            raise NeedPassword()
        self._keys = {"lid": lid, "lidPlugged": plugged, "button": button}
        return self.jobs.start("power-keys", "Saving power button and lid settings", [], target="power-keys",
                               runner=self._simulate(["Saving…"], 1))

    def usage(self):
        if not self._usage_path().exists() and self.settings.get("keepRecent"):
            for app_id, mins in (("google-chrome.desktop", 142), ("xfce4-terminal.desktop", 38), ("org.xfce.mousepad.desktop", 21),
                                 ("steam.desktop", 64)):
                self.usage_tick(app_id, mins * 60)
        return super().usage()

    def storage_clean(self, what):
        if what == "packages":
            if not self._admin_ready:
                raise NeedPassword()
            return self.jobs.start("clean", "Deleting downloaded packages", [], target="packages",
                                   runner=self._simulate(["Deleting downloaded packages…"], 1))
        return super().storage_clean(what)

    # ---- the Start menu's Recommended, and an app in the background (the taskbar's ^) ----------
    def recent_files(self):
        if not hasattr(self, "_recent"):
            home = self.files.home
            now = int(time.time())
            self._recent = [{"name": Path(rel).name, "path": str(home / rel), "time": now - age,
                             "kind": kind_of(home / rel, False)[1]}
                            for rel, age in (("Documents/Budget.xlsx", 1800), ("Documents/School/History essay.docx", 7200),
                                             ("Documents/Launch deck.pptx", 86400), ("Documents/Welcome to PolyOS.txt", 3 * 86400))
                            if (home / rel).exists()]
        return self._recent[:8]

    def background_apps(self):
        if getattr(self, "_steam_quit", False) or any(w["appId"] == "steam.desktop" for w in self.windows()):
            return []
        app = self._app("steam.desktop")
        return [{"appId": app["id"], "name": app["name"], "icon": app["icon"], "pid": 4242}]

    def end_background_app(self, app_id):
        if app_id != "steam.desktop" or not self.background_apps():
            raise ApiError("That app isn't running.", 404)
        self._steam_quit = True
        return {"ok": True}

    def open_path(self, path):
        p = self.files.resolve(path)
        if p.is_dir():
            return self.open_app("files", str(p))
        if self.settings.get("keepRecent"):
            recent = [f for f in self.recent_files() if f["path"] != str(p)]
            self._recent = [{"name": p.name, "path": str(p), "time": int(time.time()), "kind": kind_of(p, False)[1]}, *recent]
        kind = self.files.entry(p)["kind"]
        app = {"image": "org.xfce.ristretto.desktop", "video": "vlc.desktop", "audio": "vlc.desktop"}.get(kind, "org.xfce.mousepad.desktop")
        self._add_window(app, p.name)

    def terminal_at(self, path):
        self.launch("xfce4-terminal.desktop")

    def run_default(self, what):
        if what == "files":
            return self.open_app("files")
        category = {"terminal": "TerminalEmulator", "files": "FileManager", "browser": "WebBrowser"}[what]
        app = next(a for a in self._apps if category in a["categories"])
        self.launch(app["id"])

    def run_command(self, command):
        name = command.strip().split()[0] if command.strip() else ""
        if not name:
            raise RuntimeError("Type a command to run")
        match = next((a for a in self._apps if a["id"].split(".")[-2].endswith(name.lower())
                      or a["name"].lower().startswith(name.lower())), None)
        if match is None:
            raise RuntimeError(f"PolyOS can't find “{name}”")
        self.launch(match["id"])

    def app_icon(self, app_id):
        own = {"polyos-settings.desktop": "settings.svg", "polyos-files.desktop": "files.svg",
               "polyos-taskmgr.desktop": "taskmgr.svg", "polyos-drivers.desktop": "drivers.svg",
               "polyos-store.desktop": "store.svg", "polyos-camera.desktop": "camera.svg",
               "polyos-install.desktop": "install.svg"}
        if app_id in own:
            return (paths.UI_DIR / "img" / own[app_id]).read_bytes(), "image/svg+xml"
        if app_id.startswith("polyos-cloud-"):
            return (paths.UI_DIR / "img" / "cloud-gaming.svg").read_bytes(), "image/svg+xml"
        name = next((a["name"] for a in self._apps if a["id"] == app_id), app_id)
        icon = bundled_icon([_ICONS.get(app_id, ""), *icon_names(app_id, name)])
        return (icon, "image/svg+xml") if icon else (letter_icon(name), "image/svg+xml")

    def window_icon(self, xid):
        return letter_icon("?"), "image/svg+xml"

    # ---- system --------------------------------------------------------------------------
    def system_status(self):
        with self._lock:
            return copy.deepcopy(self._system)

    def _publish_system(self):
        self.bus.publish("system", system=self.system_status())
        return self.system_status()

    def set_volume(self, level=None, delta=None, muted=None, toggle_mute=False):
        with self._lock:
            vol = self._system["volume"]
            if delta is not None:
                level = vol["level"] + delta
            if level is not None:
                vol["level"] = max(0, min(100, level))
                if muted is None and not toggle_mute and vol["level"] > 0:
                    vol["muted"] = False
            if toggle_mute:
                vol["muted"] = not vol["muted"]
            elif muted is not None:
                vol["muted"] = muted
        return self._publish_system()["volume"]

    def set_brightness(self, level=None, delta=None):
        with self._lock:
            bl = self._system["brightness"]
            if delta is not None:
                level = bl["level"] + delta
            bl["level"] = max(5, min(100, level if level is not None else 100))
        return self._publish_system()["brightness"]

    # ---- Settings > Sound and Display ----------------------------------------------------------
    _SOUND = {
        "outputs": [{"name": "alsa_output.pci-0000_00_1f.3.analog-stereo", "description": "Speakers (built-in)", "level": 45, "muted": False},
                    {"name": "alsa_output.pci-0000_01_00.1.hdmi-stereo", "description": "HDMI (Monitor)", "level": 80, "muted": False},
                    {"name": "bluez_output.AC_12_2F.1", "description": "Poly Buds", "level": 60, "muted": False}],
        "inputs": [{"name": "alsa_input.pci-0000_00_1f.3.analog-stereo", "description": "Microphone (built-in)", "level": 70, "muted": False},
                   {"name": "alsa_input.usb-Webcam_C920-02.analog-stereo", "description": "Webcam microphone", "level": 55, "muted": False}],
    }

    def sound_devices(self):
        with self._lock:
            sound = getattr(self, "_sound", None)
            if sound is None:
                sound = self._sound = {**copy.deepcopy(self._SOUND), "available": True,
                                       "defaultOutput": self._SOUND["outputs"][0]["name"],
                                       "defaultInput": self._SOUND["inputs"][0]["name"]}
            return copy.deepcopy(sound)

    def sound_set_device(self, kind, name):
        current = self.sound_devices()
        if not any(d["name"] == name for d in current["outputs" if kind == "output" else "inputs"]):
            raise ApiError("That sound device isn't connected.")
        with self._lock:
            self._sound["defaultOutput" if kind == "output" else "defaultInput"] = name
        return self.sound_devices()

    def sound_set_input(self, level, muted):
        current = self.sound_devices()
        with self._lock:
            mic = next(d for d in self._sound["inputs"] if d["name"] == current["defaultInput"])
            if level is not None:
                mic["level"] = max(0, min(150, level))
            if muted is not None:
                mic["muted"] = muted
        return self.sound_devices()

    def sound_mixer(self, tab):
        return {"opened": tab}

    _XRANDR = """Screen 0: minimum 320 x 200, current 4480 x 1440, maximum 16384 x 16384
eDP-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 309mm x 174mm
   1920x1080     60.01*+  59.97    48.00
   1680x1050     59.95
   1280x720      60.00
HDMI-1 connected 2560x1440+1920+0 (normal left inverted right x axis y axis) 597mm x 336mm
   2560x1440    143.97 + 120.00    59.95*
   1920x1080    144.00   120.00    60.00
   1280x720      60.00
DP-1 disconnected (normal left inverted right x axis y axis)
"""

    def displays_list(self):
        from . import display
        with self._lock:
            outputs = getattr(self, "_outputs", None)
            if outputs is None:
                outputs = self._outputs = display.parse_xrandr(self._XRANDR)
            return {"outputs": copy.deepcopy(outputs), "graphics": self.graphics_info()}

    def displays_set(self, name, size, rate, rotation, primary):
        from . import display
        try:
            display.validate(self.displays_list()["outputs"], name, size, rate, rotation)
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        with self._lock:
            for out in self._outputs:
                if primary:
                    out["primary"] = out["name"] == name
                if out["name"] == name:
                    if size:
                        out["mode"], out["rate"] = size, rate or next(m["rates"][0] for m in out["modes"] if m["size"] == size)
                    if rotation:
                        out["rotation"] = rotation
        saved = dict(self.settings.get("displays") or {})
        saved[name] = {"size": size, "rate": rate, "rotation": rotation or "normal", "primary": primary}
        self.update_settings({"displays": saved})
        return self.displays_list()

    def _hardware_facts(self, facts):
        """A made-up computer; POLYOS_MOCK_HW=light (or balanced) previews the other results."""
        kind = os.environ.get("POLYOS_MOCK_HW", "full")
        from .hwcheck import describe_model
        if kind == "light":
            return {**facts, "cpu": "Intel Celeron N4020 CPU @ 1.10GHz", "cores": 2, "ram": 2 * 1024 ** 3 - 180 * 1024 ** 2,
                    "graphics": [{"name": "Intel Corporation UHD Graphics 600", "driver": "i915"}], "renderer": "",
                    "computer": describe_model({"sys_vendor": "HP", "product_name": "HP Stream Laptop 14-cb1xx",
                                                "bios_date": "11/02/2019", "chassis_type": "10"})}
        if kind == "balanced":
            return {**facts, "cpu": "AMD Ryzen 5 5600X 6-Core Processor", "cores": 4, "ram": 4 * 1024 ** 3,
                    "graphics": [], "renderer": "llvmpipe (LLVM 15.0.6, 256 bits)",
                    "computer": describe_model({"sys_vendor": "QEMU", "product_name": "Standard PC (Q35 + ICH9, 2009)",
                                                "bios_date": "04/01/2014", "chassis_type": "1"})}
        return {**facts, "cpu": "AMD Ryzen 7 5700U with Radeon Graphics", "cores": 16, "ram": 16 * 1024 ** 3 - 400 * 1024 ** 2,
                "graphics": self.graphics_info(), "renderer": "AMD Radeon Graphics (renoir, LLVM 15.0.6)",
                "computer": describe_model({"sys_vendor": "LENOVO", "product_name": "82LN", "product_version": "IdeaPad 5 15ALC05",
                                            "bios_date": "03/14/2022", "chassis_type": "10"})}

    def graphics_info(self):
        return [{"name": "Intel Corporation Iris Xe Graphics [8086:9a49]", "driver": "i915"},
                {"name": "NVIDIA Corporation GA107M [GeForce RTX 3050 Mobile] [10de:25a2]", "driver": "nouveau"}]

    def wifi_list(self):
        time.sleep(0.5)
        with self._lock:
            return {"enabled": self._system["network"]["wifiEnabled"], "networks": copy.deepcopy(self._wifi)}

    def wifi_connect(self, ssid, password):
        time.sleep(1.2)
        with self._lock:
            net = next((n for n in self._wifi if n["ssid"] == ssid), None)
            if net is None:
                raise RuntimeError("Network not found")
            if net["secure"] and not net["known"]:
                if not password:
                    raise RuntimeError("This network needs a password")
                if password == "wrong" or len(password) < 8:
                    raise RuntimeError("Could not connect. Check the password and try again.")
            for n in self._wifi:
                n["active"] = n is net
            net["known"] = True
            self._system["network"].update(kind="wifi", name=ssid, signal=net["signal"])
        self._publish_system()

    def wifi_forget(self, ssid):
        with self._lock:
            for n in self._wifi:
                if n["ssid"] == ssid:
                    n["known"] = False
                    if n["active"]:
                        n["active"] = False
                        self._system["network"].update(kind="none", name=None, signal=None)
        self._publish_system()

    def wifi_enable(self, enabled):
        with self._lock:
            net = self._system["network"]
            net["wifiEnabled"] = enabled
            if not enabled:
                net.update(kind="none", name=None, signal=None)
                for n in self._wifi:
                    n["active"] = False
        self._publish_system()

    def apply_airplane(self, on):
        self.wifi_enable(not on)

    def apply_night_light(self):
        pass  # the simulator has no screens to tint

    def update_settings(self, patch):
        settings = super().update_settings(patch)
        if isinstance(patch, dict) and "airplaneMode" in patch:
            self.apply_airplane(settings["airplaneMode"])
        return settings

    def power(self, action):
        self.popup_closed()
        if action == "lock":
            return self.lock()
        self.bus.publish("power", action=action)

    # ---- lock screen (mock password: "polyos"; any well-formed recovery key works) ----------
    def lock(self):
        self.popup_closed()
        self.bus.publish("lock", locked=True)

    # PIN (simulated: kept in memory; the password is "polyos")
    def pin_status(self):
        return {"set": bool(getattr(self, "_pin", None)), "blocked": False, "available": True}

    def pin_set(self, password, new_pin):
        from . import pin
        if new_pin is not None and not pin.valid(new_pin):
            raise ApiError("A PIN is 4 to 6 digits.")
        if password != "polyos":
            raise ApiError("That password isn't right. Try again.", 403)
        self._pin = new_pin
        return self.pin_status()

    def pin_unlock(self, secret):
        return bool(getattr(self, "_pin", None)) and secret == self._pin

    def lock_unlock(self, password):
        self.unlock_throttle.check()
        time.sleep(0.5)
        if self.pin_unlock(password):
            self.unlock_throttle.succeeded()
            self.bus.publish("lock", locked=False)
            return {"ok": True}
        if password != "polyos":
            self.unlock_throttle.failed()
            raise ApiError("That password isn't right. Try again.", 403)
        self.unlock_throttle.succeeded()
        self.bus.publish("lock", locked=False)
        return {"ok": True}

    def lock_recover(self, key, password):
        from .recovery import looks_valid

        time.sleep(0.6)
        if not looks_valid(key):
            raise ApiError("That recovery key isn't right. 4 tries left.", 403)
        self.bus.publish("lock", locked=False)
        return {"ok": True}

    def greeter_recover(self, user, key, password):
        from .recovery import looks_valid

        time.sleep(0.6)
        if not looks_valid(key):
            raise ApiError("That recovery key isn't right. 4 tries left.", 403)
        return {"ok": True}

    def install_restart(self):
        self.bus.publish("power", action="reboot")

    def account_password(self, current, password):
        if current != "polyos":
            raise ApiError("That password isn't right. Try again.", 403)
        return {"ok": True}

    def account_recovery_key(self):
        from .recovery import generate

        if not self._admin_ready:
            raise NeedPassword()
        return {"key": generate()}

    def sysinfo(self):
        return {"os": "Debian GNU/Linux 13 (trixie)", "kernel": "6.12.41-amd64", "arch": "x86_64",
                "cpu": "Intel(R) Core(TM) i5-6300U CPU @ 2.40GHz", "cores": 4,
                "memoryBytes": 8 * 1024 ** 3, "diskTotal": 128 * 1000 ** 3, "diskFree": 81 * 1000 ** 3,
                "uptime": 3 * 3600 + 17 * 60, "hostname": "polyos"}

    def env(self):
        return {"dev": True, "composited": True, "live": self.live,
                "installer": "install-debian.desktop" if self.live else None,
                "panelHeight": PANEL_HEIGHT, "dockHeight": DOCK_HEIGHT,
                "dockMargin": DOCK_MARGIN, "version": __version__}

    def pick_wallpaper(self):
        raise ApiError("The file picker only works on a real PolyOS session", 501)

    def has_camera(self):
        return True  # the browser's own camera stands in

    def power_modes(self):
        from .power import MODE_INFO

        return {"modes": [{"id": k, "name": v[0], "description": v[1]} for k, v in MODE_INFO.items()],
                "profiles": ["power-saver", "balanced", "performance"], "switchable": True}

    def restart_shell(self):
        self.bus.publish("power", action="restart-shell")

    # ---- administrator access (password for the mock: "polyos") ---------------------------
    def admin_status(self):
        return {"ready": self._admin_ready}

    def admin_auth(self, password):
        time.sleep(0.5)
        if password != "polyos":
            raise ApiError("That password isn't right. Try again.", 403)
        self._admin_ready = True
        return {"ready": True}

    def _simulate(self, steps, seconds, finish=None, fail=None, restart=False):
        def runner(job, update):
            for i, message in enumerate(steps):
                update({"progress": i / len(steps), "message": message})
                for _ in range(10):
                    time.sleep(seconds / len(steps) / 10)
            if fail:
                update({"error": fail})
                return 1
            if finish:
                finish()
            if restart:
                update({"restart": True})
            update({"progress": 1.0, "message": "Done."})
            return 0
        return runner

    # ---- installer --------------------------------------------------------------------
    def install_probe(self):
        if not self.live:
            raise ApiError("PolyOS is already installed on this computer.", 409)
        time.sleep(1.0)
        if not hasattr(self, "_drives"):
            self._drives = self._sample_drives()
        uefi = True
        prober = {"/dev/nvme0n1p1": "Windows 11"}
        resize = {"/dev/nvme0n1p3": {"fs": "ntfs", "min": 131 * GB, "used": 131 * GB, "reason": None}}
        disks = [installer.describe_disk(copy.deepcopy(d["disk"]), copy.deepcopy(d["table"]), prober, uefi, "/dev/sdb", resize)
                 for d in self._drives]
        disks = installer.finish_install_options(disks, uefi)
        return {"uefi": uefi, "secureBoot": True, "ram": 8 * 1024 ** 3, "disks": disks,
                "minBytes": installer.MIN_ROOT, "liveDisk": "/dev/sdb", "bitlocker": installer.bitlocker_volumes(disks)}

    @staticmethod
    def _sample_drives():
        """A laptop with Windows 11, a second SSD, an empty hard drive and the PolyOS USB stick.

        POLYOS_MOCK_DRIVES=laptop is one SSD with Windows (C:) and a D: drive made for PolyOS;
        "bitlocker" is the same with BitLocker still on."""
        def part(path, number, size, fstype, label, parttype, mounts=()):
            return {"path": path, "number": number, "size": size, "fstype": fstype, "label": label,
                    "parttype": parttype, "mounts": list(mounts)}
        kind = os.environ.get("POLYOS_MOCK_DRIVES", "")
        if kind in ("laptop", "bitlocker"):
            c_fs = "BitLocker" if kind == "bitlocker" else "ntfs"
            disk = {"path": "/dev/nvme0n1", "size": 512 * GB, "model": "SK hynix BC711", "transport": "nvme",
                    "removable": False, "readonly": False, "table": "gpt", "mounts": [], "partitions": [
                        part("/dev/nvme0n1p1", 1, 260 * 1024 ** 2, "vfat", "SYSTEM", installer.ESP_GUID.lower()),
                        part("/dev/nvme0n1p2", 2, 16 * 1024 ** 2, "", "", installer.MSR_GUID.lower()),
                        part("/dev/nvme0n1p3", 3, 300 * GB, c_fs, "" if c_fs == "BitLocker" else "Windows", installer.MS_BASIC_GUID.lower()),
                        part("/dev/nvme0n1p4", 4, 210 * GB, "ntfs", "New Volume", installer.MS_BASIC_GUID.lower()),
                        part("/dev/nvme0n1p5", 5, 1024 ** 3, "ntfs", "Recovery", installer.WINRE_GUID.lower())]}
            s = 2048
            rows = []
            for p in disk["partitions"]:
                rows.append({"node": p["path"], "number": p["number"], "start": s, "size": p["size"] // 512, "type": "x"})
                s += p["size"] // 512
            usb = {"path": "/dev/sdb", "size": 32 * GB, "model": "SanDisk Ultra", "transport": "usb", "removable": True,
                   "readonly": False, "table": "dos", "mounts": [], "partitions": [
                       part("/dev/sdb1", 1, 32 * GB, "iso9660", "PolyOS", "0x0", ["/run/live/medium"])]}
            return [{"disk": disk, "table": {"label": "gpt", "sector": 512, "first": 2048, "last": disk["size"] // 512 - 34, "partitions": rows}},
                    {"disk": usb, "table": {"label": "dos", "sector": 512, "first": 0, "last": None, "partitions": []}}]
        nvme = {"path": "/dev/nvme0n1", "size": 512 * GB, "model": "Samsung SSD 970 EVO Plus", "transport": "nvme",
                "removable": False, "readonly": False, "table": "gpt", "mounts": [], "partitions": [
                    part("/dev/nvme0n1p1", 1, 100 * 1024 ** 2, "vfat", "SYSTEM", installer.ESP_GUID.lower()),
                    part("/dev/nvme0n1p2", 2, 16 * 1024 ** 2, "", "", installer.MSR_GUID.lower()),
                    part("/dev/nvme0n1p3", 3, 510 * GB, "ntfs", "Windows", installer.MS_BASIC_GUID.lower()),
                    part("/dev/nvme0n1p4", 4, 900 * 1024 ** 2, "ntfs", "Recovery", installer.WINRE_GUID.lower())]}
        table = {"label": "gpt", "sector": 512, "first": 2048, "last": nvme["size"] // 512 - 34, "partitions": [
            {"node": "/dev/nvme0n1p1", "number": 1, "start": 2048, "size": 204800, "type": installer.ESP_GUID.lower()},
            {"node": "/dev/nvme0n1p2", "number": 2, "start": 206848, "size": 32768, "type": "x"},
            {"node": "/dev/nvme0n1p3", "number": 3, "start": 239616, "size": 510 * GB // 512, "type": "x"},
            {"node": "/dev/nvme0n1p4", "number": 4, "start": 239616 + 510 * GB // 512, "size": 1843200, "type": "x"}]}
        ssd = {"path": "/dev/sdc", "size": 1000 * GB, "model": "Crucial MX500", "transport": "sata", "removable": False,
               "readonly": False, "table": "gpt", "mounts": [], "partitions": [
                   part("/dev/sdc1", 1, 700 * GB, "ntfs", "Games", installer.MS_BASIC_GUID.lower()),
                   part("/dev/sdc2", 2, 300 * GB, "ext4", "home", installer.LINUX_GUID.lower())]}
        ssd_table = {"label": "gpt", "sector": 512, "first": 2048, "last": ssd["size"] // 512 - 34, "partitions": [
            {"node": "/dev/sdc1", "number": 1, "start": 2048, "size": 700 * GB // 512, "type": "x"},
            {"node": "/dev/sdc2", "number": 2, "start": 2048 + 700 * GB // 512, "size": 300 * GB // 512, "type": "x"}]}
        hdd = {"path": "/dev/sda", "size": 1000 * GB, "model": "WDC WD10SPZX", "transport": "sata", "removable": False,
               "readonly": False, "table": None, "mounts": [], "partitions": []}
        usb = {"path": "/dev/sdb", "size": 32 * GB, "model": "SanDisk Ultra", "transport": "usb", "removable": True,
               "readonly": False, "table": "dos", "mounts": [], "partitions": [
                   part("/dev/sdb1", 1, 32 * GB, "iso9660", "PolyOS 0.2.0", "0x0", ["/run/live/medium"])]}
        return [{"disk": nvme, "table": table}, {"disk": ssd, "table": ssd_table}, {"disk": hdd, "table": None},
                {"disk": usb, "table": {"label": "dos", "sector": 512, "first": 0, "last": None, "partitions": []}}]

    def install_disk(self, action, disk, number=None, start=None, size=None):
        """Delete and New on the simulated drives (the real ones use polyos-admin disk)."""
        if not self.live:
            raise ApiError("PolyOS is already installed on this computer.", 409)
        if not hasattr(self, "_drives"):
            self._drives = self._sample_drives()
        drive = next((d for d in self._drives if d["disk"]["path"] == disk), None)
        if drive is None or disk == "/dev/sdb":
            raise ApiError("That drive can't be changed.")
        if not self._admin_ready:
            raise NeedPassword()
        time.sleep(0.6)
        raw, table = drive["disk"], drive["table"]
        if action == "delete":
            raw["partitions"] = [p for p in raw["partitions"] if p["number"] != number]
            table["partitions"] = [p for p in table["partitions"] if p["number"] != number]
            return {"ok": True}
        if table is None:
            table = drive["table"] = {"label": "gpt", "sector": 512, "first": 2048, "last": raw["size"] // 512 - 34, "partitions": []}
            raw["table"] = "gpt"
        region = next((r for r in installer.free_regions(table, raw["size"]) if r["start"] <= start < r["start"] + r["size"]
                       or start == 0), None)
        if region is None:
            raise ApiError("That unallocated space has changed. Press Refresh.")
        cursor = max(region["start"], start)
        esp_anywhere = any(installer.is_esp(p, d["disk"]["table"]) for d in self._drives for p in d["disk"]["partitions"])
        specs = []
        if not esp_anywhere:
            specs.append((cursor, installer.ESP_BYTES // 512, "vfat", "EFI", installer.ESP_GUID.lower()))
            cursor += installer.ESP_BYTES // 512
        count = min(size // 512 // 2048 * 2048, region["start"] + region["size"] - cursor)
        specs.append((cursor, count, "", "", installer.LINUX_GUID.lower()))
        for begin, sectors, fstype, label, ptype in specs:
            n = max([p["number"] for p in raw["partitions"]] + [0]) + 1
            node = installer.partition_node(disk, n)
            raw["partitions"].append({"path": node, "number": n, "size": sectors * 512, "fstype": fstype, "label": label,
                                      "parttype": ptype, "mounts": []})
            table["partitions"].append({"node": node, "number": n, "start": begin, "size": sectors, "type": ptype})
        table["partitions"].sort(key=lambda p: p["start"])
        return {"ok": True}

    def install_start(self, plan):
        if not self.live:
            raise ApiError("PolyOS is already installed on this computer.", 409)
        from . import polyaccount
        state = polyaccount.load(self._pa_home())
        plan = {**plan, "polyAccount": state if state.get("credential") else None}
        try:
            clean = installer.validate_plan(plan)
        except installer.InstallError as exc:
            raise ApiError(str(exc)) from None
        print("install plan:", json.dumps({"settings": clean["extraSettings"], "firstStart": clean["firstStart"],
                                           "polyAccount": bool(clean["polyAccount"])}), flush=True)
        steps = ["Preparing the disk…", "Formatting…", "Copying PolyOS to the disk… 20%", "Copying PolyOS to the disk… 55%",
                 "Copying PolyOS to the disk… 90%", "Setting up your computer…", "Creating your account…",
                 "Installing the boot loader…", "Finishing the boot menu…", "Cleaning up…"]
        if clean["mode"] == "alongside":
            steps.insert(0, "Making room: shrinking Windows 11…")
        if clean["mode"] == "custom":
            steps[0:1] = [f"Preparing {d}…" for d in clean["wipe"]] or ["Checking your partitions…"]
        return self.jobs.start("install", "Installing PolyOS", [], runner=self._simulate(steps, 14))

    # ---- Driver Manager ------------------------------------------------------------------
    def drivers_scan(self):
        time.sleep(0.6)
        from .drivers import extra_devices, recommend

        devices = [
            {"slot": "00:02.0", "className": "VGA compatible controller", "classId": "0300", "vendor": "Intel Corporation",
             "vendorId": "8086", "device": "UHD Graphics 620", "deviceId": "5917", "driver": "i915"},
            {"slot": "01:00.0", "className": "3D controller", "classId": "0302", "vendor": "NVIDIA Corporation",
             "vendorId": "10de", "device": "GP108M [GeForce MX150]", "deviceId": "1d10", "driver": "nouveau"},
            {"slot": "02:00.0", "className": "Network controller", "classId": "0280", "vendor": "Intel Corporation",
             "vendorId": "8086", "device": "Wireless 8265 / 8275", "deviceId": "24fd", "driver": "iwlwifi"},
            {"slot": "00:1f.3", "className": "Audio device", "classId": "0403", "vendor": "Intel Corporation",
             "vendorId": "8086", "device": "Sunrise Point-LP HD Audio", "deviceId": "9d71", "driver": "snd_hda_intel"},
        ]
        from .drivers import core_devices
        items = core_devices("model name\t: Intel(R) Core(TM) i7-8550U CPU @ 1.80GHz\nvendor_id\t: GenuineIntel\n",
                             "MemTotal:       16318412 kB\n", [{"name": "nvme0n1", "model": "Samsung SSD 970 EVO 512GB (NVMe drive)", "driver": "nvme"}],
                             ["Built-in screen"], "amd64")
        items += recommend(devices, "nvidia-driver", ["firmware-misc-nonfree"])
        # a 2-in-1 with a touchscreen, a pen and a webcam
        items += extra_devices([{"name": "ELAN9008:00 04F3:2C82", "pen": False}, {"name": "ELAN9008:00 04F3:2C82 Stylus", "pen": True}],
                               True, devices, ["accel"])
        for it in items:
            it["missing"] = [p for p in it["packages"] if p not in self._driver_state]
        self._driver_packages = {p for d in items for p in d["packages"]}
        return {"devices": items, "secureBoot": True, "computer": {"maker": "Dell", "model": "XPS 13 9370", "bios": "1.21.0",
                                                                   "biosDate": "03/14/2023", "laptop": True, "virtual": False}}

    def firmware_status(self):
        time.sleep(0.8)
        if getattr(self, "_fw_done", False):
            return {"available": True, "updates": []}
        return {"available": True, "updates": [
            {"device": "System Firmware", "vendor": "Dell Inc.", "current": "1.21.0", "version": "1.23.1",
             "summary": "Firmware for the Dell XPS 13 9370", "size": 21_000_000, "urgency": "high", "id": "bios"}]}

    def firmware_install(self):
        if not self._admin_ready:
            raise NeedPassword()

        def finish():
            self._fw_done = True
        return self.jobs.start("drivers", "Updating firmware", [], target="firmware",
                               runner=self._simulate(["Getting the newest firmware list from your computer's maker…",
                                                      "Installing firmware updates… Keep the computer plugged in."], 4, finish, restart=True))

    def drivers_install(self, packages):
        bad = [p for p in packages if p not in self._driver_packages]
        if bad or not packages:
            raise ApiError("Scan for drivers again, then pick from the list.")
        if not self._admin_ready:
            raise NeedPassword()

        def finish():
            self._driver_state.update(packages)
        runner = self._simulate(["Checking Debian for the latest versions…", "Downloading…", "Installing drivers…",
                                 "Configuring…"], 6, finish, restart=any(p.startswith("nvidia") for p in packages))
        return self.jobs.start("drivers", "Installing drivers", [], runner=runner,
                               on_done=lambda j: self.bus.publish("drivers"))

    # ---- updates (a simulated update service) --------------------------------------------------
    def _is_live(self):
        return self.live

    def _update_files(self):
        from . import autoupdate
        if not hasattr(self, "_upd_policy"):
            self._upd_policy = dict(autoupdate.DEFAULT_POLICY)
            self._upd_status = {"state": "idle", "lastCheck": time.time() - 2 * 3600, "latest": __version__, "available": False}
        return self._upd_policy, self._upd_status

    def _start_update_unit(self, kind):
        _policy, st = self._update_files()

        def work():
            if kind == "check":
                st.update(state="checking")
                self.bus.publish("updates")
                time.sleep(1.2)
                major, minor, patch = (int(x) for x in __version__.split(".")[:3])
                simulated = f"{major}.{minor}.{patch + 1}"  # a pretend next version
                if st.get("installed") != simulated:
                    st.update(latest=simulated, available=True, size=3_012_000, notes="Fixes and small improvements.\n"
                              "Poly Account: a faster sign-in and clearer messages.")
                st.update(state="idle", lastCheck=time.time(), error=None)
            elif kind == "tonight":
                st.update(tonight=True)
            elif kind == "now":
                if not st.get("available"):
                    st.update(error="PolyOS is already up to date.")
                else:
                    for state, secs in (("downloading", 1.5), ("installing", 2.0)):
                        st.update(state=state)
                        self.bus.publish("updates")
                        time.sleep(secs)
                    st.update(state="idle", installed=st["latest"], available=False, tonight=False, downloaded=None)
            self.bus.publish("updates")
        threading.Thread(target=work, daemon=True).start()

    def updates_policy(self, policy):
        from . import autoupdate
        current, _st = self._update_files()
        try:
            clean = autoupdate.validate_policy({**current, **policy})
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        if not self._admin_ready:
            raise NeedPassword()
        current.update(clean)
        self.bus.publish("updates")
        return self.updates_status()

    def updates_install(self, what):
        if self.live:
            raise ApiError("Install PolyOS first; updates install on the installed system.")
        if not self._admin_ready:
            raise NeedPassword()
        runner = self._simulate(["Checking Debian for the latest versions…", "Downloading…", "Installing updates…", "Configuring…"], 5, lambda: None)
        return self.jobs.start("update", "Installing Debian updates", [], target="system", runner=runner,
                               on_done=lambda j: self.bus.publish("updates"))

    # ---- PolyMarket ------------------------------------------------------------------------
    def store_list(self):
        data = store.for_arch(store.load())
        apps = [{**a, "installed": a["id"] in self._store_installed} for a in data["apps"]]
        return {"categories": data["categories"], "apps": apps, "flatpak": True}

    def store_action(self, app_id, action):
        app = self._store_app(app_id)
        if action == "remove" and app.get("system"):
            raise ApiError(f"{app['name']} is part of PolyOS and can't be removed.")
        if not self._admin_ready:
            raise NeedPassword()

        def finish():
            desktop = (app.get("desktop") or [None])[0]
            if action == "install":
                self._store_installed.add(app_id)
                if desktop and all(a["id"] != desktop for a in self._apps):
                    self._apps.append({"id": desktop, "name": app["name"], "description": app["summary"],
                                       "categories": [], "keywords": [], "icon": f"/icon/app/{desktop}", "hidden": False})
                    self._apps.sort(key=lambda a: a["name"].casefold())
            else:
                self._store_installed.discard(app_id)
                self._apps = [a for a in self._apps if a["id"] != desktop]
            self.bus.publish("apps", apps=self._apps)
        steps = (["Checking Debian for the latest versions…", "Downloading…", f"Installing {app['name']}…"]
                 if app["source"] == "debian" else ["Connecting to Flathub…", "Downloading runtime… 35%",
                                                    f"Installing {app['name']}… 80%"])
        if action == "remove":
            steps = [f"Removing {app['name']}…"]
        verb = "Installing" if action == "install" else "Removing"
        return self.jobs.start("store", f"{verb} {app['name']}", [], target=app_id,
                               runner=self._simulate(steps, 5 if action == "install" else 2, finish),
                               on_done=lambda j: self.bus.publish("store"), queue=True)

    def _add_store_app(self, app):
        desktop = (app.get("desktop") or [None])[0]
        if desktop and all(a["id"] != desktop for a in self._apps):
            self._apps.append({"id": desktop, "name": app["name"], "description": app["summary"],
                               "categories": [], "keywords": [], "icon": f"/icon/app/{desktop}", "hidden": False})
            self._apps.sort(key=lambda a: a["name"].casefold())

    # ---- editions, cloud gaming and security (simulated) --------------------------------------
    def packs(self):
        data = store.for_arch(store.load())
        out = {}
        by_id = {a["id"]: a for a in self.store_list()["apps"]}
        for name, info in data["packs"].items():
            out[name] = {"name": info["name"], "summary": info["summary"],
                         "apps": [{**by_id[aid], "default": d} for aid, d in info["apps"]]}
        return {"packs": out, "edition": self.settings.get("edition")}

    def pack_install(self, name, ids):
        info = store.pack(store.load(), name)
        if info is None or not ids or any(i not in {a for a, _ in info["apps"]} for i in ids):
            raise ApiError("Pick apps from the list.")
        if not self._admin_ready:
            raise NeedPassword()
        apps = store.validate(store.load())

        def finish():
            for i in ids:
                self._store_installed.add(i)
                self._add_store_app(apps[i])
            self.bus.publish("apps", apps=self._apps)
            self.update_settings({"editionSetup": True})
            self.add_desktop_shortcuts([(apps[i].get("desktop") or [None])[0] for i in ids])
        steps = ["Checking Debian for the latest versions…", "Connecting to Flathub…",
                 *[f"Installing {apps[i]['name']} ({n + 1} of {len(ids)})…" for n, i in enumerate(ids)]]
        return self.jobs.start("pack", f"Setting up {info['name']}", [], target=name,
                               runner=self._simulate(steps, 6, finish), on_done=lambda j: self.bus.publish("store"))

    # ---- Settings > Apps (simulated origins; startup entries in a sample folder) ------------------
    def _startup_dirs(self):
        folder = self.files.home.parent / "dev-autostart"
        if not folder.is_dir():
            folder.mkdir(parents=True)
            samples = {"nm-applet.desktop": ("Network", "Wi-Fi and network connections in the tray"),
                       "blueman.desktop": ("Bluetooth Manager", "Bluetooth devices"),
                       "org.gnome.SettingsDaemon.Power.desktop": ("Power", "GNOME power management"),
                       "print-applet.desktop": ("Print Queue Applet", "Shows printing jobs"),
                       "polyos-agent.desktop": ("PolyOS", "PolyOS's own helper")}
            for name, (title, comment) in samples.items():
                only = "OnlyShowIn=GNOME;\n" if "GNOME" in name else ""
                (folder / name).write_text(f"[Desktop Entry]\nType=Application\nName={title}\nComment={comment}\n"
                                           f"Exec=true\n{only}", "utf-8")
        return [folder]

    def _app_origin(self, desktop_id):
        if desktop_id.startswith(("com.", "org.", "net.", "md.", "us.", "io.")) and desktop_id not in (
                "org.gnome.Calculator.desktop", "org.xfce.mousepad.desktop", "org.xfce.ristretto.desktop"):
            return {"kind": "flatpak", "path": f"/var/lib/flatpak/exports/share/applications/{desktop_id}",
                    "ref": desktop_id[:-8], "user": False}
        return {"kind": "debian", "path": f"/usr/share/applications/{desktop_id}"}

    def _debian_owners(self, paths):
        owners = {"firefox-esr": "firefox-esr", "thunar": "thunar", "xfce4-terminal": "xfce4-terminal",
                  "code": "code", "libreoffice-writer": "libreoffice-writer", "steam": "steam-installer"}
        return {p: owners.get(Path(p).stem, Path(p).stem.split(".")[-1].lower()) for p in paths}

    def startup_add(self, desktop_id):
        app = next((a for a in self._apps if a["id"] == desktop_id), None)
        if app is None:
            raise ApiError("That app can't start automatically.")
        folder = startup.user_dir(self.files.home)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / desktop_id).write_text(f"[Desktop Entry]\nType=Application\nName={app['name']}\nExec=true\n", "utf-8")
        return {"startup": startup.entries(self.files.home, self._startup_dirs())}

    def app_uninstall(self, desktop_id):
        info = next((a for a in self.apps_manage()["apps"] if a["id"] == desktop_id), None)
        if info is None or not info["removable"]:
            raise ApiError("That app is part of PolyOS, or it's already gone.")
        if not self._admin_ready:
            raise NeedPassword()

        def finish():
            self._apps = [a for a in self._apps if a["id"] != desktop_id]
            self._store_installed = {i for i in self._store_installed
                                     if desktop_id not in (store.validate(store.load()).get(i, {}).get("desktop") or [])}
            self.bus.publish("apps", apps=self._apps)
        return self.jobs.start("app", f"Removing {info['name']}", [], target=desktop_id,
                               runner=self._simulate(["Removing the app…", "Tidying up…"], 3, finish),
                               on_done=lambda j: self.bus.publish("store"))

    def _apps_changed(self):
        from . import gaming

        have = set(gaming.enabled(self.settings, self.files.home))
        self._apps = [a for a in self._apps if not a["id"].startswith(gaming.PREFIX)]
        for cid in have:
            name, _url, summary = gaming.CLOUD[cid]
            self._apps.append({"id": f"{gaming.PREFIX}{cid}.desktop", "name": name, "description": summary,
                               "categories": ["Game"], "keywords": [], "icon": f"/icon/app/{gaming.PREFIX}{cid}.desktop", "hidden": False})
        self._apps.sort(key=lambda a: a["name"].casefold())
        self.bus.publish("apps", apps=self._apps)

    _security = {"firewall": True, "updates": True}

    def security_status(self):
        return {**self._security, "apparmor": True, "secureBoot": True, "recoveryKey": True,
                "lockOnSleep": self.settings.get("lockOnSleep")}

    def security_set(self, what, on):
        if what not in self._security:
            raise ApiError("Unknown security setting.")
        if not self._admin_ready:
            raise NeedPassword()

        def finish():
            self._security = {**self._security, what: on}
        return self.jobs.start("security", f"{what} {'on' if on else 'off'}", [], target=what,
                               runner=self._simulate([f"Turning {what} {'on' if on else 'off'}…"], 1.5, finish),
                               on_done=lambda j: self.bus.publish("security"))

    # ---- Task Manager -----------------------------------------------------------------------
    def procs(self):
        rnd = self._procs_seed
        apps = []
        for w in self.windows():
            app = next((a for a in self._apps if a["id"] == w["appId"]), None)
            base = 420 if "chrome" in (w["appId"] or "") else 80
            apps.append({"pid": 4000 + w["xid"] % 997, "name": app["name"] if app else w["title"], "title": w["title"],
                         "icon": w["icon"], "cpu": round(rnd.uniform(0, 9 if base > 100 else 3), 1),
                         "memory": int((base + rnd.uniform(-10, 40)) * 1024 ** 2), "count": 6 if base > 100 else 1,
                         "status": "Running", "protected": False})
        names = [("pipewire-pulse", 12), ("NetworkManager applet", 22), ("xfce4-notifyd", 18), ("gvfs-udisks2-volume-monitor", 9),
                 ("tracker-miner-fs", 34), ("blueman-applet", 26), ("light-locker", 14), ("at-spi2-registryd", 6)]
        background = [{"pid": 2000 + i * 17, "name": n, "cmdline": f"/usr/bin/{n}", "cpu": round(rnd.uniform(0, 1.2), 1),
                       "memory": int((m + rnd.uniform(0, 4)) * 1024 ** 2), "count": 1, "status": "Running", "protected": False}
                      for i, (n, m) in enumerate(names)]
        desktop = [{"pid": 1200 + i, "name": n, "cmdline": n, "cpu": round(rnd.uniform(0, c), 1), "memory": int(m * 1024 ** 2),
                    "count": 1, "status": "Running", "protected": True}
                   for i, (n, c, m) in enumerate([("polyos-shell", 3, 160), ("openbox", 0.5, 18), ("picom", 2, 40),
                                                   ("Xorg", 2, 90), ("polyos-session", 0.1, 21)])]
        cpu = round(min(100, sum(a["cpu"] for a in apps + background + desktop) + rnd.uniform(2, 6)), 1)
        return {"apps": sorted(apps, key=lambda r: -r["cpu"]), "background": sorted(background, key=lambda r: -r["cpu"]),
                "desktop": desktop,
                "perf": {"cpu": cpu, "cores": [round(max(0, min(100, cpu + rnd.uniform(-8, 8))), 1) for _ in range(4)],
                         "cpuModel": "Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz", "memTotal": 8 * 1024 ** 3,
                         "memUsed": int((2.6 + len(apps) * 0.35 + rnd.uniform(0, 0.2)) * 1024 ** 3),
                         "memCached": int(1.4 * 1024 ** 3), "swapTotal": 4 * 1024 ** 3, "swapUsed": 0,
                         "processes": 180 + len(apps) * 6, "threads": 920, "uptime": int(time.monotonic()) % 86400,
                         "netDown": int(rnd.uniform(0, 400_000)), "netUp": int(rnd.uniform(0, 60_000)),
                         "diskRead": int(rnd.uniform(0, 2_000_000)), "diskWrite": int(rnd.uniform(0, 800_000))}}

    def procs_end(self, pid, force):
        with self._lock:
            win = next((w for w in self._windows if 4000 + w["xid"] % 997 == pid), None)
        if win is not None:
            return self.window_action(win["xid"], "close")
        if 1200 <= pid < 1300:
            raise ApiError("That's part of PolyOS itself and can't be ended here.", 403)


def seed_home(home: Path) -> None:
    """A small sandbox home folder for the Files app in dev mode."""
    if (home / ".seeded").exists():
        return
    samples = {
        "Documents/Welcome to PolyOS.txt": "Everything in this folder is a sandbox for `python main.py dev`.\n",
        "Documents/School/Chemistry notes.md": "# Chemistry\n\n- Stoichiometry\n- Gas laws\n",
        "Documents/School/History essay.docx": "",
        "Documents/Budget.xlsx": "",
        "Documents/Launch deck.pptx": "",
        "Downloads/polyos-0.1.0-trixie-amd64.iso": "",
        "Downloads/scratch-project.sb3": "",
        "Downloads/report.pdf": "%PDF-1.4\n",
        "Downloads/archive.tar.gz": "",
        "Music/Lo-fi mix.mp3": "",
        "Videos/Launch trailer.mp4": "",
        "Desktop/Ideas.txt": "PolyOS ideas\n",
        "Projects/jarvis/jarvis.py": "print('hello from JARVIS')\n",
        ".bashrc": "# hidden file\n",
    }
    for rel, text in samples.items():
        target = home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, "utf-8")
    (home / "Pictures").mkdir(parents=True, exist_ok=True)
    for name in ("polyos-dusk.jpg", "polyos-violet.jpg", "pixapoly.jpg"):
        src = paths.WALLPAPER_DIR / name
        if src.exists():
            shutil.copy(src, home / "Pictures" / name)
    (home / ".seeded").write_text("1")
