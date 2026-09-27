"""Poly Account on this computer: optional, and PolyOS works the same without it.

Connecting gives this computer its own credential (never the account password): by signing in
once, creating an account, or a 6-digit code entered on the website. It lives in
~/.config/polyos/poly-account.json (readable by you only). With it, PolyOS checks in every 15
minutes: its version and update settings, its hardware too if the account's device information
isn't Minimal, and picks up actions sent from the website. Restart, lock and install updates run
only while Remote management is on here. Poly Sync keeps chosen settings the same on every
computer on the account. With sync on, each computer also keeps a backup of itself (its settings,
edition and PolyMarket apps; no files or passwords), so a new computer can be set up the same way.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__, paths, updates

CHECKIN_MINUTES = 15
TIMEOUT = 20

# Poly Sync: which PolyOS settings travel with the account, by the website's sync categories.
SYNC_KEYS = {
    "themes": ("theme", "accent", "glass"),
    "wallpapers": ("wallpaper", "lockWallpaper"),
    "settings": ("clock24h", "showSeconds", "desktopClock", "taskbarStyle", "taskbarAlign", "taskbarAutoHide", "taskbarWidgets",
                 "taskbarDate", "desktopOpen", "widgets"),
    "apps": ("pinned", "desktopIcons"),
    "accessibility": ("scale",),
}


# The backup of this computer that setting up another one can copy ("Set up like one of your
# computers"). Settings that belong to the hardware (effects, screens, performance) stay behind.
BACKUP_SETTINGS = ("theme", "accent", "glass", "wallpaper", "lockWallpaper", "clock24h", "showSeconds", "desktopClock",
                   "desktopIcons", "desktopOpen", "pinned", "startPinned", "widgets", "taskbarStyle", "taskbarAlign",
                   "taskbarAutoHide", "taskbarWidgets", "taskbarDate", "scale", "nightLight", "powerMode", "screenOff",
                   "sleepAfter", "lockOnSleep", "lockNews", "keepRecent", "cloudGaming", "gameMode", "displayMode")
BACKUP_EVERY = 6 * 3600  # at most this often, and only when something changed
EDITIONS = ("regular", "developer", "gaming")
APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MAX_BACKUP_APPS = 120


class AccountError(Exception):
    def __init__(self, message: str, status: int = 0, data: dict | None = None):
        super().__init__(message)
        self.status = status
        self.data = data or {}


def state_path(home: Path | None = None) -> Path:
    return (paths.config_dir() if home is None else home / ".config" / "polyos") / "poly-account.json"


def load(home: Path | None = None) -> dict:
    try:
        data = json.loads(state_path(home).read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


_save_lock = threading.Lock()


def _write(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def save(state: dict, home: Path | None = None) -> dict:
    """Written whole and private (0600); check-ins and settings run in threads, so one at a time."""
    with _save_lock:
        _write(state_path(home), state)
    return state


def merge(credential: str, changes: dict, home: Path | None = None) -> dict | None:
    """After a request: record what came back, unless the computer was disconnected meanwhile."""
    path = state_path(home)
    with _save_lock:
        current = load(home)
        if not credential or current.get("credential") != credential:
            return None
        current.update(changes)
        _write(path, current)
        return current


def forget(home: Path | None = None) -> None:
    with _save_lock:
        state_path(home).unlink(missing_ok=True)


def http(method: str, path: str, body: dict | None = None, credential: str | None = None) -> dict:
    """One request to the Poly Account API; AccountError with the server's own message on failure."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{updates.server()}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json", "User-Agent": f"PolyOS/{__version__}"})
    if credential:
        req.add_header("Authorization", f"Bearer {credential}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 - the Poly server (https)
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        raise AccountError(payload.get("error") or f"Poly Account answered {exc.code}.", exc.code, payload) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise AccountError(f"Couldn’t reach Poly Account ({getattr(exc, 'reason', exc)}). Check your internet connection.") from None


def device_info(hardware: dict | None = None) -> dict:
    """What the website shows about this computer (hardware only when the account allows it)."""
    info = {"hostname": socket.gethostname()[:60]}
    hw = hardware or {}
    computer = hw.get("computer") or {}
    for key, value in (("maker", computer.get("maker")), ("model", computer.get("model")), ("year", computer.get("year")),
                       ("cpu", hw.get("cpu")), ("cores", hw.get("cores")), ("ram", hw.get("ram"))):
        if value:
            info[key] = value
    if computer.get("laptop"):
        info["kind"] = "laptop"
    if hw.get("graphics"):
        info["gpu"] = hw["graphics"][0].get("name", "")[:120]
    try:
        usage = shutil.disk_usage("/")
        info["storage"], info["storageFree"] = usage.total, usage.free
    except OSError:
        pass
    info["secureBoot"] = secure_boot()
    return info


def secure_boot() -> bool:
    try:
        return any(p.read_bytes()[-1:] == b"\x01" for p in Path("/sys/firmware/efi/efivars").glob("SecureBoot-*"))
    except OSError:
        return False


def arch() -> str:
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())


