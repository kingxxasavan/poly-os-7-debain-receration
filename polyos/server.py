"""Loopback HTTP bridge between the web UI surfaces and the backend.

Security model: the server binds to 127.0.0.1 only, rejects foreign Host headers
(DNS rebinding), and every API/icon/wallpaper request must carry a random per-session
token. In the real shell the token reaches the pages through a WebKit user script, so
it is never served over HTTP or written to disk; it is only embedded in HTML in dev mode.

polyos-ctl (keybindings, the command line) gets a second token, written to the session's
runtime file, that opens only CTL_API: popups, apps, volume, brightness, power. So another
program running as you (or a command Vara runs) can't reach installing, drivers, accounts or
settings through the shell. The web UI is not the security boundary either way: everything
done as root goes through polyos-admin, which checks every request itself.
"""

from __future__ import annotations

import hmac
import json
import logging
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .backend import MIXER_TABS, OPEN_APPS, POWER_ACTIONS, RUN_TARGETS
from .core import IMAGE_TYPES, ApiError
from .vara import tools_overview

log = logging.getLogger("polyos.server")

MAX_BODY = 64 * 1024
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".woff2": "font/woff2",
    ".ico": "image/x-icon",
    ".ttf": "font/ttf",
    **IMAGE_TYPES,
}
CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'{extra}; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'")
DEV_ONLY = {"dev.html", "js/dev.js", "css/dev.css"}
# Everything the login screen's UI may call; the greeter's server answers nothing else.
GREETER_API = frozenset({"/api/state", "/api/events", "/api/greeter/state", "/api/greeter/login",
                         "/api/greeter/power", "/api/greeter/recover", "/api/performance",
                         "/wallpaper/current", "/wallpaper/lock"})


def _str(body: dict, key: str, max_len: int = 512) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ApiError(f"'{key}' must be a non-empty string")
    return value


def _opt_int(body: dict, key: str) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(f"'{key}' must be a number")
    return int(value)


def _int(body: dict, key: str) -> int:
    value = _opt_int(body, key)
    if value is None:
        raise ApiError(f"'{key}' is required")
    return value


def _opt_rate(body: dict) -> float | None:
    value = body.get("rate")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 1000:
        raise ApiError("'rate' must be a refresh rate in Hz")
    return float(value)


def _opt_bool(body: dict, key: str) -> bool | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ApiError(f"'{key}' must be true or false")
    return value


def _choice(body: dict, key: str, choices) -> str:
    value = _str(body, key)
    if value not in choices:
        raise ApiError(f"'{key}' must be one of: {', '.join(choices)}")
    return value


def _opt_str(body: dict, key: str) -> str | None:
    return None if body.get(key) in (None, "") else _str(body, key)


def _str_list(body: dict, key: str, max_items: int = 1000) -> list[str]:
    value = body.get(key)
    if not isinstance(value, list) or not value or len(value) > max_items or             not all(isinstance(v, str) and 0 < len(v) <= 4096 for v in value):
        raise ApiError(f"'{key}' must be a list of paths")
    return value


def _q(query: dict, key: str) -> str | None:
    return query.get(key, [None])[0]


# What polyos-ctl may do (its token opens nothing else).
CTL_API = {("GET", "/api/state"), *{("POST", p) for p in (
    "/api/popup", "/api/open", "/api/files/open", "/api/volume", "/api/brightness", "/api/power", "/api/run",
    "/api/shell/restart", "/api/settings")}}
CTL_SETTINGS = {"developerMode"}  # `polyos-ctl dev off` rescues a broken interface

