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
from .core import BROWSER_APPS, DEFAULTS, IMAGE_TYPES, ApiError, EventBus, Settings, bundled_icon, browser_pins, letter_icon, log
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
    "start": (640, 700),  # the Start menu (shorter on small screens)
    "launcher": (0, 0),
    "power": (0, 0),
    "run": (460, 188),
    "vara": (480, 680),
    "quick": (360, 356),  # the Wi-Fi list asks for more height via the "height" field
    "calendar": (320, 390),
    "taskmenu": (240, 200),
    "quickmenu": (264, 468),  # Super+X: the Windows-style quick link menu
    "project": (340, 330),  # Win+P: duplicate, extend or one screen
    "tray": (320, 280),  # the taskbar's ^: running apps (the panel asks for the height it needs)
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
CAMERA_APP = "polyos-camera.desktop"  # the Camera app (it says so when no camera is connected)
CAMERA_TYPES = {"photo": {"image/jpeg": ".jpg", "image/png": ".png"},
                "video": {"video/webm": ".webm", "video/mp4": ".mp4"}}
CAMERA_MAX_BYTES = {"photo": 30 * 1024 * 1024, "video": 1024 * 1024 * 1024}
# Start menu entries most people never need (Settings > Apps shows them again).
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
    "xfce4-screenshooter.desktop", "org.xfce.screenshooter.desktop", "cmatrix.desktop", "blueman-adapters.desktop",
    "org.gnome.Evince-previewer.desktop", "calamares-install-debian.desktop",
    "yad-icon-browser.desktop", "gnome-disk-image-mounter.desktop", "gnome-disk-image-writer.desktop",
    "org.gnome.Tecla.desktop", "python3.12.desktop", "python3.14.desktop", "ipython3.desktop", "org.xfce.mousepad-settings.desktop",
}
# Of the apps the PolyOS image brings along, the Start menu lists these (and PolyOS's own); the
# image records every app it ships in BASE_APPS_FILE, so the rest (XTerm, Screenshot, helpers and
# settings tools) stay out of the way. Apps installed later always show. Settings > Apps >
# "Show all apps" lists everything.
BASE_APPS_FILE = Path("/usr/share/polyos/base-apps.txt")
BASE_SHOWN = {
    "google-chrome.desktop", "chromium.desktop", "firefox-esr.desktop", "xfce4-terminal.desktop",
    "org.xfce.mousepad.desktop", "org.gnome.Evince.desktop",
    "org.xfce.ristretto.desktop", "org.gnome.Calculator.desktop", "blueman-manager.desktop",
}


def base_apps(path: Path = BASE_APPS_FILE) -> set[str]:
    try:
        return {line.strip() for line in path.read_text("utf-8").splitlines() if line.strip().endswith(".desktop")}
    except OSError:
        return set()


def app_hidden(app_id: str, base: set[str]) -> bool:
    """Left out of the Start menu (unless "Show all apps" is on)."""
    if app_id in HIDDEN_APPS:
        return True
    return app_id in base and app_id not in BASE_SHOWN and not app_id.startswith("polyos-")


def supersede_browsers(apps: list[dict], base: set[str], browser: str = "chrome") -> None:
    """The browser you chose shows, and the others stay out of sight next to it: Chromium (kept for
    cloud gaming) next to Chrome, and both next to Firefox once it's installed."""
    ids = {a["id"] for a in apps}
    if browser == "firefox" and "firefox-esr.desktop" in ids:
        hide = {"google-chrome.desktop", "chromium.desktop"}
    elif "google-chrome.desktop" in ids:
        hide = {"chromium.desktop"}
    else:
        return
    for a in apps:
        if a["id"] in hide and (a["id"] in base or a["id"] == "google-chrome.desktop"):
            a["hidden"] = a["superseded"] = True