# ---- connecting ---------------------------------------------------------------------------------
def _connected(state_home, result: dict, extra: dict | None = None) -> dict:
    state = {"credential": result["credential"], "account": result["account"], "device": result["device"],
             "sync": True, "remoteManagement": False, "telemetry": "minimal", **(extra or {})}
    return save(state, state_home)


def start_link(name: str, info: dict) -> dict:
    return http("POST", "/api/v1/device/start", {"name": name, "version": __version__, "arch": arch(), "info": info})


def poll_link(device_code: str, home: Path | None = None) -> dict:
    r = http("POST", "/api/v1/device/token", {"deviceCode": device_code})
    if r.get("status") == "approved":
        _connected(home, r)
    return r


def sign_in(email: str, password: str, name: str, info: dict, home: Path | None = None) -> dict:
    r = http("POST", "/api/v1/device/signin", {"email": email, "password": password, "name": name, "info": info,
                                                "version": __version__, "arch": arch()})
    return _connected(home, r)


def register(fields: dict, name: str, info: dict, home: Path | None = None) -> tuple[dict, str]:
    body = {k: fields.get(k) for k in ("name", "email", "password", "country")}
    r = http("POST", "/api/v1/device/register", {**body, "acceptTerms": fields.get("acceptTerms") is True, "deviceName": name,
                                                  "info": info, "version": __version__, "arch": arch()})
    return _connected(home, r), r.get("recoveryKey", "")


def disconnect(home: Path | None = None) -> None:
    state = load(home)
    if state.get("credential"):
        try:
            http("POST", "/api/v1/device/disconnect", {}, state["credential"])
        except AccountError:
            pass  # removed on the website already, or offline: forget it here anyway
    forget(home)


# ---- while connected --------------------------------------------------------------------------------
def checkin(state: dict, policy: dict, hardware: dict | None, home: Path | None = None) -> dict:
    """Report in and get waiting actions. A computer removed on the website forgets its credential."""
    payload = {"version": __version__, "arch": arch(), "channel": policy.get("channel", "stable"),
               "remoteManagement": bool(state.get("remoteManagement")),
               "policy": {k: policy.get(k) for k in ("autoDownload", "autoInstall", "askRestart", "time")}}
    if state.get("telemetry") in ("standard", "diagnostic"):
        payload["info"] = device_info(hardware)
    try:
        r = http("POST", "/api/v1/device/checkin", payload, state["credential"])
    except AccountError as exc:
        if exc.status == 401:
            forget(home)
            raise AccountError("This computer was removed from its Poly Account.", 401, {"removed": True}) from None
        raise
    changes = {"account": r.get("account", state.get("account")), "device": r.get("device", state.get("device")),
               "telemetry": r.get("telemetry", state.get("telemetry", "minimal")), "syncPrefs": r.get("sync", {}).get("prefs", {}),
               "remoteRevision": r.get("sync", {}).get("revision"), "terms": r.get("terms")}
    state.update(changes)
    merge(state["credential"], changes, home)
    return r


def report(state: dict, command_id: str, status: str, detail: str = "") -> None:
    try:
        http("POST", f"/api/v1/device/commands/{command_id}", {"status": status, "detail": detail[:300]}, state["credential"])
    except AccountError:
        pass