GET_API = {
    "/api/state": lambda be, q: be.state(),
    "/api/wifi": lambda be, q: be.wifi_list(),
    "/api/wallpapers": lambda be, q: be.wallpapers(),
    "/api/sysinfo": lambda be, q: be.sysinfo(),
    "/api/files/places": lambda be, q: be.files.places() | {"trashCount": be.files.trash_count()},
    "/api/files/list": lambda be, q: (be.files.trash_list() if _q(q, "path") == "trash:///"
                                      else be.files.list(_q(q, "path"), _q(q, "hidden") == "1")),
    "/api/files/search": lambda be, q: be.files.search(_q(q, "path"), _q(q, "q") or "", _q(q, "hidden") == "1"),
    "/api/files/info": lambda be, q: be.files.info(_q(q, "path") or ""),
    "/api/greeter/state": lambda be, q: be.greeter_state(),
    "/api/recent-files": lambda be, q: {"files": be.recent_files()},
    "/api/storage": lambda be, q: be.storage(),
    "/api/monitors": lambda be, q: {"monitors": be.monitors()},
    "/api/drivers/firmware": lambda be, q: be.firmware_status(),
    "/api/power/windows": lambda be, q: {"available": be.windows_installed()},
    "/api/account/pin": lambda be, q: be.pin_status(),
    "/api/power/keys": lambda be, q: be.power_keys(),
    "/api/apps/usage": lambda be, q: be.usage(),
    "/api/background-apps": lambda be, q: {"apps": be.background_apps()},
    "/api/vara/history": lambda be, q: be.vara.state(),
    "/api/vara/tools": lambda be, q: tools_overview(be.vara),
    "/api/vara/config": lambda be, q: be.vara.config.public(),
    "/api/admin/status": lambda be, q: be.admin_status(),
    "/api/jobs": lambda be, q: {"jobs": be.jobs.list()},
    "/api/install/probe": lambda be, q: be.install_probe(),
    "/api/drivers": lambda be, q: be.drivers_scan(),
    "/api/store": lambda be, q: be.store_list(),
    "/api/procs": lambda be, q: be.procs(),
    "/api/performance": lambda be, q: be.performance(),
    "/api/camera": lambda be, q: be.camera_status(),
    "/api/power/modes": lambda be, q: be.power_modes(),
    "/api/packs": lambda be, q: be.packs(),
    "/api/gaming/cloud": lambda be, q: be.cloud_gaming(),
    "/api/apps/manage": lambda be, q: be.apps_manage(),
    "/api/sound/devices": lambda be, q: be.sound_devices(),
    "/api/displays": lambda be, q: be.displays_list(),
    "/api/hardware": lambda be, q: be.hardware_check(),
    "/api/polyaccount": lambda be, q: be.poly_account_status(),
    "/api/polyaccount/countries": lambda be, q: be.poly_account_countries(),
    "/api/polyaccount/backups": lambda be, q: be.poly_account_backups(),
    "/api/polyaccount/backup": lambda be, q: be.poly_account_backup(_q(q, "id") or ""),
    "/api/updates": lambda be, q: be.updates_status(),
    "/api/security": lambda be, q: be.security_status(),
    "/api/widgets/data": lambda be, q: be.widgets.data(),
    "/api/widgets/weather": lambda be, q: be.widgets.weather(),
    "/api/widgets/geocode": lambda be, q: {"results": be.widgets.geocode(_q(q, "q") or "")},
    "/api/widgets/news": lambda be, q: be.widgets.news(_q(q, "topic")),
    "/api/widgets/photos": lambda be, q: {"photos": be.widgets.photos()},
    "/api/widgets/media": lambda be, q: be.widgets.media(),
}


def _password(body: dict) -> str:
    value = body.get("password", "")
    if not isinstance(value, str) or len(value) > 1024:
        raise ApiError("'password' must be a string")
    return value


def _obj(body: dict, key: str) -> dict:
    value = body.get(key)
    if not isinstance(value, dict):
        raise ApiError(f"'{key}' must be an object")
    return value


def _str_list_any(body: dict, key: str) -> list[str]:
    """A list of short names that may be empty (e.g. turning every choice off)."""
    value = body.get(key)
    if not isinstance(value, list) or len(value) > 20 or not all(isinstance(v, str) and 0 < len(v) <= 60 for v in value):
        raise ApiError(f"'{key}' must be a list of names")
    return value


def _names(body: dict, key: str) -> list[str]:
    value = body.get(key)
    if not isinstance(value, list) or not value or len(value) > 40 or \
            not all(isinstance(v, str) and 0 < len(v) <= 100 for v in value):
        raise ApiError(f"'{key}' must be a list of names")
    return value

