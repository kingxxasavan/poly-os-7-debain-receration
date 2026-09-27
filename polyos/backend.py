"""The backend contract the web UI talks to, plus logic shared by every backend."""

from __future__ import annotations

import getpass
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import __version__, devmode, drivers, gaming, paths, security, startup, store
from .core import DEFAULTS, IMAGE_TYPES, ApiError, EventBus, Settings, bundled_icon, letter_icon, log
from .files import FileSystem
from .privileged import Jobs
from .vara import Vara, complete
from .widgets import Widgets

# Floating dock geometry in logical px. The dock window is the bar itself, inset from the
# screen edges; openbox keeps PANEL_HEIGHT (times the UI scale) free for maximized windows.
DOCK_HEIGHT = 52
DOCK_MARGIN = 8
PANEL_HEIGHT = DOCK_HEIGHT + DOCK_MARGIN + 4

# Popup sizes in logical px. "taskmenu" height is supplied by the panel (it depends on the items).
# Full-screen popups cover the monitor above the dock; their size is filled in by the shell.
FULLSCREEN_POPUPS = {"launcher", "power"}
TALL_POPUPS = {"widgets"}  # full height at the left edge, like the Windows widgets board
POPUP_SIZES = {
    "start": (400, 596),  # the PolyOS Home Menu
    "launcher": (0, 0),
    "power": (0, 0),
    "run": (460, 188),
    "vara": (480, 680),
    "quick": (360, 326),  # the Wi-Fi list asks for more height via the "height" field
    "calendar": (320, 390),
    "taskmenu": (240, 200),
    "quickmenu": (264, 468),  # Super+X: the Windows-style quick link menu
    "widgets": (760, 0),
}
RECENT_LIMIT = 6
WALLPAPER_NAMES = {"polyos-prism": "Crystal", "polyos-amethyst": "Amethyst", "polyos-dusk": "Dusk",
                   "polyos-violet": "Violet", "polyos-night": "Night", "polyos-crystal": "Facets", "pixapoly": "PIXAPoLY"}
# Built-ins listed first in Settings, in this order; the rest follow alphabetically.
WALLPAPER_ORDER = ("polyos-prism", "polyos-amethyst")
POWER_ACTIONS = ("lock", "logout", "suspend", "reboot", "poweroff")
RUN_TARGETS = ("terminal", "files", "browser")
MIXER_TABS = {"playback": 1, "recording": 2, "output": 3, "input": 4, "configuration": 5}  # pavucontrol --tab
OPEN_APPS = ("settings", "files", "setup", "taskmgr", "drivers", "store", "camera")
INSTALL_APP = "polyos-install.desktop"  # "Install PolyOS 7": only while trying PolyOS from the USB
CAMERA_APP = "polyos-camera.desktop"  # listed only when a webcam is connected
CAMERA_TYPES = {"photo": {"image/jpeg": ".jpg", "image/png": ".png"},
                "video": {"video/webm": ".webm", "video/mp4": ".mp4"}}
CAMERA_MAX_BYTES = {"photo": 30 * 1024 * 1024, "video": 1024 * 1024 * 1024}
# Launcher entries most people never need (Settings > Apps shows them again).
HIDDEN_APPS = {
    "thunar.desktop", "thunar-bulk-rename.desktop", "thunar-settings.desktop", "thunar-volman-settings.desktop",
    "org.xfce.thunar.desktop", "pavucontrol.desktop", "org.pulseaudio.pavucontrol.desktop", "arandr.desktop",
    "nm-connection-editor.desktop", "light-locker-settings.desktop", "xfce4-notifyd-config.desktop",
    "org.xfce.xfce4-notifyd-config.desktop", "im-config.desktop", "calamares.desktop", "install-debian.desktop",
    "vim.desktop", "htop.desktop", "debian-xterm.desktop", "debian-uxterm.desktop", "xterm.desktop",
    "uxterm.desktop", "display-im6.q16.desktop", "display-im7.q16.desktop", "yelp.desktop", "obconf.desktop",
    "lightdm-gtk-greeter-settings.desktop", "blueman-adapters.desktop", "org.freedesktop.IBus.Setup.desktop",
    "ibus-setup.desktop", "qv4l2.desktop", "qvidcap.desktop", "org.gnome.FileRoller.desktop",
    "org.xfce.mousepad-settings.desktop", "xfce4-terminal-settings.desktop", "lxpolkit.desktop", "picom.desktop",
    "compton.desktop", "openbox.desktop", "debian-reference-common.desktop", "python3.13.desktop",
    "python3.11.desktop", "nvidia-settings.desktop", "org.gnome.Evince-previewer.desktop", "polyos-setup.desktop",
    "info.desktop", "bssh.desktop", "bvnc.desktop", "avahi-discover.desktop", "jconsole.desktop",
    "policytool.desktop", "gcr-prompter.desktop", "gcr-viewer.desktop", "org.gnome.seahorse.Application.desktop",
    "firefox-esr-safe.desktop", "nm-applet.desktop", "xfce4-about.desktop", "org.xfce.volman.desktop",
}
# Friendlier names for Debian's default apps.
DISPLAY_NAMES = {
    "firefox-esr.desktop": "Firefox", "org.xfce.mousepad.desktop": "Text Editor", "xfce4-terminal.desktop": "Terminal",
    "org.xfce.ristretto.desktop": "Image Viewer", "xfce4-screenshooter.desktop": "Screenshot",
    "blueman-manager.desktop": "Bluetooth", "org.gnome.Evince.desktop": "Documents",
}
# Clicking the button that opened a popup first blurs it (closing it) and then
# toggles it again; ignore a reopen of the same popup this soon after a close.
REOPEN_GUARD = 0.35


def panel_margin(settings: dict) -> int:
    """Logical px openbox keeps free at the bottom for maximized windows."""
    if settings.get("taskbarAutoHide"):
        return 0  # maximized windows fill the screen; the taskbar slides over them
    return DOCK_HEIGHT if settings.get("taskbarStyle") == "full" else PANEL_HEIGHT


def dock_geometry(settings: dict, width: int, height: int) -> tuple[int, int, int, int]:
    """(x, y, w, h) of the taskbar on a monitor of this size (monitor-relative)."""
    if settings.get("taskbarStyle") == "full":
        return 0, height - DOCK_HEIGHT, width, DOCK_HEIGHT
    return DOCK_MARGIN, height - DOCK_MARGIN - DOCK_HEIGHT, width - 2 * DOCK_MARGIN, DOCK_HEIGHT


