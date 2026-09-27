"""The PolyOS login and lock screen: a LightDM greeter that shows the PolyOS web UI.

LightDM starts this as the "lightdm" user through /usr/share/xgreeters/polyos-greeter.desktop.
It is also the lock screen: `dm-tool lock` switches to the greeter with the lock hint set.
The polyos-greeter wrapper falls back to lightdm-gtk-greeter if this fails to start.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("GDK_BACKEND", "x11")
os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")

import gi  # noqa: E402

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("LightDM", "1")
try:
    gi.require_version("WebKit2", "4.1")
except ValueError:
    gi.require_version("WebKit2", "4.0")
from gi.repository import Gdk, GLib, Gtk, LightDM, WebKit2  # noqa: E402

from . import __version__, paths, system  # noqa: E402
from .backend import DOCK_HEIGHT, DOCK_MARGIN, PANEL_HEIGHT, Backend  # noqa: E402
from .core import ApiError, EventBus, Settings  # noqa: E402
from .mainloop import on_main  # noqa: E402
from .server import GREETER_API, Server  # noqa: E402

log = logging.getLogger("polyos.greeter")



def _has_pin(user: str) -> bool:
    """Whether a person signs in with a PIN too (asked from the PIN service; the hash stays with root)."""
    from . import pin
    try:
        st = pin.ask({"op": "status", "user": user}, timeout=2)
        return bool(st.get("set") and not st.get("blocked"))
    except (OSError, ValueError):
        return False

class GreeterBackend(Backend):
    def __init__(self, settings: Settings, bus: EventBus, greeter: LightDM.Greeter):
        super().__init__(settings, bus, home=Path(tempfile.gettempdir()))
        self.greeter = greeter
        self._login_lock = threading.Lock()
        self._prompted = threading.Event()
        self._done = threading.Event()
        self._messages: list[str] = []
        self._authenticated = False
        greeter.connect("show-prompt", lambda _g, _text, _type: self._prompted.set())
        greeter.connect("show-message", lambda _g, text, _type: self._messages.append(text))
        greeter.connect("authentication-complete", self._on_complete)

    # ---- what the shared UI boot needs ----------------------------------------------------
    def apps(self): return []
    def windows(self): return []
    def user(self): return {"name": "", "fullName": ""}

    def system_status(self) -> dict:
        return {"volume": {"available": False, "level": 0, "muted": False},
                "brightness": system.Backlight().get(), "battery": system.battery(),
                "network": system.Network().status()}

    def env(self) -> dict:
        return {"dev": False, "composited": False, "live": False, "installer": None, "greeter": True,
                "panelHeight": PANEL_HEIGHT, "dockHeight": DOCK_HEIGHT, "dockMargin": DOCK_MARGIN,
                "version": __version__}

    # ---- login ------------------------------------------------------------------------
    def greeter_state(self) -> dict:
        return on_main(self._state_main)

    def _state_main(self) -> dict:
        users = [{"name": u.get_name(), "displayName": u.get_display_name() or u.get_name(),
                  "loggedIn": u.get_logged_in(), "pin": _has_pin(u.get_name())}
                 for u in LightDM.UserList.get_instance().get_users()]
        sessions = [{"key": s.get_key(), "name": s.get_name()} for s in LightDM.get_sessions()]
        g = self.greeter
        selected = g.get_select_user_hint() or (users[0]["name"] if len(users) == 1 else None)
        return {
            "users": users,
            "sessions": sessions,
            "defaultSession": g.get_default_session_hint() or "polyos",
            "selectedUser": selected,
            "lock": bool(g.get_lock_hint()),
            "hideUsers": bool(g.get_hide_users_hint()),
            "hostname": LightDM.get_hostname(),
            "can": {"shutdown": LightDM.get_can_shutdown(), "restart": LightDM.get_can_restart(),
                    "suspend": LightDM.get_can_suspend()},
        }

    def _on_complete(self, greeter) -> None:
        self._authenticated = greeter.get_is_authenticated()
        self._done.set()

    def _begin(self, user: str) -> None:
        if self.greeter.get_in_authentication():
            self.greeter.cancel_authentication()
        self._prompted.clear()
        self._done.clear()
        self._messages = []
        self._authenticated = False
        self.greeter.authenticate(user)

    def greeter_login(self, user: str, password: str, session: str | None):
        with self._login_lock:
            on_main(self._begin, user)
            if not self._prompted.wait(15) and not self._done.is_set():
                raise ApiError("The login service didn't answer. Try again.", 504)
            if not self._done.is_set():
                on_main(self.greeter.respond, password)
            if not self._done.wait(30):
                raise ApiError("Signing in took too long. Try again.", 504)
            if not self._authenticated:
                detail = next((m for m in reversed(self._messages) if m.strip()), None)
                raise ApiError(detail or "The password entered is incorrect. Please try again.", 403)
            on_main(self._start_session, session)
        return {"ok": True}

    def _start_session(self, session: str | None) -> None:
        try:
            self.greeter.start_session_sync(session or None)
        except GLib.Error as exc:
            raise ApiError(f"Couldn't start the desktop: {exc.message}", 500) from None

    def greeter_power(self, action: str):
        fn = {"shutdown": LightDM.shutdown, "restart": LightDM.restart, "suspend": LightDM.suspend}[action]
        try:
            on_main(fn)
        except GLib.Error as exc:
            raise ApiError(f"Couldn't {action}: {exc.message}", 500) from None

    def greeter_recover(self, user: str, key: str, password: str):
        """Forgot password: set a new one with the account's recovery key (polyos-recover via pkexec)."""
        from .recovery import RecoveryError, run_helper

        try:
            run_helper(user, key, password)
        except RecoveryError as exc:
            raise ApiError(str(exc), 403) from None
        return {"ok": True}