POST_API = {
    "/api/launch": lambda be, b: be.launch(_str(b, "id")),
    "/api/window": lambda be, b: be.window_action(
        _int(b, "xid"), _choice(b, "action", ("activate", "minimize", "close", "toggle", "kill", "fullscreen"))),
    "/api/volume": lambda be, b: be.set_volume(
        level=_opt_int(b, "level"), delta=_opt_int(b, "delta"),
        muted=_opt_bool(b, "muted"), toggle_mute=bool(_opt_bool(b, "toggleMute"))),
    "/api/brightness": lambda be, b: be.set_brightness(level=_opt_int(b, "level"), delta=_opt_int(b, "delta")),
    "/api/wifi/connect": lambda be, b: be.wifi_connect(_str(b, "ssid", 64), _opt_str(b, "password")),
    "/api/wifi/forget": lambda be, b: be.wifi_forget(_str(b, "ssid", 64)),
    "/api/wifi/enabled": lambda be, b: be.wifi_enable(bool(_opt_bool(b, "enabled"))),
    "/api/power": lambda be, b: be.power(_choice(b, "action", POWER_ACTIONS)),
    "/api/settings": lambda be, b: be.update_settings(b),
    "/api/popup": lambda be, b: be.popup_request(
        b.get("view"), anchor_x=_opt_int(b, "anchorX"), data=b.get("data"), height=_opt_int(b, "height"),
        toggle_any=bool(_opt_bool(b, "toggle"))),
    "/api/popup/closed": lambda be, b: be.popup_closed(),
    "/api/open": lambda be, b: be.open_app(_choice(b, "app", OPEN_APPS), _opt_str(b, "page")),
    "/api/run": lambda be, b: be.run_default(_choice(b, "what", RUN_TARGETS)),
    "/api/run-command": lambda be, b: be.run_command(_str(b, "command", 1024)),
    "/api/pick-wallpaper": lambda be, b: be.pick_wallpaper(),
    "/api/files/mkdir": lambda be, b: be.files_mkdir(_str(b, "parent", 4096), _str(b, "name", 255)),
    "/api/files/new-file": lambda be, b: be.files_new_file(_str(b, "parent", 4096), _str(b, "name", 255)),
    "/api/files/rename": lambda be, b: be.files_rename(_str(b, "path", 4096), _str(b, "name", 255)),
    "/api/files/copy": lambda be, b: be.files_transfer(_str_list(b, "sources"), _str(b, "dest", 4096), move=False),
    "/api/files/move": lambda be, b: be.files_transfer(_str_list(b, "sources"), _str(b, "dest", 4096), move=True),
    "/api/files/trash": lambda be, b: be.files_trash(_str_list(b, "paths")),
    "/api/files/restore": lambda be, b: be.files_restore(_str_list(b, "names")),
    "/api/files/empty-trash": lambda be, b: be.files_empty_trash(),
    "/api/files/open": lambda be, b: be.open_path(_str(b, "path", 4096)),
    "/api/background-apps/end": lambda be, b: be.end_background_app(_str(b, "app", 300)),
    "/api/jobs/cancel": lambda be, b: be.jobs.cancel(_str(b, "id", 20)),
    "/api/displays/mode": lambda be, b: be.display_mode_set(_str(b, "mode", 20)),
    "/api/power/windows": lambda be, b: be.restart_to_windows(),
    "/api/drivers/firmware": lambda be, b: be.firmware_install(),
    "/api/account/pin": lambda be, b: be.pin_set(b.get("password") if isinstance(b.get("password"), str) else "", _str(b, "pin", 6)),
    "/api/account/pin/remove": lambda be, b: be.pin_set(b.get("password") if isinstance(b.get("password"), str) else "", None),
    "/api/storage/clean": lambda be, b: be.storage_clean(_choice(b, "what", ("thumbnails", "trash", "packages"))),
    "/api/power/keys": lambda be, b: be.power_keys_set(_str(b, "lid", 20), _str(b, "lidPlugged", 20), _str(b, "button", 20)),
    "/api/apps/usage/clear": lambda be, b: be.usage_clear(),
    "/api/files/terminal": lambda be, b: be.terminal_at(_str(b, "path", 4096)),
    "/api/shell/restart": lambda be, b: be.restart_shell(),
    "/api/setup/done": lambda be, b: be.finish_setup(),
    "/api/vara/chat": lambda be, b: be.vara.chat(be, _str(b, "message", 4000)),
    "/api/vara/reset": lambda be, b: be.vara.reset(),
    "/api/vara/config": lambda be, b: be.vara.config.update(
        _opt_str(b, "endpoint"), _opt_str(b, "model"), b.get("apiKey") if isinstance(b.get("apiKey"), str) else None,
        _opt_str(b, "workspace"), _opt_str(b, "approval"), _opt_str(b, "provider")),
    "/api/vara/approve": lambda be, b: be.vara.approve(_str(b, "id", 80), _choice(b, "decision", ("allow", "always", "deny"))),
    "/api/vara/stop": lambda be, b: be.vara.stop(),
    "/api/vara/forget": lambda be, b: {"memory": be.vara.memory.forget(_opt_int(b, "index"))},
    "/api/vara/test": lambda be, b: be.vara_test(),
    "/api/greeter/login": lambda be, b: be.greeter_login(_str(b, "user", 64), _password(b), _opt_str(b, "session")),
    "/api/greeter/power": lambda be, b: be.greeter_power(_choice(b, "action", ("shutdown", "restart", "suspend"))),
    "/api/admin/auth": lambda be, b: be.admin_auth(_password(b)),
    "/api/install/start": lambda be, b: be.install_start(_obj(b, "plan")),
    "/api/install/disk": lambda be, b: be.install_disk(_choice(b, "action", ("delete", "new")), _str(b, "disk", 64),
                                                       _opt_int(b, "number"), _opt_int(b, "start"), _opt_int(b, "size")),
    "/api/drivers/install": lambda be, b: be.drivers_install(_names(b, "packages")),
    "/api/store/install": lambda be, b: be.store_action(_str(b, "id", 60), "install"),
    "/api/store/remove": lambda be, b: be.store_action(_str(b, "id", 60), "remove"),
    "/api/store/open": lambda be, b: be.store_open(_str(b, "id", 60)),
    "/api/procs/end": lambda be, b: be.procs_end(_int(b, "pid"), bool(_opt_bool(b, "force"))),
    "/api/widgets/data": lambda be, b: be.widgets_update(b),
    "/api/widgets/media": lambda be, b: be.widgets.media_action(_str(b, "action", 20)),
    "/api/install/restart": lambda be, b: be.install_restart(),
    "/api/account/password": lambda be, b: be.account_password(_password({"password": b.get("current", "")}), _str(b, "password", 256)),
    "/api/account/recovery-key": lambda be, b: be.account_recovery_key(),
    "/api/lock": lambda be, b: be.lock(),
    "/api/packs/install": lambda be, b: be.pack_install(_str(b, "pack", 30), _names(b, "apps")),
    "/api/gaming/cloud": lambda be, b: be.cloud_gaming_set(_str_list_any(b, "services")),
    "/api/apps/uninstall": lambda be, b: be.app_uninstall(_str(b, "id", 130)),
    "/api/apps/startup": lambda be, b: be.startup_set(_str(b, "id", 130), bool(_opt_bool(b, "enabled"))),
    "/api/apps/startup/add": lambda be, b: be.startup_add(_str(b, "id", 130)),
    "/api/apps/startup/remove": lambda be, b: be.startup_remove(_str(b, "id", 130)),
    "/api/polyaccount/link": lambda be, b: be.poly_account_link_start(),
    "/api/polyaccount/link/cancel": lambda be, b: be.poly_account_link_cancel(),
    "/api/polyaccount/signin": lambda be, b: be.poly_account_signin(_str(b, "email", 254), _password(b)),
    "/api/polyaccount/register": lambda be, b: be.poly_account_register({
        "name": _str(b, "name", 80), "email": _str(b, "email", 254), "password": _password(b), "country": _str(b, "country", 60),
        "acceptTerms": b.get("acceptTerms") is True}),
    "/api/polyaccount/settings": lambda be, b: be.poly_account_set(_opt_bool(b, "sync"), _opt_bool(b, "remoteManagement")),
    "/api/polyaccount/disconnect": lambda be, b: be.poly_account_disconnect(),
    "/api/updates/install": lambda be, b: be.updates_install(_choice(b, "what", ("system",))),
    "/api/updates/action": lambda be, b: be.updates_action(_choice(b, "kind", ("check", "tonight", "now"))),
    "/api/updates/policy": lambda be, b: be.updates_policy(_obj(b, "policy")),
    "/api/hardware": lambda be, b: be.hardware_profile(_choice(b, "profile", ("full", "balanced", "light"))),
    "/api/sound/device": lambda be, b: be.sound_set_device(_choice(b, "kind", ("output", "input")), _str(b, "name", 300)),
    "/api/sound/input": lambda be, b: be.sound_set_input(_opt_int(b, "level"), _opt_bool(b, "muted")),
    "/api/sound/mixer": lambda be, b: be.sound_mixer(_choice(b, "tab", MIXER_TABS)),
    "/api/displays": lambda be, b: be.displays_set(
        _str(b, "name", 64), _opt_str(b, "size"), _opt_rate(b), _opt_str(b, "rotation"), bool(_opt_bool(b, "primary"))),
    "/api/security": lambda be, b: be.security_set(_choice(b, "what", ("firewall", "updates")), bool(_opt_bool(b, "on"))),
    "/api/dev": lambda be, b: be.dev_action(_choice(b, "action", ("folder", "source", "reset"))),
    "/api/lock/unlock": lambda be, b: be.lock_unlock(_password(b)),
    "/api/lock/recover": lambda be, b: be.lock_recover(_str(b, "key", 64), _str(b, "password", 256)),
    "/api/greeter/recover": lambda be, b: be.greeter_recover(_str(b, "user", 64), _str(b, "key", 64), _str(b, "password", 256)),
}


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Server:
    def __init__(self, backend, ui_dir: Path, token: str, dev: bool = False, port: int = 0,
                 allow: set[str] | None = None, ctl_token: str | None = None):
        self.backend = backend
        self.ctl_token = ctl_token  # polyos-ctl's: CTL_API only
        self.allow = allow  # when set, the only protected paths this server answers (the login screen)
        self.ui_dir = Path(ui_dir).resolve()
        self.token = token
        self.dev = dev
        self.stopping = threading.Event()
        self.httpd = _HTTPServer(("127.0.0.1", port), _Handler)
        self.httpd.app = self
        self.port = self.httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def start(self) -> None:
        threading.Thread(target=self.httpd.serve_forever, name="polyos-http", daemon=True).start()
        log.info("UI server on %s", self.base_url)

    def stop(self) -> None:
        self.stopping.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    def authorized(self, *candidates: str | None) -> bool:
        return self.role(*candidates) == "ui"

    def role(self, *candidates: str | None) -> str | None:
        """"ui" for the interface's token, "ctl" for polyos-ctl's, None otherwise."""
        for c in candidates:
            if c and hmac.compare_digest(c.encode(), self.token.encode()):
                return "ui"
            if c and self.ctl_token and hmac.compare_digest(c.encode(), self.ctl_token.encode()):
                return "ctl"
        return None


