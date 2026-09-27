"""The PolyOS desktop shell: desktop, taskbar and popup surfaces on X11.

Each surface is an undecorated GTK window hosting a WebKit view of the web UI. The
window manager (openbox) handles app windows; this process tracks them with libwnck,
lists apps from .desktop files with Gio, and serves the UI through polyos.server.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

os.environ.setdefault("GDK_BACKEND", "x11")  # wnck and window type hints need X11
os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")  # avoids blank views on some GPUs/VMs

import gi  # noqa: E402

gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Wnck", "3.0")
try:
    gi.require_version("WebKit2", "4.1")
except ValueError:
    gi.require_version("WebKit2", "4.0")
from gi.repository import Gdk, GdkPixbuf, GdkX11, Gio, GLib, Gtk, WebKit2, Wnck  # noqa: E402

from . import __version__, display, gaming, paths, power, system, theme  # noqa: E402
from .backend import (CAMERA_APP, DISPLAY_NAMES, INSTALL_APP, DOCK_HEIGHT, DOCK_MARGIN, PANEL_HEIGHT, app_hidden, base_apps,
                      supersede_browsers,  # noqa: E402
                      Backend, dock_geometry, panel_margin)
from .core import IMAGE_TYPES, ApiError, EventBus, Settings, bundled_icon, icon_names, letter_icon  # noqa: E402
from .mainloop import on_main  # noqa: E402
from .procs import ProcessMonitor, protected_pids  # noqa: E402
from .server import Server  # noqa: E402

log = logging.getLogger("polyos.shell")

EXIT_LOGOUT, EXIT_RESTART = 0, 3
SOLID_BG = "#151515"
# PolyOS's own apps: launching their .desktop entries opens them in the shell directly.
OWN_APPS = {"polyos-settings.desktop": "settings", "polyos-files.desktop": "files", "polyos-taskmgr.desktop": "taskmgr",
            "polyos-drivers.desktop": "drivers", "polyos-store.desktop": "store", CAMERA_APP: "camera",
            INSTALL_APP: "setup"}
# name: (surface, window title, WM class, default size)
SINGLE_WINDOWS = {
    "taskmgr": ("taskmgr", "Task Manager", "polyos-taskmgr", (940, 640)),
    "drivers": ("drivers", "Driver Manager", "polyos-drivers", (900, 640)),
    "store": ("store", "PolyMarket", "polyos-store", (1120, 740)),
    "camera": ("camera", "Camera", "polyos-camera", (960, 640)),
}
AUTOHIDE_DELAY_MS = 700  # the taskbar slides away this long after the pointer leaves it
AUTOHIDE_SLIVER = 2  # px of the hidden taskbar left on screen to touch
GENERIC_EXECUTABLES = {"sh", "bash", "env", "flatpak", "snap", "python3", "python", "java", "wine",
                       "exo-open", "xdg-open", "pkexec", "sudo", "gtk-launch", "polyos-ctl"}


class DesktopShell(Backend):
    def __init__(self, settings: Settings, bus: EventBus, token: str, debug: bool = False):
        super().__init__(settings, bus)
        self.token = token
        self.debug = debug
        self.exit_code = EXIT_LOGOUT
        self.base_url = ""
        self.audio = system.Audio()
        self.network = system.Network()
        self.backlight = system.Backlight()
        self.gdk_screen = Gdk.Screen.get_default()
        self.composited = self.gdk_screen.is_composited()
        self._infos: dict[str, Gio.DesktopAppInfo] = {}
        self._apps: list[dict] = []
        self._app_icons: dict[str, str | None] = {}
        self._app_icon_names: dict[str, list[str]] = {}
        self._icon_cache: dict[str, tuple[bytes, str]] = {}
        self._wm_index: dict[str, str] = {}
        self._system: dict = {}
        self._system_lock = threading.Lock()
        self._poll_stop = threading.Event()
        self._win_timer = 0
        self._apps_timer = 0
        self.settings_window: Gtk.Window | None = None
        self.setup_window: Gtk.Window | None = None
        self.files_windows: set[Gtk.Window] = set()
        self.single_windows: dict[str, Gtk.Window] = {}
        self._chooser = None
        self.procmon = ProcessMonitor(protected_pids())
        self._procs_lock = threading.Lock()
        self.lock_window: Gtk.Window | None = None
        self._lock_flag = paths.runtime_dir() / "locked"  # survives a shell restart: lock again at start
        self._unlock_lock = threading.Lock()
        self._dock_hidden = False  # auto-hide: slid off the bottom edge
        self._dock_timer = 0
        self._fullscreen_app = False  # an app is full screen on top: the taskbar steps aside
        self._idle = None
        self._idle_slept = False
        self._camera = power.has_camera()
        self._live = Path("/run/live/medium").exists()  # trying PolyOS from the USB
        self._game_active = False  # Game Mode: a full-screen game is in front
        self._poll_interval = self._base_poll()

    # ==== startup ==========================================================================
    def start(self, base_url: str) -> None:
        self.base_url = base_url
        ctx = WebKit2.WebContext.get_default()
        ctx.set_cache_model(WebKit2.CacheModel.DOCUMENT_VIEWER)
        self._ucm = WebKit2.UserContentManager()
        self._ucm.add_script(WebKit2.UserScript.new(
            f"window.POLYOS = {json.dumps({'token': self.token})};",
            WebKit2.UserContentInjectedFrames.TOP_FRAME,
            WebKit2.UserScriptInjectionTime.START, None, None))

        try:
            # every connected screen on and arranged (Settings > Display, Win+P), with the modes chosen there
            self.apply_display_layout()
            self._connectors = display.connectors()
        except Exception:  # noqa: BLE001 - a screen setting must never stop the desktop starting
            log.exception("couldn't apply the saved display settings")
        self._load_apps()
        self._app_monitor = Gio.AppInfoMonitor.get()
        self._app_monitor.connect("changed", lambda *_: self._schedule_apps_reload())
        self._init_wnck()

        self.desktop = self._surface("desktop", Gdk.WindowTypeHint.DESKTOP)
        self.desktop.set_keep_below(True)
        self.panel = self._surface("panel", Gdk.WindowTypeHint.DOCK)
        self.panel.set_keep_above(True)
        self.popup = self._surface("popup", Gdk.WindowTypeHint.UTILITY)
        self.popup.set_keep_above(True)
        self.popup.connect("focus-out-event", self._on_popup_focus_out)
        self.panel.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.panel.connect("enter-notify-event", self._on_panel_enter)
        self.panel.connect("leave-notify-event", self._on_panel_leave)

        self._layout()
        # A resolution change (a VM going full screen, a new monitor) re-lays out now and again
        # once the monitor list has settled, so the wallpaper always reaches every edge.
        self.gdk_screen.connect("monitors-changed", lambda *_: self._relayout_soon())
        self.gdk_screen.connect("size-changed", lambda *_: self._relayout_soon())
        for win in (self.desktop, self.panel):
            win.show_all()
            win.stick()
        self._schedule_dock_hide()  # auto-hide: slide away shortly after sign-in
        self._refresh_system(("volume", "network", "battery", "brightness"))
        threading.Thread(target=self._poll_loop, name="polyos-poll", daemon=True).start()
        self._watch_logind()
        try:
            self._watch_gnome_background()
        except GLib.Error as exc:
            log.info("no GNOME background setting to follow: %s", exc.message)
        if self._lock_flag.exists():
            self.lock()
        threading.Thread(target=self._apply_power, args=(self.settings.snapshot(), True), daemon=True).start()
        self._idle = power.IdleClock()
        GLib.timeout_add_seconds(15, self._idle_tick)
        log.info("shell started (composited=%s)", self.composited)

    def quit(self, code: int = EXIT_LOGOUT):
        self.exit_code = code
        self._poll_stop.set()
        Gtk.main_quit()
        return False

    # ==== surfaces =========================================================================
    def _view(self, query: str, transparent: bool) -> WebKit2.WebView:
        view = WebKit2.WebView.new_with_user_content_manager(self._ucm)
        prefs = view.get_settings()
        prefs.set_enable_developer_extras(self.debug or self.settings.get("devInspector"))
        prefs.set_enable_write_console_messages_to_stdout(True)
        color = Gdk.RGBA()
        if transparent:
            color.parse("rgba(0,0,0,0)")
        else:
            color.parse(SOLID_BG)
        view.set_background_color(color)
        # right-click > Inspect Element only for debugging or with Settings > Developer > Inspector
        view.connect("context-menu", lambda *_: not (self.debug or self.settings.get("devInspector")))
        view.connect("decide-policy", self._on_decide_policy)
        view.connect("web-process-terminated", self._on_web_crash)
        if query.startswith("surface=camera"):
            prefs.set_enable_media_stream(True)
            prefs.set_enable_mediasource(True)
            self._enable_features(prefs, ("MediaRecorderEnabled",))  # video recording (WebKitGTK 2.42+)
            view.connect("permission-request", self._on_camera_permission)
        view.load_uri(f"{self.base_url}/index.html?{query}")
        return view

    def _surface(self, name: str, hint: Gdk.WindowTypeHint) -> Gtk.Window:
        win = Gtk.Window(title=f"PolyOS {name}")
        win.set_wmclass(f"polyos-{name}", "PolyOS")
        win.set_type_hint(hint)
        win.set_decorated(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        visual = self.gdk_screen.get_rgba_visual()
        transparent = self.composited and visual is not None and name != "desktop"
        if transparent:
            win.set_visual(visual)
            win.set_app_paintable(True)
        win.connect("delete-event", lambda *_: True)  # shell surfaces never close
        win.view = self._view(f"surface={name}", transparent)
        win.add(win.view)
        return win

    def _geometry(self) -> Gdk.Rectangle:
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        return monitor.get_geometry()

    def _layout(self) -> None:
        # The desktop covers the whole X screen (every monitor), so no edge is left bare; it draws a
        # wallpaper on each screen and keeps the shortcuts on the main one (monitors()).
        width, height = self.gdk_screen.get_width(), self.gdk_screen.get_height()
        self.desktop.set_size_request(width, height)
        self.desktop.resize(width, height)
        self.desktop.move(0, 0)
        self._place_panel()
        mons = self._read_monitors()
        if mons != getattr(self, "_last_monitors", None):
            self._last_monitors = mons
            self.bus.publish("monitors", monitors=mons)

    def monitors(self) -> list[dict]:
        return getattr(self, "_last_monitors", [])

    def _read_monitors(self) -> list[dict]:  # main thread
        display_ = Gdk.Display.get_default()
        primary = display_.get_primary_monitor() or display_.get_monitor(0)
        out = []
        for i in range(display_.get_n_monitors()):
            mon = display_.get_monitor(i)
            g = mon.get_geometry()
            out.append({"x": g.x, "y": g.y, "width": g.width, "height": g.height, "primary": mon == primary})
        return out

    def _relayout_soon(self) -> None:
        self._layout()
        GLib.timeout_add(400, lambda: (self._layout(), False)[1])
        GLib.timeout_add(1500, lambda: (self._layout(), False)[1])

    def _dock_rect(self) -> tuple[int, int, int, int]:
        g = self._geometry()
        x, y, w, h = dock_geometry(self.settings.snapshot(), g.width, g.height)
        return g.x + x, g.y + y, w, h

    def _place_panel(self) -> None:
        x, y, w, h = self._dock_rect()
        if self._dock_hidden:
            g = self._geometry()
            y = g.y + g.height - AUTOHIDE_SLIVER  # all but a sliver below the screen edge
        self.panel.set_size_request(w, h)
        self.panel.resize(w, h)
        self.panel.move(x, y)

    # ---- taskbar auto-hide and full-screen apps ------------------------------------------
    def _on_panel_enter(self, *_):
        if self._dock_timer:
            GLib.source_remove(self._dock_timer)
            self._dock_timer = 0
        if self._dock_hidden:
            self._dock_hidden = False
            self._place_panel()
        return False

    def _on_panel_leave(self, _win, event):
        if event.detail != Gdk.NotifyType.INFERIOR:
            self._drop_tooltip(self.panel)
            self._schedule_dock_hide()
        return False

    def _schedule_dock_hide(self) -> None:
        if not self.settings.get("taskbarAutoHide") or self._dock_timer:
            return

        def hide():
            self._dock_timer = 0
            if self._popup is None and not self._pointer_in_panel():
                self._dock_hidden = True
                self._place_panel()
            elif self._popup is not None:
                self._schedule_dock_hide()
            return False

        self._dock_timer = GLib.timeout_add(AUTOHIDE_DELAY_MS, hide)

    def _pointer_in_panel(self) -> bool:
        win = self.panel.get_window()
        if win is None:
            return False
        pointer = Gdk.Display.get_default().get_default_seat().get_pointer()
        _screen, px, py = pointer.get_position()
        ox, oy = win.get_origin()[1:]
        return ox <= px < ox + win.get_width() and oy <= py < oy + win.get_height()

    def _apply_taskbar(self, settings: dict) -> None:
        """Taskbar style / auto-hide changed: move the bar and tell openbox how much room to keep."""
        self._dock_hidden = bool(settings["taskbarAutoHide"]) and not self._pointer_in_panel()
        self._place_panel()
        scale = self.panel.get_scale_factor() or 1
        theme.set_openbox_margin(paths.runtime_dir() / "openbox-rc.xml", panel_margin(settings) * scale)

    def _sync_fullscreen(self) -> None:
        """Full-screen apps (the title bar's full-screen button, Win+F, videos, games, F11 in a
        browser) show only the app: no title bar, no taskbar, nothing popping up over it.
        Win+F (or the app's own F11/Esc) leaves full screen."""
        active = self.wscreen.get_active_window()
        full = bool(active is not None and active.is_fullscreen()
                    and active.get_class_group_name() != "PolyOS")
        if full == self._fullscreen_app:
            return
        self._fullscreen_app = full
        self._set_game_mode(full and self.settings.get("gameMode"))
        if full:
            self.panel.hide()
        else:
            self.panel.show_all()
            self.panel.stick()
            self._place_panel()

    # ---- Game Mode ------------------------------------------------------------------------------
    BACKGROUND_TASKS = ("tumblerd", "tracker-miner-fs-3", "tracker-extract-3", "baloo_file", "gvfsd-metadata",
                        "xfce4-notifyd", "blueman-applet", "nm-applet", "evolution-data-server")

    def _base_poll(self) -> float:
        """Seconds between status checks: longer when background activity is limited."""
        return 5.0 if self.settings.get("backgroundLimit") == "reduced" else 2.0

    def _set_game_mode(self, active: bool) -> None:
        """A full-screen game: performance power mode, no idle lock or sleep, and PolyOS's own
        background work (status polling, thumbnails, indexing) steps back until it closes."""
        if active == self._game_active:
            return
        self._game_active = active
        self._poll_interval = 10.0 if active else self._base_poll()
        self.bus.publish("gamemode", active=active)
        log.info("game mode %s", "on" if active else "off")

        def work():
            chosen = self.settings.get("powerMode")
            if not active:
                power.apply_mode(chosen)  # back to the mode picked in Settings
                return
            if chosen not in ("performance", "maximum"):
                power.apply_mode("performance")
            # background helpers yield the processor to the game (they keep the lower priority
            # until they restart: an unprivileged process can't raise it again, and that's harmless)
            for name in self.BACKGROUND_TASKS:
                rc, out = system.run(["pgrep", "-u", str(os.getuid()), "-x", name], 2)
                if rc == 0 and out.split():
                    system.run(["renice", "-n", "10", "-p", *out.split()], 2)
        threading.Thread(target=work, daemon=True).start()

    def _x_time(self) -> int:
        try:
            return GdkX11.x11_get_server_time(self.panel.get_window())
        except Exception:
            return Gdk.CURRENT_TIME

    def _on_decide_policy(self, view, decision, dtype):
        if dtype not in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                         WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            return False
        uri = decision.get_navigation_action().get_request().get_uri()
        if uri.startswith(self.base_url + "/") and dtype == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            return False
        decision.ignore()
        if uri.startswith(("http://", "https://", "mailto:")) and not uri.startswith(self.base_url):
            try:
                Gio.AppInfo.launch_default_for_uri(uri, None)
            except GLib.Error as exc:
                log.warning("could not open %s: %s", uri, exc.message)
        return True

    @staticmethod
    def _enable_features(prefs, names) -> None:
        if not hasattr(WebKit2.Settings, "get_all_features"):
            return
        try:
            features = WebKit2.Settings.get_all_features()
            for i in range(features.get_length()):
                feature = features.get(i)
                if feature.get_identifier() in names:
                    prefs.set_feature_enabled(feature, True)
        except (AttributeError, GLib.Error) as exc:
            log.debug("WebKit features unavailable: %s", exc)

    def _on_camera_permission(self, _view, request):
        """Only the Camera app may use the webcam, and only while Privacy & security allows it."""
        device_info = getattr(WebKit2, "DeviceInfoPermissionRequest", None)
        if device_info is not None and isinstance(request, device_info):  # camera names, to switch cameras
            (request.allow if self.settings.get("cameraAccess") else request.deny)()
            return True
        if isinstance(request, WebKit2.UserMediaPermissionRequest):
            audio = WebKit2.user_media_permission_is_for_audio_device(request)
            video = WebKit2.user_media_permission_is_for_video_device(request)
            allowed = (not video or self.settings.get("cameraAccess")) and (not audio or self.settings.get("micAccess"))
            (request.allow if allowed else request.deny)()
            return True
        request.deny()
        return True

    def _on_web_crash(self, view, reason):
        log.error("web process for %s terminated (%s); reloading", view.get_uri(), reason)
        GLib.timeout_add(500, lambda: (view.reload(), False)[1])

    # ==== popup ============================================================================
    def _show_popup(self, popup: dict) -> None:
        GLib.idle_add(self._show_popup_main, popup)

    def _drop_tooltip(self, win: Gtk.Window) -> None:
        """Close a surface's tooltip. Moving from the taskbar straight into a popup can leave it
        on screen as an empty box (its text cleared, the window never hidden)."""
        view = getattr(win, "view", None)
        if view is not None:
            view.trigger_tooltip_query()  # asks again where the pointer is now: nothing there, no tooltip

    def _show_popup_main(self, popup: dict):
        self._drop_tooltip(self.panel)
        g = self._geometry()
        if self._dock_hidden and not popup.get("fullscreen"):
            self._dock_hidden = False  # e.g. the Windows key: the menu opens from a visible taskbar
            self._place_panel()
        if popup.get("fullscreen"):  # app launcher: whole monitor, dock stays on top
            x, y, width, height = 0, 0, g.width, g.height
        elif popup.get("tall"):  # widgets board: full height along the left edge
            width = min(popup["width"], g.width - 24)
            x, y, height = 12, 12, g.height - PANEL_HEIGHT - 16
        else:
            width, height = min(popup["width"], g.width - 24), min(popup["height"], g.height - PANEL_HEIGHT - 16)
            anchor = popup.get("anchorX")
            dock_x = self._dock_rect()[0] - g.x
            if anchor is None and popup.get("view") == "start" and self.settings.get("taskbarAlign") != "left":
                x = (g.width - width) // 2  # the Start menu opens over the middle of a centered taskbar
            else:
                x = 12 if anchor is None else int(dock_x + anchor - width / 2)  # anchor is dock-relative
            x = max(12, min(x, g.width - width - 12))
            y = g.height - PANEL_HEIGHT - height - 4
        self.popup.set_size_request(width, height)
        self.popup.resize(width, height)
        self.popup.move(g.x + x, g.y + y)
        self.popup.show_all()
        self.popup.move(g.x + x, g.y + y)
        ts = self._x_time()
        self.popup.present_with_time(ts)
        gdk_win = self.popup.get_window()
        if gdk_win is not None:
            gdk_win.focus(ts)
        self.popup.view.grab_focus()
        if popup.get("fullscreen") and self.panel.get_window() is not None:
            self.panel.get_window().raise_()  # both are in the "above" layer
        return False

    def _hide_popup(self) -> None:
        GLib.idle_add(lambda: (self.popup.hide(), self._schedule_dock_hide(), False)[2])

    def _on_popup_focus_out(self, *_):
        GLib.timeout_add(120, self._check_popup_focus)
        return False

    def _check_popup_focus(self):
        if self.popup.get_visible() and not self.popup.is_active():
            self.popup_closed()
        return False

    # ==== apps =============================================================================
    def _load_apps(self) -> None:
        theme = Gtk.IconTheme.get_default()
        infos, apps, icons, themed_names = {}, [], {}, {}
        base = base_apps()  # what the PolyOS image shipped (only some of it is listed)
        cloud_on = set(gaming.enabled(self.settings, self.files.home))
        for info in Gio.AppInfo.get_all():
            if not isinstance(info, Gio.DesktopAppInfo) or not info.should_show():
                continue
            app_id = info.get_id()
            if not app_id or app_id in infos:
                continue
            if app_id == INSTALL_APP and not self._live:
                continue  # "Install PolyOS 7" is for the USB only
            cid = gaming.cloud_id(app_id)
            if cid and cid not in cloud_on:
                continue  # a cloud gaming service that isn't switched on (Settings > Gaming)
            infos[app_id] = info
            apps.append({
                "id": app_id,
                "name": DISPLAY_NAMES.get(app_id) or info.get_display_name() or app_id,
                "hidden": app_hidden(app_id, base),
                "description": info.get_description() or "",
                "categories": [c for c in (info.get_categories() or "").split(";") if c],
                "keywords": list(info.get_keywords() or []),
                "icon": f"/icon/app/{quote(app_id)}",
            })
            icons[app_id] = self._resolve_icon(theme, info.get_icon())
            if icons[app_id] is None and isinstance(info.get_icon(), Gio.ThemedIcon):
                themed_names[app_id] = list(info.get_icon().get_names())  # tried against PolyOS's own set
        apps.sort(key=lambda a: a["name"].casefold())
        supersede_browsers(apps, base)

        index: dict[str, str] = {}
        for pass_no in range(3):  # StartupWMClass beats desktop id beats executable name
            for app_id, info in infos.items():
                stem = app_id[:-len(".desktop")]
                if pass_no == 0:
                    keys = [info.get_startup_wm_class()]
                elif pass_no == 1:
                    keys = [stem, stem.rsplit(".", 1)[-1]]
                else:
                    exe = Path(info.get_executable() or "").name
                    keys = [exe] if exe not in GENERIC_EXECUTABLES else []
                for key in keys:
                    if key:
                        index.setdefault(key.lower(), app_id)
        exes = {}
        for app_id, info in infos.items():  # program name -> app, for apps running in the background
            exe = Path(info.get_executable() or "").name.lower()
            if exe and exe not in GENERIC_EXECUTABLES:
                exes.setdefault(exe, app_id)
                exes.setdefault(exe[:15], app_id)  # the kernel keeps 15 characters of a process's name
        self._exes = exes
        self._infos, self._apps, self._app_icons, self._wm_index = infos, apps, icons, index
        self._app_icon_names = themed_names
        self._icon_cache.clear()

    def _apps_changed(self) -> None:
        GLib.idle_add(lambda: self._schedule_apps_reload() and False)

    def _schedule_apps_reload(self) -> None:
        if self._apps_timer:
            return

        def reload():
            self._apps_timer = 0
            self._load_apps()
            self.bus.publish("apps", apps=self._apps)
            self._windows_changed()
            return False

        self._apps_timer = GLib.timeout_add(500, reload)

    @staticmethod
    def _resolve_icon(theme: Gtk.IconTheme, gicon) -> str | None:
        if gicon is None:
            return None
        if isinstance(gicon, Gio.FileIcon):
            path = gicon.get_file().get_path()
            return path if path and os.path.isfile(path) else None
        if isinstance(gicon, Gio.ThemedIcon):
            names = gicon.get_names()
            for name in names:
                if os.path.isabs(name) and os.path.isfile(name):
                    return name
            info = theme.choose_icon(names, 128, 0)
            if info is not None:
                return info.get_filename()
        return None

    def apps(self) -> list[dict]:
        return self._apps

    def app_icon(self, app_id: str) -> tuple[bytes, str]:
        cached = self._icon_cache.get(app_id)
        if cached:
            return cached
        path = self._app_icons.get(app_id)
        result = None
        if path:
            ext = Path(path).suffix.lower()
            try:
                if ext in (".svg", ".png"):
                    result = (Path(path).read_bytes(), IMAGE_TYPES[ext])
                else:  # xpm, svgz, ...: convert to PNG
                    result = (on_main(self._png_from_file, path), "image/png")
            except (OSError, GLib.Error) as exc:
                log.debug("icon %s unreadable: %s", path, exc)
        if result is None:
            app = next((a for a in self._apps if a["id"] == app_id), None)
            names = [*self._app_icon_names.get(app_id, []), *icon_names(app_id, app["name"] if app else "")]
            icon = bundled_icon(names)
            result = (icon or letter_icon(app["name"] if app else app_id), "image/svg+xml")
        self._icon_cache[app_id] = result
        return result

    @staticmethod
    def _png_from_file(path: str) -> bytes:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(path, 128, 128)
        ok, data = pixbuf.save_to_bufferv("png", [], [])
        return bytes(data)

    def launch(self, app_id: str):
        if app_id in OWN_APPS:
            self.note_launch(app_id)
            return self.open_app(OWN_APPS[app_id])
        on_main(self._launch_main, app_id)
        self.note_launch(app_id)

    def _launch_main(self, app_id: str):
        info = self._infos.get(app_id) or Gio.DesktopAppInfo.new(app_id)
        if info is None:
            raise ApiError("That app is no longer installed", 404)
        ctx = Gdk.Display.get_default().get_app_launch_context()
        ctx.set_timestamp(self._x_time())
        try:
            info.launch([], ctx)
        except GLib.Error as exc:
            raise ApiError(f"Could not start {info.get_display_name()}: {exc.message}", 500) from None

    def run_default(self, what: str):
        if what == "files":
            return self.open_app("files")
        system.run_default(what)

    def open_path(self, path: str):
        p = self.files.resolve(path)
        if p.is_dir():
            return self.open_app("files", str(p))
        if not p.exists():
            raise ApiError("That file no longer exists.", 404)
        if self.settings.get("keepRecent"):  # the Start menu's Recommended (GTK's recent files)
            GLib.idle_add(lambda: Gtk.RecentManager.get_default().add_item(p.as_uri()) and False)
        return on_main(self._open_uri_main, p.as_uri())

    def _exe_index(self) -> dict[str, str]:
        return getattr(self, "_exes", {})

    def _open_uri_main(self, uri: str):
        ctx = Gdk.Display.get_default().get_app_launch_context()
        ctx.set_timestamp(self._x_time())
        try:
            Gio.AppInfo.launch_default_for_uri(uri, ctx)
        except GLib.Error as exc:
            raise ApiError(f"No app is set up to open this file ({exc.message}).", 415) from None

    def terminal_at(self, path: str):
        folder = self.files.resolve(path)
        system.spawn(["x-terminal-emulator"] if system.have("x-terminal-emulator") else ["xterm"], cwd=str(folder))

    def run_command(self, command: str):
        system.run_command(command)
        self.popup_closed()

    def env(self) -> dict:
        installer = next((i for i in ("install-debian.desktop", "calamares.desktop") if i in self._infos), None)
        return {"dev": False, "composited": self.composited, "live": self._live,
                "installer": installer, "panelHeight": PANEL_HEIGHT, "dockHeight": DOCK_HEIGHT,
                "dockMargin": DOCK_MARGIN, "version": __version__}

    # ==== windows ==========================================================================
    def _init_wnck(self) -> None:
        Wnck.set_client_type(Wnck.ClientType.PAGER)  # our requests count as direct user actions
        self.wscreen = Wnck.Screen.get_default()
        self.wscreen.force_update()
        self.wscreen.connect("window-opened", self._on_window_opened)
        self.wscreen.connect("window-closed", lambda *_: self._windows_changed())
        self.wscreen.connect("active-window-changed", self._on_active_changed)
        for win in self.wscreen.get_windows():
            self._watch_window(win)

    def _watch_window(self, win) -> None:
        for sig in ("name-changed", "state-changed", "icon-changed", "class-changed"):
            try:
                win.connect(sig, lambda *_: self._windows_changed())
            except TypeError:
                pass  # signal missing in this libwnck version

    def _on_window_opened(self, _screen, win) -> None:
        self._watch_window(win)
        self._windows_changed()

    def _on_active_changed(self, screen, _previous) -> None:
        self._windows_changed()
        active = screen.get_active_window()
        if active is not None and self._popup is not None and active.get_class_group_name() != "PolyOS":
            self.popup_closed()

    def _windows_changed(self) -> None:
        if self._win_timer:
            return

        def fire():
            self._win_timer = 0
            self.bus.publish("windows", windows=self._windows_main())
            self._sync_fullscreen()
            return False

        self._win_timer = GLib.timeout_add(60, fire)

    def _match_app(self, win) -> str | None:
        for getter in ("get_class_group_name", "get_class_instance_name"):
            fn = getattr(win, getter, None)
            value = fn() if fn else None
            if value and value.lower() in self._wm_index:
                return self._wm_index[value.lower()]
        return None

    def _windows_main(self) -> list[dict]:
        active = self.wscreen.get_active_window()
        active_xid = active.get_xid() if active is not None else None
        out = []
        for win in self.wscreen.get_windows():
            if win.is_skip_tasklist() or win.get_transient() is not None:
                continue
            if win.get_window_type() not in (Wnck.WindowType.NORMAL, Wnck.WindowType.DIALOG):
                continue
            xid = win.get_xid()
            app_id = self._match_app(win)
            out.append({
                "xid": xid,
                "title": win.get_name() or "",
                "appId": app_id,
                "active": xid == active_xid,
                "minimized": win.is_minimized(),
                "fullscreen": win.is_fullscreen(),
                "icon": f"/icon/app/{quote(app_id)}" if app_id else f"/icon/window/{xid}",
            })
        return out

    def windows(self) -> list[dict]:
        return on_main(self._windows_main)

    def window_action(self, xid: int, action: str):
        return on_main(self._window_action_main, xid, action)

    def _window_action_main(self, xid: int, action: str):
        win = Wnck.Window.get(xid)
        if win is None:
            raise ApiError("That window is gone", 404)
        ts = self._x_time()
        if action == "toggle":
            action = "minimize" if win.is_active() and not win.is_minimized() else "activate"
        if action == "activate":
            if win.is_minimized():
                win.unminimize(ts)
            win.activate(ts)
        elif action == "minimize":
            win.minimize()
        elif action == "fullscreen":  # toggle: only the app, no title bar or taskbar
            if win.is_minimized():
                win.unminimize(ts)
            win.activate(ts)
            win.set_fullscreen(not win.is_fullscreen())
        elif action == "close":
            win.close(ts)
        elif action == "kill":  # "Force close": for apps that stopped responding
            pid = win.get_pid()
            if not pid:
                win.close(ts)
                return
            try:
                self.procmon.end(pid, force=True)
            except (ProcessLookupError, PermissionError) as exc:
                raise ApiError(str(exc), 409) from None

    def window_icon(self, xid: int) -> tuple[bytes, str]:
        def grab():
            win = Wnck.Window.get(xid)
            pixbuf = win.get_icon() if win is not None else None
            if pixbuf is None:
                return None
            ok, data = pixbuf.save_to_bufferv("png", [], [])
            return bytes(data) if ok else None

        data = on_main(grab)
        return (data, "image/png") if data else (letter_icon("?"), "image/svg+xml")

    # ==== system ===========================================================================
    def _refresh_system(self, parts) -> dict:
        new = {}
        if "volume" in parts:
            vol = self.audio.get()
            new["volume"] = {k: vol[k] for k in ("available", "level", "muted")}
        if "network" in parts:
            new["network"] = self.network.status()
        if "battery" in parts:
            new["battery"] = system.battery()
        if "brightness" in parts:
            new["brightness"] = self.backlight.get()
        with self._system_lock:
            changed = any(self._system.get(k) != v for k, v in new.items())
            self._system.update(new)
            snapshot = json.loads(json.dumps(self._system))
        if changed:
            self.bus.publish("system", system=snapshot)
        return snapshot

    def _poll_loop(self) -> None:
        tick = 0
        while not self._poll_stop.wait(self._poll_interval):
            tick += 1
            parts = ["volume"]
            if tick % 3 == 0:
                parts += ["network", "brightness"]
            if tick % 15 == 0:
                parts.append("battery")
            try:
                self._refresh_system(parts)
            except Exception:
                log.exception("system poll failed")
            try:
                self._watch_screens()
            except Exception:  # noqa: BLE001
                log.exception("screen watch failed")
            try:
                self._track_usage()
            except Exception:  # noqa: BLE001 - usage is a nicety
                log.debug("usage tracking failed", exc_info=True)

    def _watch_screens(self) -> None:
        """A screen plugged in or out: arrange them again (X doesn't turn a new screen on by itself)."""
        now = display.connectors()
        if now != getattr(self, "_connectors", now):
            log.info("screens changed: %s", now)
            time.sleep(1)  # let the new screen finish introducing itself (EDID)
            self.apply_display_layout()
            GLib.idle_add(lambda: (self._relayout_soon(), False)[1])
        self._connectors = now

    def _track_usage(self) -> None:
        """Settings > Apps > Usage: the seconds since the last poll go to the app in front, if someone's there."""
        now = time.monotonic()
        last, self._usage_last = getattr(self, "_usage_last", None), now
        if last is None or now - last > 120 or self._locked_flag():
            return
        idle = self._idle.idle_ms() if self._idle else 0
        if idle is not None and idle > 120000:
            return
        active = next((w for w in self.windows() if w.get("active") and not w.get("minimized")), None)
        self.usage_tick(active.get("appId") if active else None, now - last)

    def _locked_flag(self) -> bool:
        return self._lock_flag.exists()

    def system_status(self) -> dict:
        with self._system_lock:
            if self._system:
                return json.loads(json.dumps(self._system))
        return self._refresh_system(("volume", "network", "battery", "brightness"))

    def set_volume(self, level=None, delta=None, muted=None, toggle_mute=False):
        self.audio.set(level=level, delta=delta, muted=muted, toggle_mute=toggle_mute)
        return self._refresh_system(("volume",))["volume"]

    def set_brightness(self, level=None, delta=None):
        self.backlight.set(level=level, delta=delta)
        return self._refresh_system(("brightness",))["brightness"]

    def wifi_list(self) -> dict:
        status = self._refresh_system(("network",))["network"]
        if not status["available"] or not status["wifiDevice"] or not status["wifiEnabled"]:
            return {"enabled": status["wifiEnabled"], "networks": []}
        return {"enabled": True, "networks": self.network.wifi_list()}

    def wifi_connect(self, ssid: str, password: str | None):
        try:
            self.network.connect(ssid, password)
        finally:
            self._refresh_system(("network",))

    def wifi_forget(self, ssid: str):
        self.network.forget(ssid)
        self._refresh_system(("network",))

    def wifi_enable(self, enabled: bool):
        self.network.set_wifi(enabled)
        time.sleep(0.5)
        self._refresh_system(("network",))

    # ---- power mode, screen off and sleep ------------------------------------------------
    def _apply_power(self, settings: dict, initial: bool = False) -> None:
        profile = power.apply_mode(settings["powerMode"])
        screen_off, _sleep = power.timers(settings)
        if self._live:
            screen_off = 0  # the live USB: installing takes a while, and a dark screen looks frozen
        power.apply_screen_off(screen_off)
        if settings["powerMode"] == "saver" and not initial:
            level = self.backlight.get()
            if level.get("available") and level.get("level", 0) > power.SAVER_BRIGHTNESS:
                self.set_brightness(level=power.SAVER_BRIGHTNESS)
        log.info("power mode %s (profile %s), screen off %s min", settings["powerMode"], profile, screen_off)

    def _idle_tick(self):
        """Lock when the screen turns off, and sleep after the chosen idle time (not on the live USB)."""
        idle = self._idle.idle_ms() if self._idle else None
        if idle is None or self._game_active:  # a game with a controller has no keyboard/mouse input
            return True
        settings = self.settings.snapshot()
        screen_off, sleep_after = power.timers(settings)
        minutes = idle / 60000
        if minutes < 1:
            self._idle_slept = False
            return True
        # An update installed with "Ask before restarting" off: restart PolyOS (not the computer)
        # while nobody is using it. Never while locked, so the lock screen can't be skipped.
        if getattr(self, "_restart_when_idle", False) and minutes >= 10 and self.lock_window is None \
                and not self._fullscreen_app and self.jobs.running() is None:
            self._restart_when_idle = False
            log.info("restarting PolyOS to finish an update")
            self.restart_shell()
            return True
        if screen_off and minutes >= screen_off and settings["lockOnSleep"] and self.lock_window is None \
                and not self.env()["live"]:
            self.lock()
        if sleep_after and minutes >= sleep_after and not self._idle_slept and not self.env()["live"] \
                and self.jobs.running() is None:
            self._idle_slept = True
            try:
                self.power("suspend")
            except (ApiError, RuntimeError) as exc:
                log.warning("idle sleep failed: %s", exc)
        return True

    # ---- update notices -----------------------------------------------------------------------
    def start_update_watch(self):
        path = paths.config_dir() / "update-notices.json"
        try:
            seen = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            seen = {}

        def loop():
            while True:
                try:
                    for notice in (self.first_start_notice(seen), self.update_notice(seen)):
                        if notice:
                            path.write_text(json.dumps(seen), "utf-8")
                            if notice["kind"] == "setting-up":
                                threading.Thread(target=self._notify, args=(notice["title"], notice["body"], []), daemon=True).start()
                            else:
                                self._show_update_notice(notice)
                except Exception:  # noqa: BLE001 - never let a notice take the shell down
                    log.exception("update notice failed")
                # right after installing, check often so "all set up" shows soon after it's done
                time.sleep(60 if self.first_start_pending() else 600)
        threading.Thread(target=loop, name="polyos-updates", daemon=True).start()
        return False

    def _notify(self, title: str, body: str, actions: list[tuple[str, str]]) -> str:
        """A desktop notification with buttons; the chosen button's id, or "" (dismissed, or no buttons)."""
        base = ["notify-send", "-a", "PolyOS", "-i", "polyos", title, body]
        if not system.have("notify-send"):
            return ""
        try:
            proc = subprocess.run([*base, *[f"--action={k}={v}" for k, v in actions], "--wait"], capture_output=True,
                                  text=True, timeout=6 * 3600)
            if proc.returncode == 0:
                return proc.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""
        system.spawn(base)  # an older notify-send without buttons
        return ""

    def _show_update_notice(self, n: dict) -> None:
        if n["kind"] == "updated":
            summary = self._notes_summary(n.get("notes")) or "See what’s new in Settings › Updates."
            threading.Thread(target=lambda: self._notify(f"PolyOS was updated to {n['version']}", summary, [("new", "What’s new")]) == "new"
                             and self.open_app("settings", "updates"), daemon=True).start()
            return
        if n["kind"] == "restart":
            if not n["ask"]:
                self._restart_when_idle = True
                return
            if self._notify(f"PolyOS {n['version']} is installed", "Restart PolyOS to finish. Your apps and files stay as they are.",
                            [("restart", "Restart PolyOS"), ("later", "Later")]) == "restart":
                self.restart_shell()
            return
        summary = self._notes_summary(n.get("notes")) or "Security improvements and fixes."
        if n["autoInstall"] or n["tonight"]:
            self._notify(f"PolyOS {n['version']} is ready", f"{summary} It installs at {n['time']}.", [])
            return
        choice = self._notify(f"PolyOS {n['version']} is ready", summary,
                              [("tonight", "Install tonight"), ("schedule", "Schedule"), ("later", "Later")])
        if choice == "tonight":
            self.updates_action("tonight")
        elif choice == "schedule":
            self.open_app("settings", "updates")

    @staticmethod
    def _notes_summary(notes: str | None) -> str:
        """The first real line of the release notes, short enough for a notification."""
        first = next((line.strip(" -*•#") for line in (notes or "").replace("**", "").replace("`", "").splitlines()
                      if len(line.strip(" -*•#|")) > 12), "")
        return (first[:117] + "…") if len(first) > 120 else first

    def power(self, action: str):
        self.popup_closed()
        if action == "logout":
            GLib.idle_add(self.quit, EXIT_LOGOUT)
            return
        if action == "lock":
            return self.lock()
        if action == "suspend" and self.settings.get("lockOnSleep"):
            self.lock()  # wake up to the lock screen
        system.power(action)

    # ==== lock screen ======================================================================
    # An override-redirect window over every monitor that grabs the keyboard and pointer, so
    # it appears instantly (no switch to the login screen) and shortcuts can't get past it.
    def lock(self):
        self.jobs.admin.forget()  # the administrator password is asked again after locking
        try:
            self._lock_flag.touch()
        except OSError:
            pass
        GLib.idle_add(self._lock_main)

    def _lock_main(self):
        self.popup_closed()
        if self.lock_window is None:
            win = Gtk.Window(type=Gtk.WindowType.POPUP)
            win.set_title("PolyOS lock")
            win.view = self._view("surface=lock", transparent=False)
            win.add(win.view)
            self.lock_window = win
        win = self.lock_window
        screen = self.gdk_screen
        win.move(0, 0)
        win.resize(screen.get_width(), screen.get_height())
        win.show_all()
        win.get_window().raise_()
        win.view.grab_focus()
        self._grab_input(40)
        GLib.timeout_add(1000, self._keep_lock_on_top)
        self.bus.publish("lock", locked=True)
        return False

    def _grab_input(self, tries: int):
        if self.lock_window is None or self.lock_window.get_window() is None:
            return False
        seat = Gdk.Display.get_default().get_default_seat()
        status = seat.grab(self.lock_window.get_window(), Gdk.SeatCapabilities.ALL, True, None, None, None, None)
        if status != Gdk.GrabStatus.SUCCESS and tries > 0:
            GLib.timeout_add(100, self._grab_input, tries - 1)  # a menu or drag may hold a grab briefly
        elif status != Gdk.GrabStatus.SUCCESS:
            log.warning("lock screen could not grab the keyboard (%s)", status)
        return False

    def _keep_lock_on_top(self):
        if self.lock_window is None or not self.lock_window.get_visible():
            return False
        gdk_win = self.lock_window.get_window()
        if gdk_win is not None:
            gdk_win.raise_()
        return True

    def _unlock_main(self):
        if self.lock_window is not None:
            Gdk.Display.get_default().get_default_seat().ungrab()
            self.lock_window.destroy()
            self.lock_window = None
        self._lock_flag.unlink(missing_ok=True)
        self.bus.publish("lock", locked=False)
        return False

    def lock_unlock(self, password: str):
        from . import pamauth

        with self._unlock_lock:
            self.unlock_throttle.check()
            if self.pin_unlock(password):  # a PIN (Settings > Account), checked as root
                self.unlock_throttle.succeeded()
                GLib.idle_add(self._unlock_main)
                return {"ok": True}
            try:
                ok = pamauth.authenticate(getpass.getuser(), password)
            except OSError as exc:
                log.error("PAM unavailable: %s", exc)
                raise ApiError("Unlocking isn't working. Restart the computer.", 500) from None
            if not ok:
                self.unlock_throttle.failed()
                raise ApiError("That PIN or password isn't right. Try again." if self.pin_status()["set"]
                               else "That password isn't right. Try again.", 403)
            self.unlock_throttle.succeeded()
            threading.Thread(target=self.pin_reset, daemon=True).start()
        GLib.idle_add(self._unlock_main)
        return {"ok": True}

    def lock_recover(self, key: str, password: str):
        from .recovery import RecoveryError, run_helper

        try:
            run_helper(getpass.getuser(), key, password)
        except RecoveryError as exc:
            raise ApiError(str(exc), 403) from None
        GLib.idle_add(self._unlock_main)
        return {"ok": True}

    def _watch_gnome_background(self) -> None:
        """Photos (Ristretto) and other apps set the wallpaper through GNOME's background setting: follow it."""
        source = Gio.SettingsSchemaSource.get_default()
        if source is None or source.lookup("org.gnome.desktop.background", True) is None:
            return
        self._gnome_bg = Gio.Settings.new("org.gnome.desktop.background")

        def changed(settings, key):
            uri = settings.get_string(key)
            if not uri.startswith("file://"):
                return
            path = Gio.File.new_for_uri(uri).get_path()
            if path and path != self.settings.get("wallpaper"):
                try:
                    self.update_settings({"wallpaper": path})
                except ApiError as exc:
                    log.info("background from another app not used: %s", exc)
        self._gnome_bg.connect("changed::picture-uri", changed)
        self._gnome_bg.connect("changed::picture-uri-dark", changed)

    def _watch_logind(self) -> None:
        """Lock before sleeping, and when something asks logind to lock this session."""
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as exc:
            log.warning("no system bus: %s", exc.message)
            return
        login1 = "org.freedesktop.login1"
        self._system_bus = bus
        self._sleep_fd = None
        self._take_sleep_delay()
        bus.signal_subscribe(login1, login1 + ".Manager", "PrepareForSleep", "/org/freedesktop/login1", None,
                             Gio.DBusSignalFlags.NONE, lambda *args: self._on_prepare_for_sleep(args[5].unpack()[0]))
        try:
            session_id = os.environ.get("XDG_SESSION_ID")
            if session_id:
                reply = bus.call_sync(login1, "/org/freedesktop/login1", login1 + ".Manager", "GetSession",
                                      GLib.Variant("(s)", (session_id,)), GLib.VariantType("(o)"),
                                      Gio.DBusCallFlags.NONE, 3000, None)
            else:
                reply = bus.call_sync(login1, "/org/freedesktop/login1", login1 + ".Manager", "GetSessionByPID",
                                      GLib.Variant("(u)", (os.getpid(),)), GLib.VariantType("(o)"),
                                      Gio.DBusCallFlags.NONE, 3000, None)
            path = reply.unpack()[0]
            bus.signal_subscribe(login1, login1 + ".Session", "Lock", path, None, Gio.DBusSignalFlags.NONE,
                                 lambda *args: self.lock())
        except GLib.Error as exc:
            log.info("logind session lock signal unavailable: %s", exc.message)

    # Closing the lid: without a delay the computer can fall asleep while the lock screen is still
    # loading, and wake up to a black window. logind waits (5 s at most) while we hold a "delay"
    # inhibitor, so it's released once the lock screen has had time to draw.
    def _take_sleep_delay(self) -> None:
        if self._system_bus is None or self._sleep_fd is not None:
            return
        try:
            result, fds = self._system_bus.call_with_unix_fd_list_sync(
                "org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager", "Inhibit",
                GLib.Variant("(ssss)", ("sleep", "PolyOS", "Showing the lock screen before sleeping", "delay")),
                GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, 3000, None, None)
            self._sleep_fd = fds.get(result.unpack()[0])
        except GLib.Error as exc:
            log.info("no sleep delay from logind: %s", exc.message)

    def _release_sleep_delay(self):
        if self._sleep_fd is not None:
            try:
                os.close(self._sleep_fd)
            except OSError:
                pass
            self._sleep_fd = None
        return False

    def _on_prepare_for_sleep(self, going: bool) -> None:
        if going:
            if self.settings.get("lockOnSleep"):
                self.lock()
                GLib.timeout_add(1500, self._release_sleep_delay)  # the lock screen is local and quick to draw
            else:
                self._release_sleep_delay()
            return
        self._take_sleep_delay()  # awake: ready for next time
        # Some graphics drivers lose the compositor's picture while asleep (a black screen until
        # something moves): picom resets on SIGUSR1, and every PolyOS window draws itself again.
        subprocess.run(["pkill", "-USR1", "-x", "-u", str(os.getuid()), "picom"], check=False, capture_output=True)
        GLib.timeout_add(800, self._redraw_after_sleep)

    def _redraw_after_sleep(self):
        lock = self.lock_window
        if lock is not None and lock.get_visible() and lock.view.is_loading():
            lock.view.reload()  # it was still loading when the computer fell asleep
        for win in Gtk.Window.list_toplevels():
            win.queue_draw()
        return False

    def sysinfo(self) -> dict:
        return system.sysinfo()

    def restart_shell(self):
        GLib.idle_add(self.quit, EXIT_RESTART)

    # ==== app windows (Settings, Files, first-run setup) ======================================
    def open_app(self, name: str, page: str | None = None):
        if name in SINGLE_WINDOWS:
            GLib.idle_add(self._open_single_main, name, page)
            return
        handler = {"settings": self._open_settings_main, "files": self._open_files_main,
                   "setup": self._open_setup_main}[name]
        GLib.idle_add(handler, page)

    def _open_single_main(self, name: str, page: str | None):
        win = self.single_windows.get(name)
        if win is not None:
            if page:
                self.bus.publish("navigate", surface=SINGLE_WINDOWS[name][0], page=page)
            win.present_with_time(self._x_time())
            return False
        surface, title, wmclass, size = SINGLE_WINDOWS[name]
        query = f"surface={surface}" + (f"&page={quote(page)}" if page else "")
        win = self._app_window(query, title, wmclass, size)
        win.connect("destroy", lambda *_: self.single_windows.pop(name, None))
        self.single_windows[name] = win
        win.show_all()
        return False

    def _app_window(self, query: str, title: str, wmclass: str, size: tuple[int, int]) -> Gtk.Window:
        win = Gtk.Window(title=title)
        win.set_wmclass(wmclass, wmclass)
        win.set_icon_name(wmclass)
        win.set_default_size(*size)
        win.set_position(Gtk.WindowPosition.CENTER)
        win.view = self._view(query, transparent=False)
        # the page's <title> becomes the window title (e.g. the folder a Files window shows)
        win.view.connect("notify::title", lambda view, _p: win.set_title(view.get_title() or title))
        win.add(win.view)
        return win

    def _open_settings_main(self, page: str | None):
        if self.settings_window is None:
            query = "surface=settings" + (f"&page={quote(page)}" if page else "")
            win = self._app_window(query, "Settings", "polyos-settings", (1000, 680))
            win.connect("destroy", lambda *_: setattr(self, "settings_window", None))
            self.settings_window = win
            win.show_all()
        else:
            if page:
                self.bus.publish("navigate", surface="settings", page=page)
            self.settings_window.present_with_time(self._x_time())
        return False

    def _open_files_main(self, path: str | None):
        query = "surface=files" + (f"&path={quote(path)}" if path else "")
        win = self._app_window(query, "Files", "polyos-files", (1080, 700))
        win.connect("destroy", lambda w: self.files_windows.discard(w))
        self.files_windows.add(win)
        win.show_all()
        return False

    def _open_setup_main(self, _page=None):
        if self.setup_window is not None:
            self.setup_window.present_with_time(self._x_time())
            return False
        win = self._app_window("surface=setup", "Welcome to PolyOS", "polyos-setup", (1100, 720))
        win.set_decorated(False)
        win.fullscreen()
        win.connect("destroy", lambda *_: setattr(self, "setup_window", None))
        self.setup_window = win
        win.show_all()
        return False

    # ==== Task Manager =====================================================================
    def _windows_with_pids(self) -> list[dict]:
        out = []
        by_id = {a["id"]: a for a in self._apps}
        for win in self._windows_main():
            wnck_win = Wnck.Window.get(win["xid"])
            app = by_id.get(win["appId"]) if win["appId"] else None
            out.append({**win, "pid": wnck_win.get_pid() if wnck_win is not None else 0,
                        "name": app["name"] if app else win["title"]})
        return out

    def procs(self) -> dict:
        windows = on_main(self._windows_with_pids)
        with self._procs_lock:
            return self.procmon.sample(windows)

    def procs_end(self, pid: int, force: bool):
        try:
            self.procmon.end(pid, force)
        except ProcessLookupError as exc:
            raise ApiError(str(exc), 404) from None
        except PermissionError as exc:
            raise ApiError(str(exc) if "PolyOS" in str(exc) else "You can't end that process.", 403) from None

    def theme_icon(self, name: str) -> tuple[bytes, str]:
        if not name or len(name) > 120 or "/" in name or name.startswith("."):
            return letter_icon("?"), "image/svg+xml"
        cached = self._icon_cache.get("theme:" + name)
        if cached:
            return cached

        names = [n for n in name.split(",") if n][:6]  # candidates, best first

        def find():
            info = Gtk.IconTheme.get_default().choose_icon(names, 128, 0)
            return info.get_filename() if info is not None else None

        path = on_main(find)
        result = None
        if path:
            ext = Path(path).suffix.lower()
            try:
                result = (Path(path).read_bytes(), IMAGE_TYPES[ext]) if ext in (".svg", ".png") else \
                    (on_main(self._png_from_file, path), "image/png")
            except (OSError, GLib.Error):
                result = None
        if result is None:
            icon = bundled_icon(names)
            result = (icon, "image/svg+xml") if icon else (letter_icon(names[0].split(".")[-1] if names else "?"), "image/svg+xml")
        self._icon_cache["theme:" + name] = result
        return result

    def update_settings(self, patch: dict) -> dict:
        settings = super().update_settings(patch)
        if {"taskbarStyle", "taskbarAutoHide"} & set(patch):
            GLib.idle_add(lambda: (self._apply_taskbar(settings), False)[1])
        if {"powerMode", "screenOff", "sleepAfter"} & set(patch):
            threading.Thread(target=self._apply_power, args=(settings,), daemon=True).start()
        if "theme" in patch:
            try:
                theme.apply_gtk(settings["theme"])
            except OSError as exc:
                log.warning("could not write GTK settings: %s", exc)
            theme.switch_openbox(paths.runtime_dir() / "openbox-rc.xml", settings["theme"])
        if "showAllApps" in patch:
            self.bus.publish("apps", apps=self._apps)
        if "nightLight" in patch:
            threading.Thread(target=self.apply_night_light, daemon=True).start()
        if "airplaneMode" in patch:
            def airplane():
                self.apply_airplane(settings["airplaneMode"])
                time.sleep(0.5)
                self._refresh_system(("network",))
            threading.Thread(target=airplane, daemon=True).start()
        if "backgroundLimit" in patch and not self._game_active:
            self._poll_interval = self._base_poll()
        return settings

    def finish_setup(self):
        self.update_settings({"setupDone": True})
        GLib.idle_add(lambda: (self.setup_window.destroy() if self.setup_window else None, False)[1])

    def pick_wallpaper(self):
        GLib.idle_add(self._pick_wallpaper_main)
        return {"ok": True, "pending": True}

    def _pick_wallpaper_main(self):
        dialog = Gtk.FileChooserNative.new("Choose a wallpaper", self.settings_window,
                                           Gtk.FileChooserAction.OPEN, "_Set Wallpaper", "_Cancel")
        images = Gtk.FileFilter()
        images.set_name("Images")
        for ext in IMAGE_TYPES:
            images.add_pattern(f"*{ext}")
            images.add_pattern(f"*{ext.upper()}")
        dialog.add_filter(images)
        pictures = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
        if pictures and os.path.isdir(pictures):
            dialog.set_current_folder(pictures)

        def on_response(chooser, response):
            if response == Gtk.ResponseType.ACCEPT and chooser.get_filename():
                try:
                    self.update_settings({"wallpaper": chooser.get_filename()})
                except ApiError as exc:
                    log.warning("wallpaper rejected: %s", exc)
                    self.notify("That picture can't be a wallpaper", "Use a JPG, PNG, WebP or AVIF image.")
            self._chooser = None

        dialog.connect("response", on_response)
        self._chooser = dialog  # keep a reference while the dialog is open
        dialog.show()
        return False

    # ==== XDG autostart ====================================================================
    def run_autostart(self):
        dirs = [Path(GLib.get_user_config_dir()) / "autostart"]
        dirs += [Path(d) / "autostart" for d in GLib.get_system_config_dirs()]
        seen: set[str] = set()
        for directory in dirs:
            if not directory.is_dir():
                continue
            for entry in sorted(directory.glob("*.desktop")):
                if entry.name in seen:
                    continue
                seen.add(entry.name)  # a user file overrides the system one, even if hidden
                info = Gio.DesktopAppInfo.new_from_filename(str(entry))
                if info is None or info.get_is_hidden() or not info.get_show_in(None):
                    continue
                if (info.get_string("X-GNOME-Autostart-enabled") or "").lower() == "false":
                    continue
                try:
                    info.launch([], None)
                    log.info("autostart: %s", entry.name)
                except GLib.Error as exc:
                    log.warning("autostart %s failed: %s", entry.name, exc.message)
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="polyos-shell", description="PolyOS desktop shell")
    parser.add_argument("--debug", action="store_true", help="verbose logs and the web inspector")
    parser.add_argument("--autostart", action="store_true", help="run XDG autostart entries")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    initialized, _ = Gtk.init_check(sys.argv)
    if not initialized:
        log.error("cannot open the X display (DISPLAY=%s)", os.environ.get("DISPLAY"))
        return 1

    settings = Settings(paths.config_dir() / "settings.json")
    bus = EventBus()
    token = secrets.token_urlsafe(32)
    shell = DesktopShell(settings, bus, token, debug=args.debug)
    ctl_token = secrets.token_urlsafe(32)  # polyos-ctl's own, limited token (server.CTL_API)
    server = Server(shell, paths.UI_DIR, token, ctl_token=ctl_token)
    server.start()
    paths.write_runtime_info({"port": server.port, "token": ctl_token, "pid": os.getpid(), "version": __version__})

    shell.start(server.base_url)
    if shell._live:
        shell.show_install_app()
    if not settings.get("setupDone"):
        shell.open_app("setup")  # live USB: the installer; first sign-in: PolyOS's welcome
    if args.autostart:
        GLib.timeout_add_seconds(2, shell.run_autostart)
    if not shell._live:  # the update service's news: "ready" and "installed" notices
        GLib.timeout_add_seconds(90, shell.start_update_watch)
        GLib.timeout_add_seconds(60, shell.start_account_loop)  # Poly Account check-ins, when connected
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, shell.quit, EXIT_LOGOUT)
    try:
        Gtk.main()
    finally:
        server.stop()
        paths.clear_runtime_info(os.getpid())
    return shell.exit_code


if __name__ == "__main__":
    sys.exit(main())