class Backend:
    """Subclasses: MockBackend (dev in a browser) and DesktopShell (the real session)."""

    dev = False

    def __init__(self, settings: Settings, bus: EventBus, home: Path | None = None):
        self.settings = settings
        self.bus = bus
        self.files = FileSystem(home or Path.home())
        self.vara = Vara(settings.path.parent / "vara.json", bus, self.files.home)  # ~/.config/polyos/vara.json
        self.vara.on_attention = self._vara_attention
        self.jobs = Jobs(bus)
        self._driver_packages: set[str] = set()
        self.widgets = Widgets(settings.path.parent / "widgets.json", self.files.home)
        self._popup_lock = threading.Lock()
        self._popup: dict | None = None
        self._popup_key: str | None = None
        self._last_closed: tuple[str, float] | None = None
        self.unlock_throttle = security.Throttle()

    # ---- implemented by subclasses -------------------------------------------------
    def apps(self) -> list[dict]: raise NotImplementedError
    def windows(self) -> list[dict]: raise NotImplementedError
    def launch(self, app_id: str): raise NotImplementedError
    def window_action(self, xid: int, action: str): raise NotImplementedError
    def system_status(self) -> dict: raise NotImplementedError
    def set_volume(self, level=None, delta=None, muted=None, toggle_mute=False): raise NotImplementedError
    def set_brightness(self, level=None, delta=None): raise NotImplementedError
    def wifi_list(self) -> dict: raise NotImplementedError
    def wifi_connect(self, ssid: str, password: str | None): raise NotImplementedError
    def wifi_forget(self, ssid: str): raise NotImplementedError
    def wifi_enable(self, enabled: bool): raise NotImplementedError
    def power(self, action: str): raise NotImplementedError
    def sysinfo(self) -> dict: raise NotImplementedError
    def env(self) -> dict: raise NotImplementedError
    def app_icon(self, app_id: str) -> tuple[bytes, str]: raise NotImplementedError
    def window_icon(self, xid: int) -> tuple[bytes, str]: raise NotImplementedError
    def open_app(self, name: str, page: str | None = None): raise NotImplementedError
    def run_default(self, what: str): raise NotImplementedError
    def run_command(self, command: str): raise NotImplementedError
    def open_path(self, path: str): raise NotImplementedError
    def terminal_at(self, path: str): raise NotImplementedError
    def pick_wallpaper(self): raise NotImplementedError
    def restart_shell(self): raise NotImplementedError
    def finish_setup(self): raise NotImplementedError

    # the login screen (only the greeter backend and the dev mock implement these)
    def greeter_state(self) -> dict: raise ApiError("only available on the login screen", 404)
    def greeter_login(self, user: str, password: str, session: str | None): raise ApiError("only available on the login screen", 404)
    def greeter_power(self, action: str): raise ApiError("only available on the login screen", 404)
    def theme_icon(self, name: str) -> tuple[bytes, str]:
        names = [n for n in name.split(",") if n][:6]
        icon = bundled_icon(names)
        if icon:
            return icon, "image/svg+xml"
        return letter_icon((names[0] if names else "?").split(".")[-1] or "?"), "image/svg+xml"

    def lock(self): raise ApiError("locking isn't available here", 404)
    def lock_unlock(self, password: str): raise ApiError("locking isn't available here", 404)
    def lock_recover(self, key: str, password: str): raise ApiError("locking isn't available here", 404)
    def greeter_recover(self, user: str, key: str, password: str): raise ApiError("only available on the login screen", 404)
    def procs(self) -> dict: raise NotImplementedError
    def procs_end(self, pid: int, force: bool): raise NotImplementedError
    def _show_popup(self, popup: dict) -> None: pass
    def _hide_popup(self) -> None: pass

    # ---- shared --------------------------------------------------------------------
    def user(self) -> dict:
        name = getpass.getuser()
        full = name
        try:
            import pwd

            full = pwd.getpwnam(name).pw_gecos.split(",")[0].strip() or name
        except (ImportError, KeyError):
            pass
        if full.lower() in ("debian live user", "live user"):  # the live USB's account
            full = ""
        return {"name": name, "fullName": full or name.capitalize()}

    def state(self) -> dict:
        return {
            "version": __version__,
            "user": self.user(),
            "hostname": socket.gethostname(),
            "settings": self.settings.snapshot(),
            "apps": self.apps(),
            "windows": self.windows(),
            "system": self.system_status(),
            "env": self.env(),
            "popup": self._popup,
        }

    def note_launch(self, app_id: str) -> None:
        """Remember an opened app for the Start menu's Recent list (and Vara's sense of your habits)."""
        if not self.settings.get("keepRecent"):
            return
        try:
            name = next((a["name"] for a in self.apps() if a["id"] == app_id), app_id.removesuffix(".desktop"))
            self.vara.habits.opened(name)
        except (OSError, KeyError, ValueError):
            pass
        recent = [a for a in self.settings.get("recent") if a != app_id]
        try:
            self.update_settings({"recent": [app_id, *recent][:RECENT_LIMIT]})
        except ApiError:
            pass  # an id that fails validation is simply not remembered

    # ---- Files app: every change tells open Files windows to refresh ------------------------
    def _files_changed(self, *folders: str) -> None:
        self.bus.publish("files", folders=sorted({str(Path(f)) for f in folders if f}))

    def files_mkdir(self, parent, name):
        entry = self.files.mkdir(parent, name)
        self._files_changed(parent)
        return entry

    def files_new_file(self, parent, name):
        entry = self.files.new_file(parent, name)
        self._files_changed(parent)
        return entry

    def files_rename(self, path, name):
        entry = self.files.rename(path, name)
        self._files_changed(str(Path(path).parent))
        return entry

    def files_transfer(self, sources, dest, move):
        done = (self.files.move if move else self.files.copy)(sources, dest)
        self._files_changed(dest, *[str(Path(s).parent) for s in sources] if move else [])
        return {"entries": done}

    def files_trash(self, paths):
        count = self.files.trash_paths(paths)
        self._files_changed(*[str(Path(p).parent) for p in paths], "trash:///")
        return {"count": count}

    def files_restore(self, names):
        count = self.files.restore(names)
        self._files_changed("trash:///", str(self.files.home))
        return {"count": count}

    def files_empty_trash(self):
        count = self.files.empty_trash()
        self._files_changed("trash:///")
        return {"count": count}

    def file_raw(self, path: str) -> Path:
        """Image previews for the Files app (only image types are ever served)."""
        p = self.files.resolve(path)
        if p.suffix.lower() not in IMAGE_TYPES or not p.is_file():
            raise ApiError("not an image", 404)
        if p.stat().st_size > 40 * 1024 * 1024:
            raise ApiError("image too large to preview", 413)
        return p

    # ---- administrator access and background jobs ----------------------------------------
    def admin_status(self) -> dict:
        return {"ready": self.jobs.admin.ready()}

    def admin_auth(self, password: str):
        self.jobs.admin.authenticate(password)
        return {"ready": True}

    # ---- installer (live USB only) -----------------------------------------------------
    def install_probe(self) -> dict:
        if not self.env()["live"]:
            raise ApiError("PolyOS is already installed on this computer.", 409)
        return self.jobs.admin.call(["probe"], timeout=600)

    def install_start(self, plan: dict) -> dict:
        if not self.env()["live"]:
            raise ApiError("PolyOS is already installed on this computer.", 409)
        from . import polyaccount
        from .installer import InstallError, validate_plan

        # connected to Poly Account during setup: the installed PolyOS stays connected
        state = polyaccount.load(self._pa_home())
        plan = {**plan, "polyAccount": state if state.get("credential") else None}
        try:
            clean = validate_plan(plan)
        except InstallError as exc:
            raise ApiError(str(exc)) from None
        path = paths.runtime_dir() / "install-plan.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(clean, fh)
        return self.jobs.start("install", "Installing PolyOS", ["install", str(path)])

    def install_disk(self, action: str, disk: str, number: int | None = None, start: int | None = None,
                     size: int | None = None) -> dict:
        """The drive screen's Delete and New: change the partitions right away (live USB only)."""
        if not self.env()["live"]:
            raise ApiError("PolyOS is already installed on this computer.", 409)
        if action == "delete" and number is not None:
            args = ["disk", "delete", disk, str(number)]
        elif action == "new" and start is not None and size:
            args = ["disk", "new", disk, str(start), str(size)]
        else:
            raise ApiError("Choose a partition or unallocated space.")
        return self.jobs.admin.call(args, timeout=300)

    def install_restart(self):
        """Restart right away after installing (a normal restart waits on the live system's services).

        The finish screen asks for the USB drive to be removed first, so polyos-admin may not start
        (it lives on the drive); then the ordinary restart takes over.
        """
        if not self.env()["live"]:
            raise ApiError("PolyOS is already installed on this computer.", 409)
        try:
            code = self.jobs.admin.stream(["reboot"], lambda _event: None)
        except (ApiError, OSError):
            code = 1
        if code:
            self.power("reboot")

    # ---- your account -------------------------------------------------------------------
    def _account_request(self, payload: dict) -> None:
        path = paths.runtime_dir() / "account-request.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        errors: list[str] = []
        rc = self.jobs.admin.stream(["account", str(path)], lambda e: errors.append(e["error"]) if "error" in e else None)
        path.unlink(missing_ok=True)
        if errors or rc != 0:
            raise ApiError(errors[-1] if errors else "That didn't work. Try again.", 500)

    def account_password(self, current: str, password: str):
        if not password or len(password) > 256 or "\n" in password:
            raise ApiError("Choose a new password.")
        self.jobs.admin.authenticate(current)  # proves it's you (and unlocks sudo)
        self._account_request({"password": password})
        return {"ok": True}

    def account_recovery_key(self):
        from .recovery import generate

        if not self.jobs.admin.ready():
            from .privileged import NeedPassword

            raise NeedPassword()
        key = generate()
        self._account_request({"recoveryKey": key})
        return {"key": key}

    # ---- Driver Manager ------------------------------------------------------------------
    def drivers_scan(self) -> dict:
        result = drivers.scan()
        self._driver_packages = {p for d in result["devices"] for p in d["packages"]}
        return result

    def drivers_install(self, packages: list[str]) -> dict:
        bad = [p for p in packages if p not in self._driver_packages or not drivers.DRIVER_PACKAGE_RE.match(p)]
        if bad or not packages:
            raise ApiError("Scan for drivers again, then pick from the list.")
        def done(job):
            if job.get("state") == "done" and "onboard" in packages:
                self.update_settings({"desktopOpen": "single"})  # a touchscreen: icons open with one tap
            self.bus.publish("drivers")
        return self.jobs.start("drivers", "Installing drivers", ["drivers", *packages], on_done=done)

    # ---- PolyMarket ------------------------------------------------------------------------
    def _store_app(self, app_id: str) -> dict:
        app = store.validate(store.load()).get(app_id)
        if app is None:
            raise ApiError("That app isn't in PolyMarket.", 404)
        return app

    def store_list(self) -> dict:
        return store.catalog_with_status(store.load())

    def store_action(self, app_id: str, action: str) -> dict:
        app = self._store_app(app_id)
        if action == "remove" and app.get("system"):
            raise ApiError(f"{app['name']} is part of PolyOS and can't be removed.")
        verb = "Installing" if action == "install" else "Removing"
        return self.jobs.start("store", f"{verb} {app['name']}", ["store", action, app_id], target=app_id,
                               on_done=lambda job: self.bus.publish("store"))

    def store_open(self, app_id: str):
        app = self._store_app(app_id)
        ids = {a["id"] for a in self.apps()}
        target = next((d for d in app.get("desktop", []) if d in ids), None)
        if target is None:
            raise ApiError(f"{app['name']} runs from the Terminal." if not app.get("desktop") else
                           f"{app['name']} isn't installed yet.", 404)
        return self.launch(target)

    def performance(self) -> dict:
        """The login and lock screens' "Performance: Optimal" card (load average and free memory)."""
        try:
            load = os.getloadavg()[0] / (os.cpu_count() or 1)
        except (AttributeError, OSError):
            return {"level": "optimal"}
        avail = 1.0
        try:
            info = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    info[key] = int(rest.split()[0])
            avail = info["MemAvailable"] / info["MemTotal"]
        except (OSError, KeyError, ValueError, IndexError, ZeroDivisionError):
            pass
        level = "optimal" if load < 0.7 and avail > 0.15 else "busy" if load < 1.5 and avail > 0.05 else "high"
        return {"level": level, "load": round(load, 2), "memoryFree": round(avail, 2)}

    def power_modes(self) -> dict:
        """Settings > Power: the four PolyOS modes and whether this computer can switch profiles."""
        from .power import MODE_INFO, available_profiles

        offered = available_profiles()
        return {"modes": [{"id": k, "name": v[0], "description": v[1]} for k, v in MODE_INFO.items()],
                "profiles": offered, "switchable": bool(offered)}

    # ---- Camera app ----------------------------------------------------------------------
    def has_camera(self) -> bool:
        from .power import has_camera

        return has_camera()

    def camera_status(self) -> dict:
        folder = self.files.home / "Pictures" / "Camera"
        return {"camera": self.has_camera(), "allowed": self.settings.get("cameraAccess"),
                "micAllowed": self.settings.get("micAccess"), "folder": str(folder)}

    def camera_save(self, kind: str, ctype: str, data: bytes) -> dict:
        """Save a photo or video from the Camera app to ~/Pictures/Camera."""
        if not self.settings.get("cameraAccess"):
            raise ApiError("Camera access is turned off in Settings > Privacy & security.", 403)
        ext = CAMERA_TYPES.get(kind, {}).get(ctype.split(";")[0].strip().lower())
        if ext is None:
            raise ApiError("unsupported photo or video format", 415)
        if not data:
            raise ApiError("nothing was captured")
        if kind == "photo" and not (data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG")):
            raise ApiError("that isn't a photo", 415)
        folder = self.files.home / "Pictures" / "Camera"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        prefix = "Photo" if kind == "photo" else "Video"
        for n in range(100):
            target = folder / f"{prefix} {stamp}{f' ({n + 1})' if n else ''}{ext}"
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                break
            except FileExistsError:
                continue
        else:
            raise ApiError("couldn't pick a file name", 500)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        self._files_changed(str(folder))
        return {"path": str(target), "name": target.name}

    # ---- editions: Gaming and Developer packs ----------------------------------------------
    def packs(self) -> dict:
        return {"packs": store.packs_with_status(store.load()), "edition": self.settings.get("edition")}

    def pack_install(self, name: str, ids: list[str]) -> dict:
        info = store.pack(store.load(), name)
        if info is None:
            raise ApiError("That edition doesn't exist.", 404)
        allowed = {aid for aid, _default in info["apps"]}
        if not ids or any(i not in allowed for i in ids):
            raise ApiError(f"Pick apps from the {info['name']} list.")

        def done(job):
            self.bus.publish("store")
            if job["state"] == "done":
                self.update_settings({"editionSetup": True})
                apps = store.validate(store.load())
                self.add_desktop_shortcuts([store.installed_launcher(apps[i], self.files.home) for i in ids])
        return self.jobs.start("pack", f"Setting up {info['name']}", ["pack", name, *ids], target=name, on_done=done)

    def add_desktop_shortcuts(self, desktop_ids: list[str | None]) -> None:
        """Put newly installed apps on the desktop (edition packs), after the ones already there."""
        icons = list(self.settings.get("desktopIcons") or [])
        new = [d for d in dict.fromkeys(desktop_ids) if d and d not in icons]
        if new and len(icons) < 24:
            self.update_settings({"desktopIcons": (icons + new)[:24]})

    # ---- Settings > Sound and Display ----------------------------------------------------------
    def sound_devices(self) -> dict:
        from . import system
        return system.sound_devices()

    def sound_set_device(self, kind: str, name: str) -> dict:
        from . import system
        try:
            system.set_default_device(kind, name)
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        return self.sound_devices()

    def sound_set_input(self, level: int | None, muted: bool | None) -> dict:
        from . import system
        system.set_input(level, muted)
        return self.sound_devices()

    def sound_mixer(self, tab: str) -> dict:
        """Advanced sound settings: the PulseAudio mixer (installed with PolyOS) on the matching tab."""
        from . import system
        if not system.have("pavucontrol"):
            raise ApiError("The advanced sound mixer (pavucontrol) isn't installed.")
        system.spawn(["pavucontrol", f"--tab={MIXER_TABS[tab]}"])
        return {"opened": tab}

    def displays_list(self) -> dict:
        from . import display, system
        rc, out = system.run(["xrandr", "--query"], 10) if system.have("xrandr") else (1, "")
        return {"outputs": display.parse_xrandr(out) if rc == 0 else [], "graphics": self.graphics_info()}

    def displays_set(self, name: str, size: str | None, rate: float | None, rotation: str | None, primary: bool) -> dict:
        """Change one screen right away; the setting keeps it for next time (the UI asks to keep or undo)."""
        from . import display, system
        outputs = self.displays_list()["outputs"]
        try:
            display.validate(outputs, name, size, rate, rotation)
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        rc, out = system.run(display.command(name, size, rate, rotation, primary), 15)
        if rc != 0:
            raise ApiError(out.strip() or "The screen couldn't be changed.")
        saved = dict(self.settings.get("displays") or {})
        if primary:
            saved = {k: {**v, "primary": False} for k, v in saved.items()}
        saved[name] = {"size": size, "rate": rate, "rotation": rotation or "normal", "primary": primary}
        self.update_settings({"displays": saved})
        return self.displays_list()

    def apply_saved_displays(self) -> None:
        """At start: the modes chosen in Settings, for the screens connected now (skipping anything they can't show)."""
        from . import display, system
        saved = self.settings.get("displays") or {}
        if not saved:
            return
        outputs = self.displays_list()["outputs"]
        for name, cfg in saved.items():
            try:
                display.validate(outputs, name, cfg.get("size"), cfg.get("rate"), cfg.get("rotation"))
            except ValueError:
                continue
            system.run(display.command(name, cfg.get("size"), cfg.get("rate"), cfg.get("rotation"), cfg.get("primary")), 15)

    # ---- updates (Settings > Updates; the update service itself is polyos/autoupdate.py) ----------
    def _is_live(self) -> bool:
        return Path("/run/live/medium").exists()

    def _update_files(self) -> tuple[dict, dict]:
        from . import autoupdate
        return autoupdate.load_policy(), autoupdate.load_status()

    def updates_status(self) -> dict:
        """What the update service last did, its settings, and whether PolyOS needs a restart."""
        from . import updates
        policy, st = self._update_files()
        latest = st.get("latest") or ""
        installed = st.get("installed") or ""
        return {
            "current": __version__, "live": self._is_live(), "policy": policy, "state": st.get("state") or "idle",
            "latest": latest, "available": bool(st.get("available")) and updates.newer(latest), "notes": st.get("notes") or "",
            "size": st.get("size") or 0, "downloaded": st.get("downloaded"), "tonight": bool(st.get("tonight")),
            "lastCheck": st.get("lastCheck"), "error": st.get("error"), "reason": st.get("reason"),
            "installed": installed, "restartNeeded": bool(installed) and updates.newer(installed),
        }

    def _start_update_unit(self, kind: str) -> None:
        from . import autoupdate
        try:
            autoupdate.start_unit(kind)
        except Exception as exc:  # noqa: BLE001 - no systemd or polkit here
            raise ApiError(f"The update service couldn't start ({exc}).") from None

    def updates_action(self, kind: str) -> dict:
        """check, tonight (install at the preferred time) or now: no password (data/polkit/50-polyos-update.rules)."""
        if self._is_live():
            raise ApiError("Install PolyOS first; updates install on the installed system.")
        self._start_update_unit(kind)
        self.bus.publish("updates")
        return self.updates_status()

    def updates_policy(self, policy: dict) -> dict:
        """Automatic checks, downloads and installs, the time, restarts and the channel (the administrator password)."""
        from . import autoupdate
        from .privileged import NeedPassword
        try:
            clean = autoupdate.validate_policy({**self._update_files()[0], **policy})
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        if not self.jobs.admin.ready():
            raise NeedPassword()
        self.jobs.admin.call(["update-policy", json.dumps(clean)])
        self.bus.publish("updates")
        return self.updates_status()

    def updates_install(self, what: str) -> dict:
        """Debian's updates for everything else (Settings > Updates > Update everything)."""
        if self._is_live():
            raise ApiError("Install PolyOS first; updates install on the installed system.")
        return self.jobs.start("update", "Installing Debian updates", ["update", "system"], target="system",
                               on_done=lambda _job: self.bus.publish("updates"))

    def update_notice(self, seen: dict) -> dict | None:
        """The calm notification: once per new version ("ready"), once when it's installed ("restart"), and
        once PolyOS is running the new version ("updated")."""
        st = self.updates_status()
        if st["live"]:
            return None
        # this PolyOS is the version the update service installed (automatically or not): say so, once
        if st["installed"] == __version__ and seen.get("updated") != __version__:
            seen["updated"] = __version__
            return {"kind": "updated", "version": __version__, "notes": st["notes"] if st["latest"] == __version__ else ""}
        if st["restartNeeded"] and seen.get("restart") != st["installed"]:
            seen["restart"] = st["installed"]
            return {"kind": "restart", "version": st["installed"], "ask": st["policy"]["askRestart"]}
        if st["available"] and seen.get("ready") != st["latest"] and st["state"] == "idle":
            seen["ready"] = st["latest"]
            return {"kind": "ready", "version": st["latest"], "notes": st["notes"], "tonight": st["tonight"],
                    "autoInstall": st["policy"]["autoInstall"], "time": st["policy"]["time"]}
        return None

    def first_start_notice(self, seen: dict) -> dict | None:
        """After installing: what's finishing in the background (drivers, edition apps), then when it's done."""
        from . import firststart
        plan = firststart.load(firststart.PLAN_PATH)
        st = firststart.load(firststart.STATUS_PATH)
        names = {"gaming": "Gaming apps", "developer": "Developer tools"}
        if firststart.pending(plan) and not seen.get("firstStart"):
            seen["firstStart"] = True
            parts = [x for x in (names.get(plan.get("pack")), "recommended drivers" if plan.get("drivers") else None,
                                 "Vara Voice" if plan.get("vara") else None) if x]
            return {"kind": "setting-up", "title": "Finishing setting up PolyOS",
                    "body": f"Your {' and '.join(parts)} are installing in the background (once you’re online). "
                            "You can use PolyOS in the meantime."}
        if st.get("state") in ("done", "failed") and seen.get("firstStartDone") != st.get("updated"):
            seen["firstStartDone"] = st.get("updated")
            if st["state"] == "failed":
                return {"kind": "setting-up", "title": "Some things didn’t install",
                        "body": "Install them from Settings › Apps and Settings › Drivers when you’re online."}
            done = [x for x in (names.get(st.get("packDone")), "drivers" if st.get("drivers") == "done" else None,
                                "Vara Voice (say “Hey Vera”)" if st.get("varaDone") else None) if x]
            return {"kind": "setting-up", "title": "PolyOS is all set up",
                    "body": f"Installed: {', '.join(done) or 'your apps'}."
                            + (" Restart when it suits you to start using the new drivers." if st.get("drivers") == "done" else "")}
        return None

    def first_start_pending(self) -> bool:
        from . import firststart
        return firststart.PLAN_PATH.exists()

    # ---- Poly Account (optional; polyos/polyaccount.py) -----------------------------------------
    def _pa_home(self) -> Path:
        return self.files.home

    def _pa_hardware(self) -> dict:
        from . import hwcheck
        try:
            return self._hardware_facts(hwcheck.gather(self.graphics_info(), ""))
        except Exception:  # noqa: BLE001 - only nice-to-have details
            return {}

    def _pa_device_name(self) -> str:
        hw = self._pa_hardware().get("computer") or {}
        return " ".join(x for x in (hw.get("maker"), hw.get("model")) if x)[:60] or socket.gethostname()[:60]

    def poly_account_status(self) -> dict:
        from . import polyaccount, updates
        state = polyaccount.load(self._pa_home())
        link = getattr(self, "_pa_link", None)
        return {
            "connected": bool(state.get("credential")), "live": self._is_live(), "server": updates.server(),
            "account": state.get("account"), "device": state.get("device"), "sync": bool(state.get("sync")),
            "remoteManagement": bool(state.get("remoteManagement")), "lastCheckin": state.get("lastCheckin"),
            "error": state.get("lastError"), "telemetry": state.get("telemetry", "minimal"),
            "link": {k: link[k] for k in ("userCode", "verificationUrl", "expiresAt", "status")} if link else None,
        }

    def _pa_connected(self, recovery_key: str = "") -> dict:
        self._pa_link = None
        threading.Thread(target=self.poly_account_checkin, daemon=True).start()
        self.bus.publish("polyaccount")
        return {**self.poly_account_status(), "recoveryKey": recovery_key}

    def poly_account_link_start(self) -> dict:
        """A 6-digit code to enter on the website; PolyOS waits for it in the background."""
        from . import polyaccount
        try:
            r = polyaccount.start_link(self._pa_device_name(), polyaccount.device_info(self._pa_hardware()))
        except polyaccount.AccountError as exc:
            raise ApiError(str(exc)) from None
        link = {"userCode": r["userCode"], "verificationUrl": r["verificationUrl"], "status": "waiting",
                "expiresAt": time.time() + r.get("expiresIn", 600), "deviceCode": r["deviceCode"], "interval": r.get("interval", 5)}
        self._pa_link = link

        def wait():
            while self._pa_link is link and time.time() < link["expiresAt"]:
                time.sleep(link["interval"])
                try:
                    res = polyaccount.poll_link(link["deviceCode"], self._pa_home())
                except polyaccount.AccountError:
                    continue
                if res.get("status") == "approved":
                    self._pa_connected()
                    return
                if res.get("status") == "expired":
                    break
            if self._pa_link is link:
                link["status"] = "expired"
                self.bus.publish("polyaccount")
        threading.Thread(target=wait, name="poly-link", daemon=True).start()
        return self.poly_account_status()

    def poly_account_link_cancel(self) -> dict:
        self._pa_link = None
        return self.poly_account_status()

    def poly_account_signin(self, email: str, password: str) -> dict:
        from . import polyaccount
        try:
            polyaccount.sign_in(email, password, self._pa_device_name(), polyaccount.device_info(self._pa_hardware()), self._pa_home())
        except polyaccount.AccountError as exc:
            raise ApiError(str(exc), 400) from None
        return self._pa_connected()

    def poly_account_register(self, fields: dict) -> dict:
        from . import polyaccount
        try:
            _state, key = polyaccount.register(fields, self._pa_device_name(), polyaccount.device_info(self._pa_hardware()), self._pa_home())
        except polyaccount.AccountError as exc:
            raise ApiError(str(exc), 400) from None
        return self._pa_connected(key)

    def poly_account_countries(self) -> dict:
        from . import polyaccount
        try:
            return polyaccount.http("GET", "/api/countries")
        except polyaccount.AccountError as exc:
            raise ApiError(str(exc)) from None

    def poly_account_set(self, sync: bool | None, remote: bool | None) -> dict:
        from . import polyaccount
        state = polyaccount.load(self._pa_home())
        if not state.get("credential"):
            raise ApiError("Connect a Poly Account first.")
        if sync is not None:
            state["sync"] = sync
        if remote is not None:
            state["remoteManagement"] = remote
        polyaccount.save(state, self._pa_home())
        threading.Thread(target=self.poly_account_checkin, daemon=True).start()
        self.bus.publish("polyaccount")
        return self.poly_account_status()

    def poly_account_disconnect(self) -> dict:
        from . import polyaccount
        polyaccount.disconnect(self._pa_home())
        self.bus.publish("polyaccount")
        return self.poly_account_status()

    def poly_account_checkin(self) -> dict | None:
        """Report in, run waiting actions from the website, and bring in synced settings."""
        from . import autoupdate, polyaccount
        home = self._pa_home()
        state = polyaccount.load(home)
        if not state.get("credential"):
            return None
        try:
            r = polyaccount.checkin(state, autoupdate.load_policy(), self._pa_hardware(), home)
        except polyaccount.AccountError as exc:
            if exc.status != 401:
                polyaccount.merge(state["credential"], {"lastError": str(exc)}, home)
            self.bus.publish("polyaccount")
            return None
        state.update(lastCheckin=time.time(), lastError=None)
        polyaccount.merge(state["credential"], {"lastCheckin": state["lastCheckin"], "lastError": None}, home)
        for cmd in r.get("commands") or []:
            self._pa_run(state, cmd)
        self._pa_pull(state, r.get("sync", {}).get("revision"))
        self.bus.publish("polyaccount")
        return r

    def start_account_loop(self, first_delay: float = 0, every: float | None = None):
        """Check in every 15 minutes (30 with background activity limited) while connected."""
        def loop():
            time.sleep(first_delay)
            while True:
                try:
                    self.poly_account_checkin()
                except Exception:  # noqa: BLE001 - try again next time
                    pass
                time.sleep(every or (1800 if self.settings.get("backgroundLimit") == "reduced" else 900))
        threading.Thread(target=loop, name="poly-account", daemon=True).start()
        return False

    def _pa_run(self, state: dict, cmd: dict) -> None:
        from . import polyaccount
        kind = cmd.get("kind")
        try:
            if kind == "check-updates":
                self.updates_action("check")
            elif not state.get("remoteManagement"):
                raise ApiError("Remote management is off on this computer.")
            elif kind == "update":
                self.updates_action("now")
            elif kind == "lock":
                self.lock()
            elif kind == "restart":
                polyaccount.report(state, cmd["id"], "done", "Restarting")
                self.power("reboot")
                return
            else:
                raise ApiError(f"Unknown action: {kind}")
            polyaccount.report(state, cmd["id"], "done")
        except Exception as exc:  # noqa: BLE001 - reported back to the website
            polyaccount.report(state, cmd["id"], "failed", str(exc))

    def _pa_pull(self, state: dict, revision: str | None) -> None:
        from . import polyaccount
        keys = polyaccount.synced_keys(state)
        if not keys or not revision or revision == state.get("pulledRevision"):
            return
        try:
            values = polyaccount.pull(state, keys)
        except polyaccount.AccountError:
            return
        current = self.settings.snapshot()
        changes = {k: v for k, v in values.items() if current.get(k) != v and polyaccount.shareable(k, v)}
        if changes:
            self._pa_applying = True
            try:
                self.update_settings(changes)  # _pa_applying: not pushed straight back
            except ApiError:
                pass
            finally:
                self._pa_applying = False
        state["pulledRevision"] = revision
        polyaccount.merge(state["credential"], {"pulledRevision": revision}, self._pa_home())

    def _pa_push(self, patch: dict) -> None:
        """Settings changed here: send the synced ones to the account (in the background)."""
        from . import polyaccount
        if getattr(self, "_pa_applying", False):
            return
        state = polyaccount.load(self._pa_home())
        if not state.get("credential"):
            return
        keys = [k for k in polyaccount.synced_keys(state) if k in patch]
        if not keys:
            return
        settings = self.settings.snapshot()

        def send():
            try:
                polyaccount.push(state, settings, keys)
            except polyaccount.AccountError:
                pass
        threading.Thread(target=send, daemon=True).start()

    # ---- trying PolyOS from the USB -----------------------------------------------------------
    def show_install_app(self) -> None:
        """On the USB: "Install PolyOS 7" first in the dock and on the desktop, to get back to the installer."""
        pinned, icons = self.settings.get("pinned"), self.settings.get("desktopIcons")
        patch = {}
        if INSTALL_APP not in pinned:
            patch["pinned"] = [INSTALL_APP, *pinned][:24]
        if INSTALL_APP not in icons:
            patch["desktopIcons"] = [INSTALL_APP, *icons][:24]
        if patch:
            self.update_settings(patch)

    # ---- the hardware check (first start, and Settings > Power & Performance) ----------------
    def hardware_check(self) -> dict:
        from . import hwcheck, system
        renderer = ""
        if system.have("glxinfo"):
            rc, out = system.run(["glxinfo", "-B"], 8)
            renderer = hwcheck.parse_renderer(out) if rc == 0 else ""
        result = hwcheck.assess(self._hardware_facts(hwcheck.gather(self.graphics_info(), renderer)))
        return result | {"current": self.settings.get("performanceProfile")}

    def _hardware_facts(self, facts: dict) -> dict:
        return facts

    def hardware_profile(self, profile: str) -> dict:
        """Set PolyOS up for this computer: "full", "balanced" or the "light" optimized version."""
        from . import hwcheck
        patch = dict(hwcheck.PROFILE_SETTINGS[profile])
        patch.setdefault("backgroundLimit", self.hardware_check()["background"])
        self.update_settings(patch)
        return self.hardware_check()

    def graphics_info(self) -> list[dict]:
        """Graphics cards and the driver each uses (from lspci)."""
        from . import drivers, system
        if not system.have("lspci"):
            return []
        rc, out = system.run(["lspci", "-vmmknn"], 10)
        if rc != 0:
            return []
        return [{"name": f"{d['vendor']} {d['device']}".strip(), "driver": d.get("driver") or ""}
                for d in drivers.parse_lspci(out) if str(d.get("classId", "")).startswith("03")]

    # ---- Settings > Apps: installed apps and startup apps ----------------------------------------
    def _startup_dirs(self) -> list[Path]:
        return startup.system_dirs()

    def apps_manage(self) -> dict:
        """Every app with where it came from and whether it can be uninstalled, and the startup apps."""
        home = self.files.home
        catalog = store.validate(store.load())
        system_ids = {d for a in catalog.values() if a.get("system") for d in a.get("desktop") or []}
        apps = [a for a in self.apps() if not a.get("hidden")]
        origins = {a["id"]: self._app_origin(a["id"]) for a in apps}
        owners = self._debian_owners([o["path"] for o in origins.values() if o["kind"] == "debian"])
        items = []
        for app in apps:
            origin = origins[app["id"]]
            package = owners.get(origin.get("path", ""))
            part_of_polyos = app["id"].startswith("polyos-") or app["id"] in system_ids or (package or "").startswith("polyos")
            removable = not part_of_polyos and (origin["kind"] in ("flatpak", "local") or (origin["kind"] == "debian" and bool(package)))
            items.append({"id": app["id"], "name": app["name"], "icon": app["icon"], "kind": origin["kind"],
                          "package": package, "removable": removable})
        return {"apps": items, "startup": startup.entries(home, self._startup_dirs())}

    def _app_origin(self, desktop_id: str) -> dict:
        return store.app_origin(desktop_id, self.files.home)

    def _debian_owners(self, paths: list[str]) -> dict[str, str]:
        return store.debian_owners(paths)

    def app_uninstall(self, desktop_id: str) -> dict:
        info = next((a for a in self.apps_manage()["apps"] if a["id"] == desktop_id), None)
        if info is None or not info["removable"]:
            raise ApiError("That app is part of PolyOS, or it's already gone.")
        origin = self._app_origin(desktop_id)
        if origin["kind"] == "local":  # a launcher of your own: just remove it
            Path(origin["path"]).unlink(missing_ok=True)
            self._apps_changed()
            return {"removed": desktop_id}
        if origin["kind"] == "flatpak" and origin.get("user"):
            proc = subprocess.run(["flatpak", "uninstall", "--user", "-y", "--noninteractive", origin["ref"]],
                                  capture_output=True, text=True, timeout=600)
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout).strip().splitlines()
                raise ApiError(detail[-1] if detail else "Flatpak couldn't remove it.")
            self._apps_changed()
            return {"removed": desktop_id}

        def done(_job):
            self._apps_changed()
            self.bus.publish("store")
        return self.jobs.start("app", f"Removing {info['name']}", ["app", "remove", desktop_id], target=desktop_id, on_done=done)

    def startup_set(self, entry_id: str, enabled: bool) -> dict:
        try:
            startup.set_enabled(self.files.home, entry_id, enabled, self._startup_dirs())
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        return {"startup": startup.entries(self.files.home, self._startup_dirs())}

    def startup_add(self, desktop_id: str) -> dict:
        path = store.launcher_path(desktop_id, self.files.home)
        if path is None:
            raise ApiError("That app can't start automatically.")
        try:
            startup.add(self.files.home, path)
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        return {"startup": startup.entries(self.files.home, self._startup_dirs())}

    def startup_remove(self, entry_id: str) -> dict:
        try:
            startup.remove(self.files.home, entry_id, self._startup_dirs())
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        return {"startup": startup.entries(self.files.home, self._startup_dirs())}

    # ---- cloud gaming ------------------------------------------------------------------------
    def cloud_gaming(self) -> dict:
        return {"services": [{"id": cid, "name": v[0], "url": v[1], "summary": v[2]} for cid, v in gaming.CLOUD.items()],
                "installed": gaming.enabled(self.settings, self.files.home)}

    def cloud_gaming_set(self, ids: list[str]) -> dict:
        if any(i not in gaming.CLOUD for i in ids):
            raise ApiError("Unknown cloud gaming service.")
        gaming.set_enabled(self.settings, self.files.home, ids)
        self._apps_changed()
        return self.cloud_gaming()

    def _apps_changed(self) -> None:
        """New .desktop files (the real shell notices them itself)."""

    # ---- security checkup --------------------------------------------------------------------
    def security_status(self) -> dict:
        from . import recovery

        status = security.status()
        try:
            # the records folder is root-only on most systems: then it can't be checked from here
            status["recoveryKey"] = recovery.has_key(self.user()["name"]) if os.access(recovery.STORE, os.X_OK) else None
        except Exception:  # noqa: BLE001 - unreadable record: just don't claim one
            status["recoveryKey"] = None
        status["lockOnSleep"] = self.settings.get("lockOnSleep")
        return status

    def security_set(self, what: str, on: bool) -> dict:
        if what not in ("firewall", "updates"):
            raise ApiError("Unknown security setting.")
        title = {"firewall": "Firewall", "updates": "Automatic updates"}[what]
        return self.jobs.start("security", f"{title} {'on' if on else 'off'}", ["security", what, "on" if on else "off"],
                               target=what, on_done=lambda _job: self.bus.publish("security"))

    # ---- developer mode ----------------------------------------------------------------------
    def ui_override(self, rel: str) -> Path | None:
        """A developer's replacement for a built-in UI file (only while developer mode is on)."""
        if not self.settings.get("developerMode"):
            return None
        return devmode.resolve(self.settings.path.parent, rel)

    def dev_action(self, action: str) -> dict:
        config = self.settings.path.parent
        if action == "folder":
            path = devmode.prepare(config)
        elif action == "source":
            path = devmode.copy_source(paths.UI_DIR, self.files.home)
        elif action == "reset":
            aside = devmode.reset(config)
            return {"path": str(aside) if aside else None}
        else:
            raise ApiError("Unknown developer action.")
        self.open_app("files", str(path))
        return {"path": str(path)}

    def widgets_update(self, patch: dict) -> dict:
        data = self.widgets.update(patch)
        self.bus.publish("widgets", keys=sorted(patch))
        return data

    def _vara_attention(self) -> None:
        """Vara is waiting for an Allow or Deny: bring its chat back if no popup is open."""
        with self._popup_lock:
            busy_elsewhere = self._popup is not None
        if not busy_elsewhere:
            try:
                self.popup_request("vara")
            except Exception:  # noqa: BLE001 - the chat still shows the request when opened
                log.debug("couldn't open Vara for an approval", exc_info=True)

    # ---- Vara Voice (vara_voice.py): a process of its own, following the settings ----------------
    def notify(self, title: str, body: str) -> None:
        """A plain desktop notification (reminders)."""
        from . import system
        if system.have("notify-send"):
            system.spawn(["notify-send", "-a", "PolyOS", "-i", "polyos", title, body])

    def assistant_names(self) -> dict:
        """What the assistant is called on screen ("Vara", or "Jarvis") and the word it answers to."""
        from . import vara_voice
        own = (self.settings.get("assistantName") or "").strip()
        return {"display": own or "Vara", "wake": vara_voice.clean_name(own) if own else vara_voice.DEFAULT_NAME}

    def vara_voice_status(self) -> dict:
        from . import vara_voice
        proc = getattr(self, "_voice_proc", None)
        names = self.assistant_names()
        return {"installed": self._voice_installed(), "enabled": bool(self.settings.get("varaVoice")),
                "wake": bool(self.settings.get("varaVoiceWake")), "speak": bool(self.settings.get("varaVoiceSpeak")),
                "followUp": bool(self.settings.get("varaVoiceFollowUp")), "openMic": bool(self.settings.get("varaVoiceOpenMic")),
                "hud": bool(self.settings.get("varaHud")), "name": names["display"],
                "running": bool(proc is not None and proc.poll() is None), "wakeWords": f"Hey {names['wake'].title()}",
                **getattr(self, "_voice_state", {"state": "off", "text": ""}), "home": str(vara_voice.VOICE_HOME)}

    def _voice_installed(self) -> bool:
        from . import vara_voice
        return vara_voice.installed()

    def vara_voice_state(self, state: str, text: str = "") -> dict:
        """From the voice process: listening, thinking, speaking or idle (the Vara chat shows it)."""
        if state not in ("idle", "listening", "thinking", "speaking"):
            raise ApiError("Unknown voice state.")
        before = getattr(self, "_voice_state", {}).get("state")
        self._voice_state = {"state": state, "text": text[:300]}
        self.bus.publish("varaVoice", **self._voice_state)
        # "Hey Vera": the HUD (or the Vara panel) shows it listening, unless a full-screen app is in front
        if state == "listening" and before != "listening" and not getattr(self, "_fullscreen_app", False):
            if self.settings.get("varaHud"):
                if not getattr(self, "_hud_open", False):
                    self.hud(True)
            elif (self._popup or {}).get("view") != "vara":
                try:
                    self.popup_request("vara")
                except ApiError:
                    pass
        return self._voice_state

    def vara_voice_listen(self) -> dict:
        """Push to talk (Win+Shift+V, or the microphone button in the Vara chat)."""
        if not self.vara_voice_status()["running"]:
            raise ApiError("Turn on Vara Voice in Settings › Vara first.", 409)
        self.vara.announce("", "listen")
        return {"ok": True}

    def vara_voice_install(self) -> dict:
        def done(job):
            if job["state"] == "done":
                self.update_settings({"varaVoice": True})
            self.bus.publish("varaVoice", **getattr(self, "_voice_state", {"state": "off", "text": ""}))
        return self.jobs.start("vara-voice", "Installing Vara Voice", ["vara-voice", "install"], target="vara-voice", on_done=done)

    def sync_vara_voice(self) -> None:
        """Start, stop or restart the voice process to match the settings (and bring it back if it quit)."""
        from . import vara_voice
        want = bool(self.settings.get("varaVoice")) and self._voice_installed() and not self._is_live()
        args = [str(vara_voice.PYTHON), "-m", "polyos.vara_voice", "--name", self.assistant_names()["wake"],
                *([] if self.settings.get("varaVoiceSpeak") else ["--quiet"]),
                *([] if self.settings.get("varaVoiceWake") else ["--no-wake"]),
                *([] if self.settings.get("varaVoiceFollowUp") else ["--no-follow-up"]),
                *(["--open-mic"] if self.settings.get("varaVoiceOpenMic") else [])]
        proc = getattr(self, "_voice_proc", None)
        running = proc is not None and proc.poll() is None
        if running and (not want or getattr(self, "_voice_args", None) != args):
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
            running = False
            self._voice_state = {"state": "off", "text": ""}
            self.bus.publish("varaVoice", **self._voice_state)
        if want and not running:
            self._stop_stray_voice()
            log_path = paths.state_dir() / "vara-voice.log"
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
            with open(log_path, "ab") as log_file:
                self._voice_proc = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL, stdout=log_file,
                                                    stderr=subprocess.STDOUT, start_new_session=True)
            self._voice_args = args
            (paths.runtime_dir() / "vara-voice.pid").write_text(str(self._voice_proc.pid))

    def stop_vara_voice(self) -> None:
        """When the shell exits (a restart starts a new one)."""
        proc = getattr(self, "_voice_proc", None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
        (paths.runtime_dir() / "vara-voice.pid").unlink(missing_ok=True)

    def _stop_stray_voice(self) -> None:
        """One left by a shell that crashed: only ever one Vara listening."""
        pid_file = paths.runtime_dir() / "vara-voice.pid"
        try:
            pid = int(pid_file.read_text().strip())
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except (OSError, ValueError):
            return
        if b"polyos.vara_voice" in cmdline:
            try:
                os.kill(pid, 15)
            except OSError:
                pass

    def vara_tool_open(self, name: str) -> dict:
        """A tool Vara made: its folder in VS Code (or Files)."""
        from . import system, vara_toolmaker
        folder = vara_toolmaker.tools_dir(self.vara.home) / name
        if not vara_toolmaker.NAME.match(name) or not folder.is_dir():
            raise ApiError("That tool doesn't exist.", 404)
        if system.have("code"):
            system.spawn(["code", str(folder)])
        else:
            self.open_path(str(folder))
        return {"ok": True}

    def vara_index_rebuild(self) -> dict:
        self.vara.index.clear()
        threading.Thread(target=lambda: self.vara.index.update(self.vara.index.roots([self.vara.workspace()]), budget=600,
                                                                max_files=5000), daemon=True).start()
        return self.vara.index.stats()

    # ---- the HUD: the assistant's full-screen interface -------------------------------------------
    def hud(self, show: bool) -> dict:
        """Open or close the HUD (it opens by itself when the assistant hears its name, if Settings allows)."""
        self._hud_open = bool(show)
        self._show_hud(self._hud_open)
        self.bus.publish("hud", open=self._hud_open)
        return {"open": self._hud_open}

    def _show_hud(self, show: bool) -> None:
        pass  # the desktop shell puts a full-screen window up (shell.py)

    def _cpu_percent(self) -> float | None:
        """Busy share of the processor since the last call (from /proc/stat)."""
        try:
            fields = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
        except (OSError, ValueError):
            return None
        idle, total = fields[3] + (fields[4] if len(fields) > 4 else 0), sum(fields)
        before = getattr(self, "_cpu_sample", None)
        self._cpu_sample = (idle, total)
        if not before or total == before[1]:
            return None
        return round(100 * (1 - (idle - before[0]) / (total - before[1])), 1)

    def hud_data(self) -> dict:
        """Everything the HUD shows, in one call (it polls every couple of seconds while open)."""
        from .vara_tools import TOOLS, custom_tools
        mem = None
        try:
            info = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    info[key] = int(rest.split()[0])
            mem = round(100 * (1 - info["MemAvailable"] / info["MemTotal"]), 1)
        except (OSError, KeyError, ValueError, IndexError, ZeroDivisionError):
            pass
        try:
            uptime = int(float(Path("/proc/uptime").read_text().split()[0]))
        except (OSError, ValueError, IndexError):
            uptime = None
        try:
            status = self.system_status()
        except Exception:  # noqa: BLE001 - the HUD shows what it can
            status = {}
        state = self.vara.state()
        history = state["history"]
        last_user = next((h["content"] for h in reversed(history) if h["role"] == "user"), "")
        last_reply = next((h["content"] for h in reversed(history) if h["role"] == "assistant" and not h.get("interim")), "")
        notes = self.vara.memory.notes()
        return {
            "name": self.assistant_names()["display"], "wake": self.assistant_names()["wake"],
            "cpu": self._cpu_percent(), "memory": mem, "uptime": uptime, "cores": os.cpu_count(),
            "battery": status.get("battery"), "network": status.get("network"), "volume": status.get("volume"),
            "voice": self.vara_voice_status(), "busy": state["busy"], "pending": state["pending"],
            "steps": [{"title": h["title"], "status": h["status"], "icon": h.get("icon")}
                      for h in history if h["role"] == "step"][-6:],
            "you": last_user[:300], "reply": last_reply[:600],
            "plan": next((h["steps"] for h in reversed(history) if h["role"] == "plan"), []),
            "scheduled": self.vara.schedule.items()[:6],
            "knowledge": {"notes": len(notes), "preferences": sum(1 for n in notes if n.get("kind") == "preference"),
                          "documents": self.vara.index.stats()["files"],
                          "tools": len(TOOLS) + len(custom_tools(self.vara.home)), "skills": len(self.vara.skills.list())},
        }

    def vara_test(self) -> dict:
        reply = complete(self.vara.config.load(), [{"role": "user", "content": "Reply with just the word: ready"}], timeout=60)
        return {"ok": True, "reply": reply[:200]}

    def update_settings(self, patch: dict) -> dict:
        if isinstance(patch, dict) and patch.get("keepRecent") is False:
            patch = {**patch, "recent": []}  # turning activity history off forgets it too
            self.vara.habits.clear()
        if isinstance(patch, dict) and patch.get("varaIndex") is False:
            self.vara.index.clear()  # turning document search off forgets the index too
        settings = self.settings.update(patch)
        self.bus.publish("settings", settings=settings)
        self._pa_push(patch)  # Poly Sync, when connected
        if {"varaVoice", "varaVoiceWake", "varaVoiceSpeak", "varaVoiceFollowUp", "varaVoiceOpenMic", "assistantName"} & set(patch):
            self.sync_vara_voice()
        return settings

    def wallpapers(self) -> list[dict]:
        out = []
        if paths.WALLPAPER_DIR.is_dir():
            rank = {stem: i for i, stem in enumerate(WALLPAPER_ORDER)}
            for path in sorted(paths.WALLPAPER_DIR.iterdir(), key=lambda p: (rank.get(p.stem, len(rank)), p.name)):
                if path.suffix.lower() in IMAGE_TYPES:
                    out.append({
                        "id": f"builtin:{path.name}",
                        "name": WALLPAPER_NAMES.get(path.stem) or path.stem.replace("-", " ").replace("_", " ").title(),
                        "url": f"/wallpaper/builtin/{path.name}",
                    })
        return out

    def builtin_wallpaper(self, name: str) -> Path | None:
        path = paths.WALLPAPER_DIR / name
        if "/" in name or "\\" in name or name.startswith(".") or not path.is_file():
            return None
        return path

    def wallpaper_path(self, key: str = "wallpaper") -> Path | None:
        value = self.settings.get(key)
        if value.startswith("builtin:"):
            path = self.builtin_wallpaper(value.split(":", 1)[1])
        else:
            path = Path(value) if Path(value).is_file() else None
        if path is None:  # missing file or a removed built-in: use the default, else any built-in
            default = DEFAULTS[key].split(":", 1)[1]
            path = self.builtin_wallpaper(default)
            if path is None and (builtins := self.wallpapers()):
                path = self.builtin_wallpaper(builtins[0]["id"].split(":", 1)[1])
        return path

    def popup_request(self, view, anchor_x=None, data=None, height=None, toggle_any=False):
        """Open, switch or toggle the shell popup (start menu, quick settings, ...).

        toggle_any (the Windows key): close whatever popup is open instead of switching to `view`.
        """
        if data is not None and not isinstance(data, dict):
            raise ApiError("popup data must be an object")
        with self._popup_lock:
            if view is None or (toggle_any and self._popup is not None):
                return self._close_popup_locked()
            if view not in POPUP_SIZES:
                raise ApiError(f"unknown popup: {view}")
            key = view + json.dumps(data or {}, sort_keys=True)
            if len(key) > 4096:
                raise ApiError("popup data too large")
            if self._popup is not None and self._popup_key == key:
                return self._close_popup_locked()
            now = time.monotonic()
            if self._last_closed and self._last_closed[0] == key and now - self._last_closed[1] < REOPEN_GUARD:
                return None
            width, default_height = POPUP_SIZES[view]
            popup = {
                "view": view,
                "data": data or {},
                "width": width,
                "height": max(80, min(int(height), 900)) if height else default_height,
                "anchorX": anchor_x,
                "fullscreen": view in FULLSCREEN_POPUPS,
                "tall": view in TALL_POPUPS,
            }
            self._popup, self._popup_key = popup, key
        self.bus.publish("popup", popup=popup)
        self._show_popup(popup)
        return popup

    def popup_closed(self):
        with self._popup_lock:
            return self._close_popup_locked()

    def _close_popup_locked(self):
        if self._popup is None:
            return None
        self._last_closed = (self._popup_key, time.monotonic())
        self._popup = self._popup_key = None
        self.bus.publish("popup", popup=None)
        self._hide_popup()
        return None