class _Handler(BaseHTTPRequestHandler):
    server_version = "PolyOS"
    sys_version = ""

    def log_message(self, fmt, *args):
        log.debug("%s " + fmt, self.address_string(), *args)

    @property
    def app(self) -> Server:
        return self.server.app

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method: str) -> None:
        try:
            if self.headers.get("Host", "") not in self.app.allowed_hosts:
                raise ApiError("forbidden", 403)
            url = urlsplit(self.path)
            path = unquote(url.path)
            if path.startswith(("/api/", "/icon/", "/wallpaper/", "/files/")):
                query_token = parse_qs(url.query).get("t", [None])[0]
                role = self.app.role(self.headers.get("X-PolyOS-Token"), query_token)
                if role is None:
                    raise ApiError("unauthorized", 401)
                self._ctl = role == "ctl"
                if self._ctl and (method, path) not in CTL_API:
                    raise ApiError("polyos-ctl can't do that", 403)
                if self.app.allow is not None and path not in self.app.allow:
                    raise ApiError("not found", 404)
                if method == "POST":
                    return self._post_api(path)
                return self._get_protected(path)
            if method != "GET":
                raise ApiError("method not allowed", 405)
            return self._static(path)
        except ApiError as exc:
            self._json({"error": str(exc)}, exc.status)
        except RuntimeError as exc:  # system errors carry user-facing messages
            self._json({"error": str(exc)}, 500)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("%s %s failed", method, self.path)
            self._json({"error": "Internal error"}, 500)

    # ---- routes ----------------------------------------------------------------------
    def _get_protected(self, path: str) -> None:
        be = self.app.backend
        query = parse_qs(urlsplit(self.path).query)
        if path == "/api/events":
            return self._events()
        if path in GET_API:
            return self._json(GET_API[path](be, query))
        if path == "/files/raw":
            return self._file(be.file_raw(_q(query, "path") or ""), "no-cache")
        if path.startswith("/icon/app/"):
            data, ctype = be.app_icon(path[len("/icon/app/"):])
            return self._bytes(data, ctype, "max-age=600")
        if path.startswith("/icon/theme/"):
            data, ctype = be.theme_icon(path[len("/icon/theme/"):])
            return self._bytes(data, ctype, "max-age=600")
        if path.startswith("/icon/window/"):
            try:
                xid = int(path[len("/icon/window/"):])
            except ValueError:
                raise ApiError("bad window id") from None
            data, ctype = be.window_icon(xid)
            return self._bytes(data, ctype, "no-cache")
        if path == "/wallpaper/current":
            return self._file(be.wallpaper_path(), "no-cache")
        if path == "/wallpaper/lock":
            return self._file(be.wallpaper_path("lockWallpaper"), "no-cache")
        if path.startswith("/wallpaper/builtin/"):
            return self._file(be.builtin_wallpaper(path[len("/wallpaper/builtin/"):]), "max-age=3600")
        raise ApiError("not found", 404)

    def _post_api(self, path: str) -> None:
        if path == "/api/camera/save":
            return self._camera_save()
        handler = POST_API.get(path)
        if handler is None:
            raise ApiError("not found", 404)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError("request too large", 413)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise ApiError("invalid JSON") from None
        if not isinstance(body, dict):
            raise ApiError("expected a JSON object")
        if getattr(self, "_ctl", False) and path == "/api/settings" and set(body) - CTL_SETTINGS:
            raise ApiError("polyos-ctl can't change that setting", 403)
        result = handler(self.app.backend, body)
        self._json({"ok": True} if result is None else result)

    def _camera_save(self) -> None:
        """Raw photo/video bytes from the Camera app (too big for the JSON API's limit)."""
        from .backend import CAMERA_MAX_BYTES

        kind = _q(parse_qs(urlsplit(self.path).query), "kind")
        if kind not in CAMERA_MAX_BYTES:
            raise ApiError("'kind' must be photo or video")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError("bad length") from None
        if length <= 0:
            raise ApiError("nothing was captured")
        if length > CAMERA_MAX_BYTES[kind]:
            raise ApiError("That recording is too large to save.", 413)
        data = self.rfile.read(length)
        self._json(self.app.backend.camera_save(kind, self.headers.get("Content-Type", ""), data))

    def _events(self) -> None:
        bus = self.app.backend.bus
        q = bus.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while not self.app.stopping.is_set() and bus.is_subscribed(q):
                try:
                    chunk = b"data: " + q.get(timeout=15) + b"\n\n"
                except queue.Empty:
                    chunk = b": ping\n\n"  # also detects closed connections
                self.wfile.write(chunk)
                self.wfile.flush()
        except OSError:
            pass
        finally:
            bus.unsubscribe(q)

    def _static(self, path: str) -> None:
        if path == "/":
            path = "/dev.html" if self.app.dev else "/index.html"
        rel = path.lstrip("/")
        target = (self.app.ui_dir / rel).resolve()
        if (rel in DEV_ONLY and not self.app.dev) or not target.is_relative_to(self.app.ui_dir) \
                or not target.is_file():
            raise ApiError("not found", 404)
        # developer mode: the person's own copy of this file, from ~/.config/polyos/ui
        override = getattr(self.app.backend, "ui_override", None)
        if override is not None and self.app.allow is None:
            target = override(rel) or target
        data = target.read_bytes()
        headers = {}
        if target.suffix == ".html":
            if self.app.dev:
                boot = f"<script>window.POLYOS={json.dumps({'token': self.app.token})};</script>"
                data = data.replace(b"<!--POLYOS-BOOT-->", boot.encode())
            headers["Content-Security-Policy"] = CSP.format(extra=" 'unsafe-inline'" if self.app.dev else "")
        ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._bytes(data, ctype, "no-cache", headers)

    # ---- responses ---------------------------------------------------------------------
    def _file(self, path: Path | None, cache: str) -> None:
        if path is None or not path.is_file():
            raise ApiError("not found", 404)
        ctype = IMAGE_TYPES.get(path.suffix.lower())
        if ctype is None:
            raise ApiError("unsupported file type", 415)
        self._bytes(path.read_bytes(), ctype, cache)

    def _bytes(self, data: bytes, ctype: str, cache: str, headers: dict | None = None, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, status: int = 200) -> None:
        try:
            self._bytes(json.dumps(obj).encode(), "application/json", "no-store", status=status)
        except (BrokenPipeError, ConnectionResetError):
            pass
