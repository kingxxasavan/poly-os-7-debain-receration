"""After installing: what the setup on the USB drive chose that needs the internet, done on its own.

Setup asks everything before PolyOS installs, so the installed computer starts straight to the
desktop. Two things need a download: the recommended drivers the hardware check found, and the
edition's apps (Gaming or Developer). The installer writes them to PLAN_PATH, and
polyos-first-start.timer runs `polyos-admin first-start` as root every few minutes after starting
up until they're installed; offline, it just tries again later. STATUS_PATH (readable by
everyone) is what the desktop shows as notifications.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

PLAN_PATH = Path("/var/lib/polyos/first-start.json")
STATUS_PATH = Path("/var/lib/polyos/first-start-status.json")
PACKS = ("gaming", "developer")


def clean_plan(drivers: list, pack: str | None, driver_re) -> dict:
    """Only driver package names the driver check allows, and a known edition pack."""
    names = [d for d in dict.fromkeys(drivers or []) if isinstance(d, str) and driver_re.match(d)][:40]
    return {"drivers": names, "pack": pack if pack in PACKS else None}


def pending(plan: dict) -> bool:
    return bool(plan.get("drivers") or plan.get("pack"))


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    tmp.chmod(mode)
    os.replace(tmp, path)


def write_plan(plan: dict, root: Path) -> None:
    """For the installer: the plan inside the new system (root is where it's mounted)."""
    _write(root / PLAN_PATH.relative_to("/"), plan, 0o644)


def run(emit, install_drivers, install_pack, online, plan_path: Path = PLAN_PATH, status_path: Path = STATUS_PATH,
        clock=time.time) -> dict:
    """One try. install_drivers(names) and install_pack(name) raise on failure; online() -> bool."""
    plan = load(plan_path)
    status = load(status_path)
    if not pending(plan):
        plan_path.unlink(missing_ok=True)
        return status
    what = [x for x in (("drivers" if plan.get("drivers") else None), plan.get("pack")) if x]
    status.update(state="waiting", what=what, pack=plan.get("pack"), error=None, updated=clock())
    if not online():
        status["message"] = "Waiting for the internet to finish setting up."
        _write(status_path, status, 0o644)
        return status
    status.update(state="running", message="Finishing setting up PolyOS…")
    _write(status_path, status, 0o644)
    errors = []
    if plan.get("drivers"):
        try:
            install_drivers(plan["drivers"])
            plan["drivers"] = []
            status["drivers"] = "done"
        except Exception as exc:  # noqa: BLE001 - reported, tried again next time
            errors.append(f"Drivers: {exc}")
    if plan.get("pack"):
        try:
            install_pack(plan["pack"])
            status["packDone"] = plan["pack"]
            plan["pack"] = None
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Apps: {exc}")
    attempts = int(status.get("attempts") or 0) + 1
    if pending(plan) and attempts < 5:
        _write(plan_path, plan, 0o644)
    else:
        plan_path.unlink(missing_ok=True)  # done, or given up after five tries (Settings can still do it)
    status.update(state="done" if not errors else ("failed" if not pending(plan) or attempts >= 5 else "waiting"),
                  error="; ".join(errors) or None, attempts=attempts, updated=clock(),
                  message="Done." if not errors else "Some things didn’t install. PolyOS tries again later.")
    _write(status_path, status, 0o644)
    emit({"progress": 1.0, "message": status["message"], "result": status})
    return status