def synced_keys(state: dict) -> list[str]:
    """The settings Poly Sync carries for this account, if sync is on here."""
    if not state.get("sync"):
        return []
    prefs = state.get("syncPrefs") or {k: True for k in SYNC_KEYS}
    return [key for cat, keys in SYNC_KEYS.items() if prefs.get(cat, True) for key in keys]


def shareable(key: str, value) -> bool:
    """Only built-in wallpapers travel: a picture from this computer's disk wouldn't exist elsewhere."""
    return not (key in ("wallpaper", "lockWallpaper") and not str(value).startswith("builtin:"))


def push(state: dict, settings: dict, keys: list[str]) -> int:
    items = {f"settings.{k}": settings[k] for k in keys if k in settings and shareable(k, settings[k])}
    if not items:
        return 0
    http("PUT", "/api/v1/sync", {"items": items}, state["credential"])
    return len(items)


def pull(state: dict, keys: list[str]) -> dict:
    """{setting: value} from the account for the synced keys."""
    items = http("GET", "/api/v1/sync", None, state["credential"]).get("items", {})
    wanted = set(keys)
    return {k[len("settings."):]: v["value"] for k, v in items.items()
            if k.startswith("settings.") and k[len("settings."):] in wanted}


# ---- backups: "Set up like one of your computers" ----------------------------------------------

def make_backup(settings: dict, apps: list[str]) -> dict:
    """This computer's backup: its settings (built-in wallpapers only), edition and PolyMarket apps."""
    return {"polyos": __version__, "edition": settings.get("edition") if settings.get("edition") in EDITIONS else "regular",
            "settings": {k: settings[k] for k in BACKUP_SETTINGS if k in settings and shareable(k, settings[k])},
            "apps": sorted({a for a in apps if isinstance(a, str) and APP_ID_RE.match(a)})[:MAX_BACKUP_APPS]}


def clean_backup(data) -> dict | None:
    """A backup from the account, made safe to apply: known settings that pass their checks, a known
    edition and plausible app ids (what they install is decided by PolyMarket's catalog)."""
    if not isinstance(data, dict):
        return None
    from .core import VALIDATORS
    settings = {}
    raw = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    for key in BACKUP_SETTINGS:
        if key in raw and key in VALIDATORS and shareable(key, raw[key]):
            try:
                settings[key] = VALIDATORS[key](raw[key])
            except (ValueError, TypeError):
                continue
    apps = [a for a in dict.fromkeys(data.get("apps") or []) if isinstance(a, str) and APP_ID_RE.match(a)][:MAX_BACKUP_APPS]
    edition = data.get("edition") if data.get("edition") in EDITIONS else "regular"
    return {"edition": edition, "settings": settings, "apps": apps}


def backup_due(state: dict, backup: dict, now: float) -> bool:
    """Send the backup when it changed, and not more than every few hours."""
    if not state.get("credential") or not state.get("sync"):
        return False
    digest = hashlib.sha256(json.dumps(backup, sort_keys=True).encode()).hexdigest()
    if digest == state.get("backupHash"):
        return False
    return now - float(state.get("backupAt") or 0) >= BACKUP_EVERY or not state.get("backupHash")


def push_backup(state: dict, backup: dict, now: float, home: Path | None = None) -> None:
    http("PUT", "/api/v1/backup", {"backup": backup}, state["credential"])
    digest = hashlib.sha256(json.dumps(backup, sort_keys=True).encode()).hexdigest()
    state.update(backupHash=digest, backupAt=now)
    merge(state["credential"], {"backupHash": digest, "backupAt": now}, home)


def list_backups(state: dict) -> list[dict]:
    """The account's other computers that have a backup, newest first."""
    return http("GET", "/api/v1/backups", None, state["credential"]).get("backups", [])


def get_backup(state: dict, device_id: str) -> dict:
    if not re.match(r"^[0-9a-fA-F-]{36}$", device_id or ""):
        raise AccountError("That backup isn’t in your account.", 404)
    r = http("GET", f"/api/v1/backups/{device_id}", None, state["credential"])
    return {"id": r.get("id"), "name": r.get("name"), "updatedAt": r.get("updatedAt"), "backup": clean_backup(r.get("backup"))}

