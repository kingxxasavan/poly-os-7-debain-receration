"""polyos-admin: the only part of PolyOS that runs as root.

    polyos-admin probe                     disks and systems for the installer (JSON)
    polyos-admin install PLAN [--dry-run]  install PolyOS (PLAN: JSON file from the setup UI, deleted after reading)
    polyos-admin store install|remove ID   an app from the PolyMarket catalog
    polyos-admin drivers PACKAGE...        driver packages (names must match drivers.DRIVER_PACKAGE_RE)
    polyos-admin account FILE              for the sudo user: {"password"} and/or {"recoveryKey"} (file deleted)
    polyos-admin reboot                    restart right away (after installing, from the live USB)
    polyos-admin first-start               after installing: the drivers and edition apps setup chose (a timer runs it)
    polyos-admin app remove DESKTOP_ID     Settings > Apps: uninstall the Debian package or Flatpak behind an app
    polyos-admin disk delete DISK NUMBER   the installer's drive screen: delete a partition (live USB only)
    polyos-admin disk new DISK START BYTES     ... or make one in unallocated space (START in sectors)
    polyos-admin pack NAME ID...           an edition's apps (gaming, developer), only ids in its catalog pack
    polyos-admin security firewall|updates on|off   the firewall (ufw) and automatic security updates
    polyos-admin update polyos|system      online updates: the newest PolyOS release, or Debian's updates
    polyos-admin auto-update timer|check|tonight|now   the update service (systemd runs these)
    polyos-admin update-policy JSON        Settings > Updates: automatic checks, downloads, installs, time, channel
    polyos-admin power-keys LID PLUGGED BUTTON   what closing the lid (on battery / plugged in) and the power button do
    polyos-admin clean-packages            Settings > Storage: delete apt's downloaded packages
    polyos-admin restart-windows           start Windows next time only (grub-reboot), then restart
    polyos-admin firmware                  install the firmware updates the computer's maker offers (fwupd, LVFS)

Every command prints JSON lines: {"progress": 0..1, "message": "..."} while it works,
{"result": ...} for data, and {"error": "..."} (exit status 1) when it fails.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import drivers, installer, recovery, store

APT_ENV = {"DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
APT_OPTS = ["-o", "APT::Status-Fd=1", "-o", "Dpkg::Use-Pty=0", "-o", "DPkg::Lock::Timeout=180",
            "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"]


class AdminError(Exception):
    pass


def emit(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def parse_apt_status(line: str) -> tuple[float, str] | None:
    """APT::Status-Fd lines: dlstatus:<n>:<percent>:<text> (download) / pmstatus:<pkg>:<percent>:<text> (install)."""
    parts = line.split(":", 3)
    if len(parts) < 4 or parts[0] not in ("dlstatus", "pmstatus"):
        return None
    try:
        pct = float(parts[2])
    except ValueError:
        return None
    if parts[0] == "dlstatus":
        return 0.1 + 0.4 * pct / 100, "Downloading…"
    return 0.5 + 0.48 * pct / 100, re.sub(r"^(Installing|Preparing|Unpacking|Configuring)\s+", r"\1 ", parts[3]).strip()[:120]


_HOST = re.compile(r"(?:Could not resolve|Temporary failure resolving) '([^']+)'|Failed to fetch https?://([^/\s]+)")
_BROKEN = re.compile(r"dpkg: error processing (?:package|archive) (\S+?)(?::\w+)? \(")
UPDATE_LOG = Path("/var/log/polyos-update.log")


def broken_packages(text: str) -> list[str]:
    """The packages dpkg couldn't set up, from apt's output."""
    return sorted({m.group(1) for m in _BROKEN.finditer(text)})


def _apt_error(tail: list[str]) -> str:
    """A readable message from apt's last lines, naming the server or the real error."""
    text = "\n".join(tail)
    if "Could not get lock" in text or "Unable to acquire the dpkg" in text:
        return "Another installation is running. Try again in a few minutes."
    if "No space left" in text:
        return "There isn't enough free disk space."
    if "Unable to locate package" in text or "has no installation candidate" in text:
        return "That package isn't available from Debian right now. Try again later."
    if "not valid yet" in text or "is not valid yet" in text:
        return "The computer's clock is wrong, so Debian's servers were refused. Fix the date and time, then try again."
    host = _HOST.search(text)
    if host and ("Temporary failure resolving" in text or "Could not resolve" in text or "Could not connect" in text
                 or "Connection timed out" in text or "Network is unreachable" in text):
        return (f"Couldn't reach {host.group(1) or host.group(2)}. Check your internet connection and try again.")
    broken = broken_packages(text)
    if broken:  # the real problem, not "Sub-process /usr/bin/dpkg returned an error code (1)"
        why = next((ln.strip() for ln in tail if "returned error exit status" in ln or "trying to overwrite" in ln), "")
        return (f"{', '.join(broken)} couldn't finish setting up, so the installation stopped."
                + (f" ({why})" if why else "") + f" Details: {UPDATE_LOG}")
    errors = [ln[2:].strip() for ln in tail if ln.startswith("E:")]
    return errors[-1] if errors else "The installation didn't finish."