# Friendlier names for Debian's default apps.
DISPLAY_NAMES = {
    "firefox-esr.desktop": "Firefox", "chromium.desktop": "Chromium", "org.xfce.mousepad.desktop": "Text Editor", "xfce4-terminal.desktop": "Terminal",
    "org.xfce.ristretto.desktop": "Photos", "xfce4-screenshooter.desktop": "Screenshot",
    "blueman-manager.desktop": "Bluetooth", "org.gnome.Evince.desktop": "Document Viewer",
    "org.gnome.Calculator.desktop": "Calculator", "org.gnome.FileRoller.desktop": "Archive Manager",
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

    # ---- dual boot: Start › Power › Restart to Windows ---------------------------------------------
    def windows_installed(self) -> bool:
        """Windows is in the boot menu (/boot/grub/grub.cfg is readable by everyone)."""
        from .admin import windows_entry
        try:
            return windows_entry(Path("/boot/grub/grub.cfg").read_text("utf-8", errors="replace")) is not None
        except OSError:
            return False

    def boot_menu(self) -> dict:
        """With Windows too: is the PolyOS boot menu shown at every start (Settings › Power)?"""
        from .installer import boot_menu_shown
        try:
            shown = boot_menu_shown(Path("/etc/default/grub").read_text("utf-8", errors="replace"))
        except OSError:
            shown = False
        return {"windows": self.windows_installed(), "shown": shown}

    def boot_menu_set(self, show: bool) -> dict:
        if not self.windows_installed():
            raise ApiError("Windows isn't on this computer's boot menu.", 404)
        return self.jobs.start("boot-menu", "Changing the boot menu", ["boot-menu", "show" if show else "hide"], target="boot-menu")

    def restart_to_windows(self) -> dict:
        if not self.windows_installed():
            raise ApiError("Windows isn't on this computer's boot menu.", 404)
        proc = subprocess.run(["systemctl", "start", "--no-block", "polyos-restart-windows.service"], capture_output=True,
                              text=True, timeout=20)
        if proc.returncode != 0:
            raise ApiError("Couldn't restart into Windows.", 500)
        return {"ok": True}

    def notify(self, title: str, body: str) -> None:
        """A plain desktop notification."""
        from . import system
        if system.have("notify-send"):
            system.spawn(["notify-send", "-a", "PolyOS", "-i", "polyos", title, body])

    # ---- Settings > Storage -------------------------------------------------------------------
    def storage(self) -> dict:
        from . import storage
        try:
            apt_cache = sum(f.stat().st_size for f in Path("/var/cache/apt/archives").glob("*.deb"))
        except OSError:
            apt_cache = 0
        return {"drives": storage.drives(), "folders": storage.breakdown(self.files.home), "packageCache": apt_cache}

    def storage_clean(self, what: str) -> dict:
        from . import storage
        if what == "thumbnails":
            return {"freed": storage.clear_thumbnails(self.files.home)}
        if what == "trash":
            self.files_empty_trash()
            return {"freed": None}
        if what == "packages":
            return self.jobs.start("clean", "Deleting downloaded packages", ["clean-packages"], target="packages")
        raise ApiError("Unknown clean-up.")

    # ---- Settings > Power & Performance: the lid and the power button ---------------------------
    def power_keys(self) -> dict:
        from . import power
        return {**power.power_keys(), "hasLid": power.has_lid(), "actions": power.KEY_ACTIONS}

    def power_keys_set(self, lid: str, plugged: str, button: str) -> dict:
        from . import power
        power.logind_text(lid, plugged, button)  # checks the choices before asking for the password
        return self.jobs.start("power-keys", "Saving power button and lid settings", ["power-keys", lid, plugged, button],
                               target="power-keys")

    # ---- Settings > Apps > Usage: time with each app in front, per day (kept two weeks, on this computer)
    USAGE_DAYS = 14

    def _usage_path(self) -> Path:
        return self.settings.path.parent / "usage.json"

    def usage_tick(self, app_id: str | None, seconds: float) -> None:
        if not app_id or seconds <= 0 or not self.settings.get("keepRecent"):
            return
        import datetime
        path = self._usage_path()
        try:
            data = json.loads(path.read_text("utf-8"))
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        day = datetime.date.today().isoformat()
        today = data.setdefault(day, {})
        today[app_id] = round(today.get(app_id, 0) + seconds)
        for old in sorted(data)[:-self.USAGE_DAYS]:
            data.pop(old, None)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), "utf-8")
        os.replace(tmp, path)

    def usage(self) -> dict:
        import datetime
        try:
            data = json.loads(self._usage_path().read_text("utf-8"))
        except (OSError, ValueError):
            data = {}
        today = datetime.date.today()
        week = {(today - datetime.timedelta(days=i)).isoformat() for i in range(7)}
        names = {a["id"]: a for a in self.apps()}
        totals: dict[str, dict] = {}
        for day, apps in data.items():
            for app_id, secs in (apps or {}).items():
                t = totals.setdefault(app_id, {"today": 0, "week": 0})
                if day == today.isoformat():
                    t["today"] += secs
                if day in week:
                    t["week"] += secs
        rows = [{"id": i, "name": names[i]["name"] if i in names else i.removesuffix(".desktop"),
                 "icon": names[i]["icon"] if i in names else f"/icon/app/{i}", **t} for i, t in totals.items() if t["week"]]
        days = [{"day": (today - datetime.timedelta(days=i)).isoformat(),
                 "seconds": sum((data.get((today - datetime.timedelta(days=i)).isoformat()) or {}).values())} for i in range(6, -1, -1)]
        return {"apps": sorted(rows, key=lambda r: -r["week"]), "days": days, "on": bool(self.settings.get("keepRecent"))}

    def usage_clear(self) -> dict:
        self._usage_path().unlink(missing_ok=True)
        return self.usage()

    # ---- the Start menu and the taskbar's ^ ------------------------------------------------------
    def recent_files(self) -> list[dict]:
        from .files import recent_files
        return recent_files(self.files.home)

    def _exe_index(self) -> dict[str, str]:
        """Program name -> app id, for apps that keep running without a window (the shell fills it in)."""
        return {}

    def background_apps(self) -> list[dict]:
        """Apps running without a window of their own (Discord or Steam in the background), for the taskbar's ^."""
        from . import procs
        index = self._exe_index()
        if not index:
            return []
        apps = {a["id"]: a for a in self.apps() if not a.get("hidden")}
        with_windows = {w.get("appId") for w in self.windows()}
        found: dict[str, dict] = {}
        for pid, names in procs.user_processes():
            app_id = next((index[n.lower()] for n in names if n.lower() in index), None)
            if app_id and app_id in apps and app_id not in with_windows and app_id not in found \
                    and not app_id.startswith("polyos-"):
                found[app_id] = {"appId": app_id, "name": apps[app_id]["name"], "icon": apps[app_id]["icon"], "pid": pid}
        return sorted(found.values(), key=lambda a: a["name"].casefold())

    def end_background_app(self, app_id: str) -> dict:
        """Quit an app running in the background: its processes get a polite SIGTERM."""
        from . import procs
        index = self._exe_index()
        pids = [pid for pid, names in procs.user_processes() if any(index.get(n.lower()) == app_id for n in names)]
        if not pids:
            raise ApiError("That app isn't running.", 404)
        for pid in pids:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        return {"ok": True}

    def note_launch(self, app_id: str) -> None:
        """Remember an opened app for the Start menu's Recent list."""
        if not self.settings.get("keepRecent"):
            return
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

    # ---- PIN sign-in (polyos/pin.py): set with your password, checked by a root service -----------
    def pin_status(self) -> dict:
        from . import pin
        try:
            return {**pin.ask({"op": "status"}), "available": True}
        except (OSError, ValueError):
            return {"set": False, "blocked": False, "available": False}

    def pin_set(self, password: str, new_pin: str | None) -> dict:
        from . import pin
        if new_pin is not None and not pin.valid(new_pin):
            raise ApiError("A PIN is 4 to 6 digits.")
        if not password:
            raise ApiError("Enter your password first.")
        self.jobs.admin.authenticate(password)  # the PIN is set (or removed) only with the password
        self._account_request({"pin": new_pin})
        return self.pin_status()

    def pin_unlock(self, secret: str) -> bool:
        """The lock screen: a PIN, checked by the PIN service (it counts wrong tries, as root)."""
        from . import pin
        if not pin.valid(secret):
            return False
        try:
            return bool(pin.ask({"op": "verify", "pin": secret}).get("ok"))
        except (OSError, ValueError):
            return False

    def pin_reset(self) -> None:
        """Signed in or unlocked with the password: the PIN works again after too many wrong tries."""
        from . import pin
        try:
            pin.ask({"op": "reset"}, timeout=3)
        except (OSError, ValueError):
            pass

    def account_recovery_key(self):
        from .recovery import generate

        if not self.jobs.admin.ready():
            from .privileged import NeedPassword

            raise NeedPassword()
        key = generate()
        self._account_request({"recoveryKey": key})
        return {"key": key}

    # ---- Driver Manager ------------------------------------------------------------------
    def firmware_status(self) -> dict:
        return drivers.firmware_updates()

    def firmware_install(self) -> dict:
        return self.jobs.start("drivers", "Updating firmware", ["firmware"], target="firmware")

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
                               on_done=lambda job: self.bus.publish("store"), queue=True)

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
        if self.settings.get("nightLight"):
            self.apply_night_light()
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
        if self.settings.get("nightLight"):
            self.apply_night_light()  # a new mode can reset the gamma

    def monitors(self) -> list[dict]:
        """Each screen's place on the desktop (the shell knows; the desktop draws a wallpaper on each)."""
        return []

    def apply_display_layout(self) -> dict:
        """Every connected screen on, arranged the way Settings (or Win+P) says: at start, and whenever a
        screen is plugged in or out. Each screen's own resolution, rate and orientation are kept."""
        from . import display, system
        if not system.have("xrandr"):
            return {"ok": False}
        rc, out = system.run(["xrandr", "--query"], 15)
        if rc != 0:
            return {"ok": False}
        outputs = display.parse_xrandr(out)
        cmd = display.layout_command(outputs, self.settings.get("displayMode"), self.settings.get("displays") or {},
                                     display.still_on(out))
        if cmd:
            rc, err = system.run(cmd, 20)
            if rc != 0:
                log.warning("screen layout failed: %s", err.strip()[-300:])
                # something in the arrangement didn't take: at least every screen on, side by side
                system.run(display.layout_command(outputs, "extend", {}, display.still_on(out)), 20)
        if self.settings.get("nightLight"):
            self.apply_night_light()
        return {"ok": True, "screens": len(outputs)}

    def display_mode_set(self, mode: str) -> dict:
        from . import display
        if mode not in display.MODES:
            raise ApiError("Choose Duplicate, Extend or one screen.")
        self.update_settings({"displayMode": mode})
        return self.apply_display_layout()

    def apply_night_light(self) -> None:
        """Night light on or off on every screen (at start, when it's switched, and after a screen change)."""
        from . import display, system
        if not system.have("xrandr"):
            return
        for cmd in display.night_light_commands(self.displays_list()["outputs"], bool(self.settings.get("nightLight"))):
            system.run(cmd, 10)

    def apply_airplane(self, on: bool) -> None:
        """Airplane mode: NetworkManager's radios (Wi-Fi and mobile broadband) and Bluetooth, off or on."""
        from . import system
        if system.have("nmcli"):
            system.run(["nmcli", "radio", "all", "off" if on else "on"], 10)
        if system.have("bluetoothctl"):
            system.run(["bluetoothctl", "power", "off" if on else "on"], 10)

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
            parts = [x for x in (names.get(plan.get("pack")), "recommended drivers" if plan.get("drivers") else None) if x]
            return {"kind": "setting-up", "title": "Finishing setting up PolyOS",
                    "body": f"Your {' and '.join(parts)} are installing in the background (once you’re online). "
                            "You can use PolyOS in the meantime."}
        if st.get("state") in ("done", "failed") and seen.get("firstStartDone") != st.get("updated"):
            seen["firstStartDone"] = st.get("updated")
            if st["state"] == "failed":
                return {"kind": "setting-up", "title": "Some things didn’t install",
                        "body": "Install them from Settings › Apps and Settings › Drivers when you’re online."}
            done = [x for x in (names.get(st.get("packDone")), "drivers" if st.get("drivers") == "done" else None) if x]
            return {"kind": "setting-up", "title": "PolyOS is all set up",
                    "body": f"Your {' and '.join(done) or 'apps'} are installed."
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
            "backupAt": state.get("backupAt"),
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

    def poly_account_backups(self) -> dict:
        """The account's other computers that have a backup (setup: "Set up like one of your computers")."""
        from . import polyaccount
        state = polyaccount.load(self._pa_home())
        if not state.get("credential"):
            raise ApiError("Connect a Poly Account first.")
        try:
            return {"backups": polyaccount.list_backups(state)}
        except polyaccount.AccountError as exc:
            raise ApiError(str(exc), 502) from None

    def poly_account_backup(self, device_id: str) -> dict:
        from . import polyaccount
        state = polyaccount.load(self._pa_home())
        if not state.get("credential"):
            raise ApiError("Connect a Poly Account first.")
        try:
            return polyaccount.get_backup(state, device_id)
        except polyaccount.AccountError as exc:
            raise ApiError(str(exc), exc.status if exc.status in (401, 404) else 502) from None

    def _pa_backup(self, state: dict) -> None:
        """Keep this computer's backup in the account (with sync on), so a new one can copy it."""
        from . import polyaccount, store
        if self._is_live():  # the USB drive's session isn't a computer to copy
            return
        try:
            catalog = store.catalog_with_status(store.load())
            apps = [a["id"] for a in catalog["apps"] if a.get("installed") and not a.get("system")]
        except Exception:  # noqa: BLE001 - no app list this time; settings still count
            apps = []
        backup = polyaccount.make_backup(self.settings.snapshot(), apps)
        now = time.time()
        if polyaccount.backup_due(state, backup, now):
            try:
                polyaccount.push_backup(state, backup, now, self._pa_home())
            except polyaccount.AccountError:
                pass

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
        self._pa_backup(state)
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

    def vara_test(self) -> dict:
        reply = complete(self.vara.config.load(), [{"role": "user", "content": "Reply with just the word: ready"}], timeout=60)
        return {"ok": True, "reply": reply[:200]}

    def update_settings(self, patch: dict) -> dict:
        if isinstance(patch, dict) and patch.get("keepRecent") is False:
            patch = {**patch, "recent": []}  # turning activity history off forgets it too
            self._usage_path().unlink(missing_ok=True)
        if isinstance(patch, dict) and patch.get("browser") in BROWSER_APPS and patch["browser"] != self.settings.get("browser"):
            patch = {**browser_pins(self.settings.snapshot(), patch["browser"]), **patch}  # the new browser where the old one was
        settings = self.settings.update(patch)
        self.bus.publish("settings", settings=settings)
        self._pa_push(patch)  # Poly Sync, when connected
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

    PICTURE_FOLDERS = (("Pictures", 3), ("Downloads", 1), ("Desktop", 1), ("Documents", 1))

    def my_pictures(self, limit: int = 120) -> list[dict]:
        """Pictures you could use as a wallpaper (Pictures, Downloads, Desktop, Documents), newest first."""
        home = Path(self.files.home)
        found: dict[str, dict] = {}

        def walk(folder: Path, depth: int) -> None:
            try:
                entries = list(os.scandir(folder))
            except OSError:
                return
            for e in entries:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        if depth > 1:
                            walk(Path(e.path), depth - 1)
                    elif Path(e.name).suffix.lower() in IMAGE_TYPES and not e.name.lower().endswith(".svg"):
                        st = e.stat()
                        if 0 < st.st_size < 40_000_000:
                            found[e.path] = {"path": e.path, "name": e.name, "mtime": int(st.st_mtime)}
                except OSError:
                    continue

        for name, depth in self.PICTURE_FOLDERS:
            walk(home / name, depth)
        return sorted(found.values(), key=lambda x: -x["mtime"])[:limit]

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
