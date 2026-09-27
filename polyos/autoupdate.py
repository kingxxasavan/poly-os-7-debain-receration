"""The PolyOS update service: checks, downloads and installs PolyOS updates on its own schedule.

It runs as root from systemd (polyos-update.timer, hourly), through polyos-admin:

    polyos-admin auto-update timer     the hourly run: check, download, install when it's time
    polyos-admin auto-update check     check now
    polyos-admin auto-update tonight   install the waiting update at the preferred time
    polyos-admin auto-update now       download (if needed) and install right away

Settings > Updates writes the policy with `polyos-admin update-policy JSON` (the administrator
password) and starts the check/tonight/now units without one (data/polkit/50-polyos-update.rules
allows only those, for the person at the computer). The service writes its state to STATUS,
which everyone can read; the shell shows it and sends the "ready" notification.

Installs are only ever signed PolyOS releases (updates.py checks the signature and checksums).
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import time
from pathlib import Path

from . import __version__, updates

POLICY_PATH = Path("/etc/polyos/update.json")
STATE_DIR = Path("/var/lib/polyos/update")
STATUS_PATH = STATE_DIR / "status.json"
CHECK_EVERY = 6 * 3600          # seconds between automatic checks
INSTALL_WINDOW = 3 * 3600       # an update due at 2:00 may install until 5:00
DEFAULT_POLICY = {"channel": "stable", "autoCheck": True, "autoDownload": True, "autoInstall": False,
                  "time": "02:00", "askRestart": True}


def validate_policy(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("expected settings")
    out = dict(DEFAULT_POLICY)
    for key in ("autoCheck", "autoDownload", "autoInstall", "askRestart"):
        if key in raw:
            if not isinstance(raw[key], bool):
                raise ValueError(f"{key} must be on or off")
            out[key] = raw[key]
    if "channel" in raw:
        if raw["channel"] not in updates.CHANNELS:
            raise ValueError("Choose Stable, Beta or Developer.")
        out["channel"] = raw["channel"]
    if "time" in raw:
        t = str(raw["time"])
        if len(t) != 5 or t[2] != ":" or not t[:2].isdigit() or not t[3:].isdigit() or int(t[:2]) > 23 or int(t[3:]) > 59:
            raise ValueError("Choose a time like 02:00.")
        out["time"] = t
    return out


def load_policy(path: Path = POLICY_PATH) -> dict:
    try:
        return validate_policy(json.loads(path.read_text("utf-8")))
    except (OSError, ValueError):
        return dict(DEFAULT_POLICY)


def save_policy(policy: dict, path: Path = POLICY_PATH) -> dict:
    clean = validate_policy(policy)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=2) + "\n", "utf-8")
    tmp.chmod(0o644)
    os.replace(tmp, path)
    return clean


def load_status(path: Path = STATUS_PATH) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_status(status: dict, path: Path = STATUS_PATH) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o755)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2) + "\n", "utf-8")
    tmp.chmod(0o644)
    os.replace(tmp, path)
    return status


def in_window(policy: dict, now: datetime.datetime | None = None) -> bool:
    """True from the preferred time until three hours after it (across midnight too)."""
    now = now or datetime.datetime.now()
    hour, minute = (int(x) for x in policy["time"].split(":"))
    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if start > now:
        start -= datetime.timedelta(days=1)
    return (now - start).total_seconds() < INSTALL_WINDOW


class Service:
    """One run of the update service. `install` and `apt_update` are what polyos-admin passes in."""

    def __init__(self, emit=lambda _e: None, install=None, apt_update=None, policy=None, status_path: Path = STATUS_PATH,
                 cache: Path = updates.CACHE, get=updates._get, clock=time.time, now=None):
        self.emit = emit
        self.install_files = install
        self.apt_update = apt_update or (lambda: None)
        self.policy = policy or load_policy()
        self.status_path = status_path
        self.cache = cache
        self.get = get
        self.clock = clock
        self.now = now
        self.status = load_status(status_path)

    def save(self, **changes) -> dict:
        self.status.update(changes, running=__version__, policy=self.policy)
        return _save_status(self.status, self.status_path)

    def check(self) -> dict:
        self.save(state="checking", error=None)
        try:
            found = updates.check(self.policy["channel"], get=self.get)
        except (OSError, ValueError) as exc:
            return self.save(state="idle", error=f"Couldn’t check for updates: {exc}", lastCheck=self.clock())
        latest = found["latest"]
        return self.save(state="idle", lastCheck=self.clock(), latest=latest, available=found["available"], notes=found.get("notes", ""),
                         size=found.get("size", 0), reason=found.get("reason"),
                         downloaded=self.status.get("downloaded") if self.status.get("downloaded") == latest else None)

    def download(self) -> list[Path]:
        self.save(state="downloading", error=None)
        try:
            version, files = updates.download(self.emit, self.policy["channel"], get=self.get, cache=self.cache)
        except (OSError, ValueError) as exc:
            self.save(state="idle", error=str(exc))
            raise
        self.save(state="idle", downloaded=version)
        return files

    def install(self) -> dict:
        files = self.download()
        self.save(state="installing")
        self.emit({"progress": 0.5, "message": f"Installing PolyOS {self.status.get('downloaded')}…"})
        try:
            self.apt_update()
            self.install_files(files)
        except Exception as exc:  # noqa: BLE001 - reported to the person, then raised
            self.save(state="idle", error=f"The update didn’t install: {exc}")
            raise
        version = self.status.get("downloaded")
        return self.save(state="idle", installed=version, installedAt=self.clock(), available=False, tonight=False, downloaded=None,
                         error=None)

    # ---- the modes ------------------------------------------------------------------------------
    def run(self, mode: str) -> dict:
        if mode == "check":
            return self.check()
        if mode == "tonight":
            self.save(tonight=True)
            return self.status
        if mode == "now":
            if not self.status.get("available") or updates.version_tuple(self.status.get("latest", "")) <= updates.version_tuple(__version__):
                self.check()
            if not self.status.get("available"):
                raise ValueError(self.status.get("error") or self.status.get("reason") or "PolyOS is already up to date.")
            return self.install()
        if mode != "timer":
            raise ValueError(f"unknown mode: {mode}")
        policy = self.policy
        due = self.clock() - float(self.status.get("lastCheck") or 0) >= CHECK_EVERY
        if policy["autoCheck"] and due:
            self.check()
        if not self.status.get("available"):
            return self.status
        if policy["autoDownload"] and self.status.get("downloaded") != self.status.get("latest"):
            try:
                self.download()
            except (OSError, ValueError):
                return self.status
        wants = policy["autoInstall"] or self.status.get("tonight")
        if wants and in_window(policy, self.now):
            return self.install()
        return self.status


def apt_install(files: list[Path], apt, repair=None) -> None:
    """Install the update's packages. Something another installation left half done is finished first;
    if the install still fails, repair once more and retry, then say which package is the problem."""
    args = ["install", *[str(f) for f in files]]
    if repair is None:
        apt(args, start=0.5)
        return
    repair()
    try:
        apt(args, start=0.5)
        return
    except Exception:  # noqa: BLE001 - one repair and retry, then the real reason
        still = repair()
        try:
            apt(args, start=0.5)
            return
        except Exception as exc:
            ours = {f.name.split("_", 1)[0] for f in files}
            others = [p for p in still if p not in ours]
            if others:
                raise ValueError(f"The update is ready, but {', '.join(others)} is only half installed and stops every "
                                   f"installation. Remove it in Settings › Apps (or: sudo apt-get remove {others[0]}), "
                                   "then install the update again.") from None
            raise exc


def start_unit(kind: str) -> None:
    """From the signed-in person's session: start polyos-update-{check,tonight,now}.service (polkit allows it)."""
    if kind not in ("check", "tonight", "now"):
        raise ValueError(kind)
    subprocess.run(["systemctl", "start", "--no-block", f"polyos-update-{kind}.service"], check=True, capture_output=True, timeout=20)