def _setup_display() -> None:
    rc, out = system.run(["xrandr", "--query"], 5)
    scale = system.auto_scale(system.parse_xrandr_dpi(out) if rc == 0 else None)
    if scale > 1:
        os.environ.setdefault("GDK_SCALE", str(scale))
        os.environ.setdefault("GDK_DPI_SCALE", str(1 / scale))
    if system.have("xsetroot"):
        subprocess.run(["xsetroot", "-solid", "#151515", "-cursor_name", "left_ptr"], check=False)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _setup_display()
    initialized, _ = Gtk.init_check(sys.argv)
    if not initialized:
        log.error("cannot open the display")
        return 1

    greeter = LightDM.Greeter()
    greeter.connect_to_daemon_sync()

    # The greeter runs as "lightdm" and can't read users' settings: it uses PolyOS defaults.
    settings = Settings(Path(tempfile.gettempdir()) / f"polyos-greeter-{os.getpid()}.json")
    bus = EventBus()
    token = secrets.token_urlsafe(32)
    backend = GreeterBackend(settings, bus, greeter)
    server = Server(backend, paths.UI_DIR, token, allow=GREETER_API)
    server.start()

    ucm = WebKit2.UserContentManager()
    ucm.add_script(WebKit2.UserScript.new(
        f"window.POLYOS = {json.dumps({'token': token})};", WebKit2.UserContentInjectedFrames.TOP_FRAME,
        WebKit2.UserScriptInjectionTime.START, None, None))
    view = WebKit2.WebView.new_with_user_content_manager(ucm)
    view.get_settings().set_enable_write_console_messages_to_stdout(True)
    color = Gdk.RGBA()
    color.parse("#151515")
    view.set_background_color(color)
    view.connect("context-menu", lambda *_: True)

    def only_local(_view, decision, _type):  # nothing but the login UI loads here
        action = getattr(decision, "get_navigation_action", None)
        if action and not action().get_request().get_uri().startswith(server.base_url + "/"):
            decision.ignore()
            return True
        return False

    view.connect("decide-policy", only_local)
    view.connect("web-process-terminated", lambda v, _r: GLib.timeout_add(500, lambda: (v.reload(), False)[1]))
    view.load_uri(f"{server.base_url}/index.html?surface=greeter")

    win = Gtk.Window(title="PolyOS login")
    win.set_decorated(False)
    win.add(view)

    def layout(*_):
        display = Gdk.Display.get_default()
        geo = (display.get_primary_monitor() or display.get_monitor(0)).get_geometry()
        win.move(geo.x, geo.y)
        win.set_size_request(geo.width, geo.height)
        win.resize(geo.width, geo.height)

    layout()
    Gdk.Screen.get_default().connect("monitors-changed", layout)
    win.show_all()

    def grab_focus():  # there's no window manager on the login screen to give us focus
        gdk_win = win.get_window()
        if gdk_win is not None:
            gdk_win.focus(Gdk.CURRENT_TIME)
        view.grab_focus()
        return False

    GLib.timeout_add(300, grab_focus)
    try:
        Gtk.main()
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