def _have_package_lists() -> bool:
    lists = Path("/var/lib/apt/lists")
    return lists.is_dir() and any(lists.glob("*_Packages*"))


def _log(lines: list[str]) -> None:
    """apt's full output for the last failed installation, for diagnosis (/var/log/polyos-update.log)."""
    try:
        UPDATE_LOG.write_text("\n".join(lines[-400:]) + "\n", "utf-8")
    except OSError:
        pass


def apt(args: list[str], start: float = 0.0) -> None:
    tail: list[str] = []
    everything: list[str] = []
    proc = subprocess.Popen(["apt-get", "-y", *APT_OPTS, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env={**os.environ, **APT_ENV}, bufsize=1)
    for line in proc.stdout:
        line = line.rstrip()
        everything = (everything + [line])[-400:]
        status = parse_apt_status(line)
        if status:
            emit({"progress": max(start, status[0]), "message": status[1]})
        elif line:
            tail = (tail + [line])[-60:]
    if proc.wait() != 0:
        _log(["$ apt-get " + " ".join(args), *everything])
        raise AdminError(_apt_error(tail))


def half_installed() -> list[str]:
    """Packages dpkg left unfinished (unpacked, half-configured, waiting on triggers)."""
    out = subprocess.run(["dpkg-query", "-W", "-f", "${db:Status-Abbrev} ${Package}\n"], capture_output=True, text=True,
                         timeout=60).stdout
    return sorted(line.split()[1] for line in out.splitlines()
                  if len(line.split()) == 2 and line[:2] in ("iU", "iF", "iH", "iW", "iT"))


def repair_packages() -> list[str]:
    """Finish what an earlier installation left half done (a crash, a power cut, a package whose setup
    failed), so it doesn't stop this one. Returns what's still broken."""
    env = {**os.environ, **APT_ENV}
    if half_installed():
        emit({"log": "finishing half-installed packages: " + ", ".join(half_installed())})
        subprocess.run(["dpkg", "--configure", "-a", "--force-confdef", "--force-confold"], capture_output=True,
                       text=True, env=env, timeout=1800)
    subprocess.run(["apt-get", "-y", *APT_OPTS, "-f", "install"], capture_output=True, text=True, env=env, timeout=1800)
    return half_installed()


def apt_update() -> None:
    """Refresh apt's package lists. A single failed source (a mirror hiccup, deb-src, a 404) isn't fatal:
    the install that follows says what's really missing. Only stop when nothing could be downloaded."""
    emit({"progress": 0.02, "message": "Checking Debian for the latest versions…"})
    proc = subprocess.run(["apt-get", "-q", "update", "-o", "DPkg::Lock::Timeout=180"], capture_output=True, text=True,
                          env={**os.environ, **APT_ENV}, timeout=600)
    lines = (proc.stdout + proc.stderr).splitlines()
    if proc.returncode == 0 and not any(ln.startswith(("E:", "Err:")) for ln in lines):
        return
    emit({"log": "apt-get update: " + " | ".join(ln for ln in lines if ln.startswith(("E:", "W:", "Err:")))[:2000]})
    if not _have_package_lists():
        raise AdminError(_apt_error(lines[-60:]))


_PERCENT = re.compile(r"(\d{1,3})%")


def flatpak(args: list[str]) -> None:
    if not shutil.which("flatpak"):
        raise AdminError("Flatpak isn't installed, so Flathub apps can't be added.")
    tail: list[str] = []
    proc = subprocess.Popen(["flatpak", *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env={**os.environ, "LC_ALL": "C"}, bufsize=1)
    for line in proc.stdout:  # universal newlines split flatpak's \r progress updates
        line = line.strip()
        if not line:
            continue
        m = _PERCENT.search(line)
        if m:
            emit({"progress": 0.1 + 0.88 * min(100, int(m.group(1))) / 100, "message": line[:120]})
        tail = (tail + [line])[-40:]
    if proc.wait() != 0:
        text = "\n".join(tail)
        if "Could not resolve" in text or "Unable to connect" in text or "Couldn't connect" in text:
            raise AdminError("Couldn't reach Flathub. Check your internet connection and try again.")
        if "No space left" in text:
            raise AdminError("There isn't enough free disk space.")
        if "No remote refs found" in text or "not available for" in text.lower():
            raise AdminError("Flathub doesn't have that app for this computer's processor.")
        errs = [ln for ln in tail if "error" in ln.lower()]
        raise AdminError(errs[-1] if errs else "Flathub couldn't install that app.")


def store_action(action: str, app_id: str) -> None:
    apps = store.validate(store.load())
    app = apps.get(app_id)
    if app is None:
        raise AdminError("That app isn't in PolyMarket.")
    if action == "install" and not store.available(app):
        raise AdminError(f"{app['name']} isn't made for this computer's processor (ARM).")
    if action == "install":
        if app["source"] == "debian":
            apt_update()
            apt(["install", *app["packages"]], start=0.1)
            if "docker.io" in app["packages"]:
                join_group("docker")
        else:
            emit({"progress": 0.03, "message": "Connecting to Flathub…"})
            flatpak(["remote-add", "--if-not-exists", "--system", "flathub", store.FLATHUB_URL])
            flatpak(["install", "--system", "-y", "--noninteractive", "flathub", app["ref"]])
    elif action == "remove":
        if app.get("system"):
            raise AdminError(f"{app['name']} is part of PolyOS and can't be removed.")
        if app["source"] == "debian":
            apt(["remove", *app["packages"]])
            apt(["autoremove"], start=0.9)
        else:
            flatpak(["uninstall", "--system", "-y", "--noninteractive", app["ref"]])
    else:
        raise AdminError("Unknown store action.")
    emit({"progress": 1.0, "message": "Done."})


GAMING_SYSCTL = Path("/etc/sysctl.d/80-polyos-gaming.conf")
# SteamOS's value: some Windows games (through Proton) crash with Debian's default
GAMING_SYSCTL_TEXT = "# PolyOS Gaming: memory maps many games need (SteamOS uses the same value)\nvm.max_map_count = 2147483642\n"


def has_candidate(package: str) -> bool:
    """True when apt can install this package from the configured sources."""
    out = subprocess.run(["apt-cache", "policy", package], capture_output=True, text=True, timeout=60,
                         env={**os.environ, **APT_ENV}).stdout
    m = re.search(r"Candidate:\s*(\S+)", out)
    return bool(m) and m.group(1) != "(none)"


def app_remove(desktop_id: str) -> None:
    """Uninstall whatever put this app's launcher on the system; never a part of PolyOS."""
    path = next((Path(d) / desktop_id for d in store.LAUNCHER_DIRS
                 if store.DESKTOP_ID.match(desktop_id) and (Path(d) / desktop_id).is_file()), None)
    if path is None:
        raise AdminError("That app isn't installed for everyone, or it's already gone.")
    if "/flatpak/exports/" in str(path):
        emit({"progress": 0.2, "message": "Removing the app…"})
        flatpak(["uninstall", "--system", "-y", "--noninteractive", desktop_id[:-len(".desktop")]])
        emit({"progress": 1.0, "message": "Removed."})
        return
    owners = store.parse_dpkg_search(subprocess.run(["dpkg", "-S", str(path)], capture_output=True, text=True, timeout=30).stdout)
    package = owners.get(str(path))
    if not package:
        raise AdminError("PolyOS couldn't tell which package this app came from.")
    sim = subprocess.run(["apt-get", "-s", "remove", package], capture_output=True, text=True, timeout=120,
                         env={**os.environ, **APT_ENV})
    removed = [line.split()[1] for line in sim.stdout.splitlines() if line.startswith("Remv ")]
    if any(p.startswith("polyos") for p in removed) or sim.returncode != 0:
        raise AdminError("This app is part of PolyOS, so it can't be removed.")
    emit({"progress": 0.1, "message": "Removing the app…"})
    apt(["remove", package], start=0.1)
    apt(["autoremove"], start=0.9)
    emit({"progress": 1.0, "message": "Removed."})


def join_group(group: str) -> None:
    """Add the person who asked (sudo's SUDO_USER) to a group, e.g. docker, so it works without sudo."""
    user = os.environ.get("SUDO_USER", "")
    if user and user != "root" and re.match(r"^[a-z_][a-z0-9_-]{0,31}$", user):
        subprocess.run(["usermod", "-aG", group, user], check=False, capture_output=True, timeout=30)


def pack_install(name: str, ids: list[str]) -> None:
    """Install an edition's apps: every id must be in that catalog pack (the UI can't add others)."""
    data = store.load()
    apps = store.validate(data)
    pack = store.pack(data, name)
    if pack is None:
        raise AdminError("That edition doesn't exist.")
    allowed = {aid for aid, _default in pack["apps"]}
    bad = [i for i in ids if i not in allowed]
    if bad or not ids:
        raise AdminError(f"Not part of {pack['name']}: {', '.join(bad) or '(nothing chosen)'}")
    chosen = [apps[i] for i in dict.fromkeys(ids)]
    wrong = [a["name"] for a in chosen if not store.available(a)]
    if wrong:
        raise AdminError(f"Not made for this computer's processor: {', '.join(wrong)}")
    debs = [p for a in chosen if a["source"] == "debian" for p in a["packages"]]
    refs = [a["ref"] for a in chosen if a["source"] == "flathub"]
    skipped: list[str] = []
    if debs:
        apt_update()
        available = [p for p in debs if has_candidate(p)]
        skipped = [p for p in debs if p not in available]
        if available:
            apt(["install", *available], start=0.1)
        if "docker.io" in available:
            join_group("docker")
    if refs:
        emit({"progress": 0.4, "message": "Connecting to Flathub…"})
        flatpak(["remote-add", "--if-not-exists", "--system", "flathub", store.FLATHUB_URL])
        for n, ref in enumerate(refs):
            emit({"progress": 0.4 + 0.55 * n / len(refs), "message": f"Installing {ref.rsplit('.', 1)[-1]} ({n + 1} of {len(refs)})…"})
            flatpak(["install", "--system", "-y", "--noninteractive", "flathub", ref])
    if name == "gaming":
        GAMING_SYSCTL.write_text(GAMING_SYSCTL_TEXT)
        subprocess.run(["sysctl", "-q", "-p", str(GAMING_SYSCTL)], check=False, capture_output=True, timeout=30)
    if skipped:
        emit({"log": f"not available from Debian here: {', '.join(skipped)}"})
    emit({"progress": 1.0, "message": f"{pack['name']} is ready." + (f" Skipped (not in this Debian): {', '.join(skipped)}." if skipped else "")})


def security(what: str, state: str) -> None:
    """Turn the firewall (ufw: nothing gets in, everything goes out) or automatic security updates on or off."""
    from . import security as sec

    if state not in ("on", "off"):
        raise AdminError("Say on or off.")
    on = state == "on"
    if what == "firewall":
        if not shutil.which("ufw"):
            if not on:
                return emit({"progress": 1.0, "message": "The firewall is off."})
            apt_update()
            apt(["install", "ufw"], start=0.1)
        steps = [["ufw", "default", "deny", "incoming"], ["ufw", "default", "allow", "outgoing"], ["ufw", "--force", "enable"]] \
            if on else [["ufw", "disable"]]
        for args in steps:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                raise AdminError((proc.stderr or proc.stdout).strip().splitlines()[-1] if (proc.stderr or proc.stdout).strip()
                                 else "The firewall couldn't be changed.")
        emit({"progress": 1.0, "message": f"The firewall is {state}."})
    elif what == "updates":
        if on and not Path("/usr/bin/unattended-upgrade").exists():
            apt_update()
            apt(["install", "unattended-upgrades"], start=0.1)
        sec.AUTO_UPGRADES.write_text(sec.AUTO_UPGRADES_TEXT.format(on=1 if on else 0))
        emit({"progress": 1.0, "message": f"Automatic security updates are {state}."})
    else:
        raise AdminError("Unknown security setting.")


def drivers_install(packages: list[str]) -> None:
    bad = [p for p in packages if not drivers.DRIVER_PACKAGE_RE.match(p)]
    if bad or not packages:
        raise AdminError(f"Not a driver package: {', '.join(bad) or '(none)'}")
    apt_update()
    # A package this Debian doesn't have (names change between releases) is skipped, not fatal.
    policy = subprocess.run(["apt-cache", "policy", *packages], capture_output=True, text=True, timeout=120).stdout
    available = drivers.parse_apt_policy(policy)
    skipped = [p for p in packages if p not in available]
    packages = [p for p in packages if p in available]
    if skipped:
        emit({"log": f"not available in this Debian, skipped: {' '.join(skipped)}"})
    if not packages:
        raise AdminError("None of these drivers are available from Debian right now. Check your internet connection.")
    apt(["install", *packages], start=0.1)
    if any(p.startswith(("nvidia", "broadcom-sta", "linux-headers-", "libcamera", "firmware-misc")) for p in packages):
        emit({"restart": True, "message": "Restart to start using the new driver."})
    emit({"progress": 1.0, "message": "Drivers installed."})


def account(path: Path) -> None:
    """Change the invoking person's password and/or recovery key (they unlocked sudo with their password)."""
    user = os.environ.get("SUDO_USER", "")
    if not user or user == "root":
        raise AdminError("Run this from your own account.")
    try:
        request = json.loads(path.read_text("utf-8"))
    finally:
        path.unlink(missing_ok=True)
    password = request.get("password")
    key = request.get("recoveryKey")
    if "pin" in request:  # Settings > Account > PIN: a new PIN, or None to remove it
        from . import pin as pins
        new_pin = request["pin"]
        if new_pin is not None and not pins.valid(new_pin):
            raise AdminError("A PIN is 4 to 6 digits.")
        pins.save(user, pins.make_record(new_pin) if new_pin is not None else None)
    if password is not None:
        if not isinstance(password, str) or not password or len(password) > 256 or "\n" in password:
            raise AdminError("Choose a new password.")
        proc = subprocess.run(["chpasswd"], input=f"{user}:{password}\n", capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise AdminError("The password couldn't be changed.")
    if key is not None:
        if not recovery.looks_valid(str(key)):
            raise AdminError("That recovery key is damaged.")
        recovery.save_record(user, recovery.make_record(str(key)))
    emit({"progress": 1.0, "message": "Saved."})


def update(what: str) -> None:
    """Online updates: "polyos" installs the newest signed PolyOS release now (the update service);
    "system" installs Debian's updates for everything else."""
    if what == "polyos":
        auto_update("now")
    elif what == "system":
        apt_update()
        repair_packages()
        apt(["full-upgrade"], start=0.1)
        emit({"progress": 1.0, "message": "Everything is up to date."})
    else:
        raise AdminError("Choose PolyOS or system updates.")


def auto_update(mode: str) -> None:
    """The update service's runs (from systemd: polyos-update*.service; see autoupdate.py)."""
    from . import autoupdate
    service = autoupdate.Service(emit, install=lambda files: autoupdate.apt_install(files, apt, repair_packages),
                                 apt_update=apt_update)
    try:
        status = service.run(mode)
    except ValueError as exc:
        raise AdminError(str(exc)) from None
    except OSError as exc:
        raise AdminError(f"Couldn't download the update ({exc}). Check your internet connection.") from None
    message = {"check": "Checked for updates.", "tonight": f"PolyOS {status.get('latest', '')} installs tonight.",
               "now": f"PolyOS {status.get('installed', '')} is installed."}.get(mode, "Done.")
    emit({"progress": 1.0, "message": message, "result": {k: status.get(k) for k in ("latest", "available", "installed", "downloaded")}})


def update_policy(raw: str) -> None:
    """Settings > Updates: when to check, download and install, the time and the channel."""
    from . import autoupdate
    try:
        policy = autoupdate.save_policy(json.loads(raw))
    except (ValueError, TypeError) as exc:
        raise AdminError(f"Those update settings aren't valid: {exc}") from None
    emit({"progress": 1.0, "message": "Saved.", "result": policy})


def reboot() -> None:
    """Restart now, without waiting on the live system's services (the install is synced and unmounted).

    A normal shutdown of the live system reads from the USB drive, so it can hang if the drive is
    pulled out. --force twice restarts straight away; the kernel's own restart is the fallback.
    """
    if not installer.LIVE_MEDIUM.exists():
        raise AdminError("This only works from the PolyOS USB drive.")
    os.sync()
    emit({"progress": 1.0, "message": "Restarting…"})
    try:
        subprocess.run(["systemctl", "reboot", "--force", "--force"], check=False, capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:  # still here: restart through the kernel directly (everything is already written)
        Path("/proc/sysrq-trigger").write_text("b")
    except OSError:
        pass


def power_keys(lid: str, plugged: str, button: str) -> None:
    """Closing the lid and pressing the power button, through logind (it reloads its settings on SIGHUP)."""
    from . import power as pw
    try:
        text = pw.logind_text(lid, plugged, button)
    except ValueError as exc:
        raise AdminError(str(exc)) from None
    pw.LOGIND_CONF.parent.mkdir(parents=True, exist_ok=True)
    pw.LOGIND_CONF.write_text(text)
    subprocess.run(["systemctl", "kill", "-s", "HUP", "systemd-logind"], capture_output=True, timeout=30)
    emit({"progress": 1.0, "message": "Saved."})


def windows_entry(grub_cfg: str) -> str | None:
    """The boot menu's Windows entry (os-prober's), by its title."""
    m = re.search(r"^menuentry\s+'([^']*Windows[^']*)'", grub_cfg, re.M)
    return m.group(1) if m else None


def restart_windows() -> None:
    try:
        entry = windows_entry(Path("/boot/grub/grub.cfg").read_text("utf-8", errors="replace"))
    except OSError:
        entry = None
    if not entry:
        raise AdminError("Windows isn't in the boot menu.")
    proc = subprocess.run(["grub-reboot", entry], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AdminError("Couldn't choose Windows for the next start.")
    emit({"progress": 1.0, "message": "Restarting into Windows…"})
    subprocess.run(["systemctl", "reboot"], capture_output=True, timeout=60)


def firmware() -> None:
    """Firmware updates from the computer's maker (BIOS/UEFI, SSD, docks...), through fwupd and LVFS.
    Most apply while the computer restarts."""
    if not shutil.which("fwupdmgr"):
        apt_update()
        apt(["install", "fwupd"], start=0.05)
    emit({"progress": 0.2, "message": "Getting the newest firmware list from your computer's maker…"})
    subprocess.run(["fwupdmgr", "refresh", "--force", "--assume-yes"], capture_output=True, text=True, timeout=300)
    emit({"progress": 0.35, "message": "Installing firmware updates… Keep the computer plugged in."})
    proc = subprocess.run(["fwupdmgr", "update", "--assume-yes", "--no-reboot-check", "--no-unreported-check"],
                          capture_output=True, text=True, timeout=3600)
    text = (proc.stdout + proc.stderr).strip()
    if proc.returncode not in (0, 2):  # 2: nothing to do
        raise AdminError(text.splitlines()[-1] if text else "The firmware update didn't finish.")
    emit({"progress": 1.0, "message": "Firmware updated. Restart to finish." if proc.returncode == 0 else "No firmware updates.",
          "restart": proc.returncode == 0})


def clean_packages() -> None:
    before = sum(f.stat().st_size for f in Path("/var/cache/apt/archives").glob("*.deb"))
    subprocess.run(["apt-get", "clean"], capture_output=True, timeout=300)
    emit({"progress": 1.0, "message": f"Freed {before // (1024 * 1024)} MB.", "result": {"freed": before}})


def first_start() -> None:
    """After installing: the recommended drivers and the edition's apps, once online (firststart.py)."""
    import socket

    from . import firststart

    def online() -> bool:
        for host in ("deb.debian.org", "dl.flathub.org"):
            try:
                socket.create_connection((host, 443), timeout=8).close()
                return True
            except OSError:
                continue
        return False

    def install_pack(name: str) -> None:
        data = store.load()
        apps = store.validate(data)
        pack = store.pack(data, name)
        if pack is None:
            return
        ids = [aid for aid, default in pack["apps"] if default and aid in apps and store.available(apps[aid])]
        if ids:
            pack_install(name, ids)

    firststart.run(emit, drivers_install, install_pack, online)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        emit({"error": "polyos-admin must run as root (through sudo)."})
        return 1
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "probe":
            emit({"result": installer.probe(emit)})
        elif cmd == "install":
            if not rest:
                raise AdminError("No install plan given.")
            plan_path = Path(rest[0])
            try:
                plan = json.loads(plan_path.read_text("utf-8"))
            finally:
                plan_path.unlink(missing_ok=True)  # it holds the new account's password
            installer.Installer(installer.validate_plan(plan), emit, dry_run="--dry-run" in rest).run()
        elif cmd == "store" and len(rest) == 2:
            store_action(rest[0], rest[1])
        elif cmd == "drivers":
            drivers_install(rest)
        elif cmd == "account" and len(rest) == 1:
            account(Path(rest[0]))
        elif cmd == "reboot":
            reboot()
        elif cmd == "first-start":
            first_start()
        elif cmd == "app" and len(rest) == 2 and rest[0] == "remove":
            app_remove(rest[1])
        elif cmd == "disk" and rest[:1] in (["delete"], ["new"]):
            if not installer.LIVE_MEDIUM.exists():
                raise AdminError("Drives can only be changed from the PolyOS USB drive.")
            try:
                numbers = [int(x) for x in rest[2:]]
            except ValueError:
                raise AdminError("Choose a partition or unallocated space.") from None
            if rest[0] == "delete" and len(numbers) == 1:
                installer.delete_partition(rest[1], numbers[0], emit)
            elif rest[0] == "new" and len(numbers) == 2:
                installer.create_partition(rest[1], numbers[0], numbers[1], emit)
            else:
                raise AdminError(f"Unknown command: {' '.join(argv)}")
            emit({"result": {"ok": True}})
        elif cmd == "pack" and len(rest) >= 2:
            pack_install(rest[0], rest[1:])
        elif cmd == "update" and len(rest) == 1:
            update(rest[0])
        elif cmd == "auto-update" and len(rest) == 1 and rest[0] in ("timer", "check", "tonight", "now"):
            auto_update(rest[0])
        elif cmd == "update-policy" and len(rest) == 1:
            update_policy(rest[0])
        elif cmd == "power-keys" and len(rest) == 3:
            power_keys(*rest)
        elif cmd == "clean-packages":
            clean_packages()
        elif cmd == "restart-windows":
            restart_windows()
        elif cmd == "firmware":
            firmware()
        elif cmd == "security" and len(rest) == 2:
            security(rest[0], rest[1])
        else:
            raise AdminError(f"Unknown command: {' '.join(argv)}")
        return 0
    except (AdminError, installer.InstallError, store.CatalogError) as exc:
        emit({"error": str(exc)})
        return 1
    except subprocess.TimeoutExpired:
        emit({"error": "That took too long and was stopped."})
        return 1


if __name__ == "__main__":
    sys.exit(main())
