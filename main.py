#!/usr/bin/env python3
"""PolyOS project tool.

  python main.py dev          run the UI in a browser with a simulated system (any OS)
  python main.py test         run the unit tests
  python main.py check        static checks (Python, JS syntax, XML, line endings)
  python main.py deb          build polyos-shell and polyos-desktop .deb packages into dist/
  sudo python3 main.py install      build + install on Debian and make PolyOS the default session
  sudo python3 main.py uninstall    remove the PolyOS packages
  sudo python3 main.py iso          build a bootable live ISO with an installer (Debian host)
  python3 main.py nested      run the real session in a Xephyr window (Debian desktop)
  python main.py clean        remove build/ and dist/

Only the standard library is needed; runtime dependencies come from Debian's apt.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
sys.path.insert(0, str(ROOT))

from polyos import __version__ as VERSION  # noqa: E402
from polyos import gaming  # noqa: E402
from polyos.arch import debian_arch  # noqa: E402

MAINTAINER = "PolyOS Team <team@polyos.invalid>"  # set a real contact before publishing packages
HOMEPAGE = "https://scratch.mit.edu/users/PolyOS/"

# ---- package definitions -------------------------------------------------------------

PACKAGES = {
    "polyos-shell": {
        "section": "x11",
        "depends": [
            "python3 (>= 3.11)", "python3-gi", "gir1.2-gtk-3.0", "gir1.2-webkit2-4.1 | gir1.2-webkit2-4.0",
            "gir1.2-wnck-3.0", "librsvg2-common", "openbox", "x11-utils", "x11-xserver-utils", "xdg-utils", "sudo",
            "pkexec", "libpam0g", "openssl",
        ],
        "recommends": [
            "picom", "xcape", "wireplumber | pulseaudio-utils", "network-manager", "brightnessctl", "mesa-utils",
            "papirus-icon-theme", "fonts-inter | fonts-noto-core", "lxpolkit | mate-polkit", "pciutils", "flatpak",
            "playerctl", "libxss1", "power-profiles-daemon", "gstreamer1.0-plugins-good",
            "gsettings-desktop-schemas", "libnotify-bin",
        ],
        "summary": "PolyOS desktop shell",
        "description": (
            "The PolyOS desktop: dock, Home Menu, launcher, Files, Settings, Task Manager, Driver\n"
            "Manager, PolyMarket, Ask Vara and the PolyOS login screen, running as an X11 session\n"
            "on the Openbox window manager.\n"
            "The interface is web technology hosted in WebKitGTK; system integration uses\n"
            "NetworkManager, PipeWire and systemd-logind.\n"
            ".\n"
            "Install polyos-desktop for the complete PolyOS experience."
        ),
    },
    "polyos-desktop": {
        "section": "metapackages",
        "depends": [
            "polyos-shell (= {version})", "xorg", "xserver-xorg-input-libinput", "lightdm", "lightdm-gtk-greeter",
            "gir1.2-lightdm-1", "picom", "pipewire-audio", "wireplumber", "network-manager", "papirus-icon-theme",
            "fonts-inter | fonts-noto-core", "dbus-user-session", "xdg-user-dirs", "adwaita-icon-theme",
            # the browser: Google Chrome (the image adds it on Intel/AMD computers), else Chromium
            "google-chrome-stable | chromium | firefox-esr",
            "gvfs", "xfce4-terminal", "mousepad", "gnome-calculator", "pciutils", "xcape", "systemd-timesyncd", "polkitd",
            # advanced sound on every edition: the full mixer, and pactl for Settings > Sound's device lists
            "pavucontrol", "pulseaudio-utils",
        ],
        "recommends": [
            "brightnessctl", "lxpolkit | mate-polkit", "xfce4-notifyd", "tumbler", "playerctl",
            "xfce4-screenshooter", "fonts-noto-color-emoji", "network-manager-gnome",
            "ristretto", "file-roller", "gvfs-backends", "plymouth", "plymouth-label", "evince",
            "flatpak", "bluez", "blueman", "usbutils", "isenkram-cli", "mokutil", "libxss1", "ufw",
            "unattended-upgrades", "zram-tools", "gamemode", "fwupd",
            "power-profiles-daemon", "gstreamer1.0-plugins-good", "gstreamer1.0-plugins-base", "gstreamer1.0-libav",
        ],
        "summary": "PolyOS desktop environment (complete)",
        "description": (
            "Pulls in everything for a complete PolyOS system: the shell, the X server, the\n"
            "LightDM login screen themed for PolyOS, audio, networking, fonts, icons and a\n"
            "set of default apps (Google Chrome or Chromium, Terminal, Text Editor, Calculator) next\n"
            "to PolyOS Files."
        ),
    },
}

SCRIPTS = {
    "polyos-shell": {
        "postinst": (
            "#!/bin/sh\nset -e\n"
            "if [ \"$1\" = configure ]; then\n"
            "    python3 -m compileall -q /usr/lib/polyos/polyos >/dev/null 2>&1 || true\n"
            "    # the update service: hourly, following Settings > Updates\n"
            "    if [ -d /run/systemd/system ]; then systemctl daemon-reload >/dev/null 2>&1 || true; fi\n"
            "    systemctl enable polyos-update.timer >/dev/null 2>&1 || true\n"
            "    # the boot menu waits for no one now (etc/default/grub.d/50-polyos-menu.cfg), on installed systems\n"
            "    if [ -d /run/systemd/system ] && [ ! -e /run/live/medium ] && [ -f /boot/grub/grub.cfg ] && command -v update-grub >/dev/null; then\n"
            "        update-grub >/dev/null 2>&1 || true\n"
            "    fi\n"
            "    # PIN sign-in for the lock screen (polyos/pin.py)\n"
            "    systemctl enable polyos-pin.socket >/dev/null 2>&1 || true\n"
            "    if [ -d /run/systemd/system ]; then systemctl start polyos-pin.socket >/dev/null 2>&1 || true; fi\n"
            "    # (an installed, running PolyOS, not the ISO build) the USB drive's boot menu looks for this\n"
            "    if [ -d /run/systemd/system ] && [ ! -e /run/live/medium ] && [ ! -e /boot/polyos-installed ]; then\n"
            "        echo PolyOS > /boot/polyos-installed || true\n"
            "    fi\n"
            "    if [ -d /run/systemd/system ]; then systemctl start polyos-update.timer >/dev/null 2>&1 || true; fi\n"
            "fi\n"
        ),
        "prerm": (
            "#!/bin/sh\nset -e\n"
            "if [ \"$1\" = remove ]; then systemctl disable --now polyos-update.timer polyos-pin.socket >/dev/null 2>&1 || true; fi\n"
            "find /usr/lib/polyos -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true\n"
        ),
    },
    "polyos-desktop": {},
}

COPYRIGHT = f"""Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: PolyOS
Source: {HOMEPAGE}
Comment: PolyOS for Debian is based on the design of PolyOS, an operating system
 built in Scratch by AndrewInput and the PIXAPoLY team.

Files: *
Copyright: 2026 The PolyOS Team
License: GPL-3+

Files: usr/share/polyos/ui/img/logo*.svg usr/share/polyos/ui/img/seven.svg
 usr/share/icons/hicolor/scalable/apps/polyos.svg usr/share/polyos/wallpapers/pixapoly.jpg
Copyright: AndrewInput and PIXAPoLY Software (PolyOS for Scratch)
License: CC-BY-SA-2.0
 https://creativecommons.org/licenses/by-sa/2.0/

Files: usr/share/polyos/ui/img/apps/*
Copyright: Papirus Development Team
License: GPL-3
 App icons from the Papirus icon theme (github.com/PapirusDevelopmentTeam/papirus-icon-theme).
 On Debian systems, the complete text of the GNU General Public License
 version 3 can be found in "/usr/share/common-licenses/GPL-3".

Files: usr/share/fonts/truetype/polyos/* usr/share/polyos/ui/fonts/*
Copyright: 2020 The Poppins Project Authors
License: OFL-1.1
 See /usr/share/doc/polyos-shell/Poppins-OFL.txt

License: GPL-3+
 This program is free software: you can redistribute it and/or modify it under
 the terms of the GNU General Public License as published by the Free Software
 Foundation, either version 3 of the License, or (at your option) any later
 version.
 .
 On Debian systems, the complete text of the GNU General Public License
 version 3 can be found in "/usr/share/common-licenses/GPL-3".
"""

TEXT_SUFFIXES = {".script", ".plymouth", ".desc", ".py", ".js", ".css", ".html", ".svg", ".xml", ".conf", ".desktop", ".ini", ".json", ".md", ".rules", ""}
DEV_UI_FILES = {"dev.html", "js/dev.js", "css/dev.css"}


def _tree(src: Path, dest: str, mode: int = 0o644, skip=frozenset()):
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src).as_posix()
        if path.is_file() and "__pycache__" not in path.parts and rel not in skip:
            yield path, f"{dest}/{rel}", mode


def package_files(name: str) -> list[tuple[Path | bytes, str, int]]:
    data = ROOT / "data"
    copyright_file = (COPYRIGHT.encode(), f"usr/share/doc/{name}/copyright", 0o644)
    if name == "polyos-desktop":
        return [
            (data / "lightdm/lightdm.conf.d/50-polyos.conf", "usr/share/lightdm/lightdm.conf.d/50-polyos.conf", 0o644),
            (data / "lightdm/lightdm-gtk-greeter.conf.d/50-polyos.conf",
             "usr/share/lightdm/lightdm-gtk-greeter.conf.d/50-polyos.conf", 0o644),
            *_tree(data / "plymouth/polyos", "usr/share/plymouth/themes/polyos"),
            (data / "xorg/40-polyos-touchpad.conf", "usr/share/X11/xorg.conf.d/40-polyos-touchpad.conf", 0o644),
            # Firefox draws a normal title bar (with PolyOS's close button) instead of tabs in the title bar
            (data / "firefox/policies.json", "etc/firefox/policies/policies.json", 0o644),
            (data / "systemd/50-polyos.conf", "usr/lib/systemd/system.conf.d/50-polyos.conf", 0o644),
            (data / "systemd/journald-polyos.conf", "usr/lib/systemd/journald.conf.d/50-polyos.conf", 0o644),
            copyright_file,
        ]
    files = [
        *[(p, d, m) for p, d, m in _tree(ROOT / "polyos", "usr/lib/polyos/polyos") if p.suffix == ".py"],
        *_tree(ROOT / "ui", "usr/share/polyos/ui", skip=DEV_UI_FILES),
        *[(data / "bin" / b, f"usr/bin/{b}", 0o755) for b in ("polyos-session", "polyos-shell", "polyos-ctl", "polyos-greeter")],
        (data / "bin/polyos-admin", "usr/libexec/polyos/polyos-admin", 0o755),
        (data / "bin/polyos-recover", "usr/libexec/polyos/polyos-recover", 0o755),
        (data / "pam/polyos-lock", "etc/pam.d/polyos-lock", 0o644),
        (data / "pam/polyos-login", "etc/pam.d/polyos-login", 0o644),
        (data / "bin/polyos-pin", "usr/libexec/polyos/polyos-pin", 0o755),
        *[(p, f"usr/lib/systemd/system/{p.name}", 0o644) for p in sorted((data / "systemd").glob("polyos-pin*"))],
        (data / "systemd/polyos-restart-windows.service", "usr/lib/systemd/system/polyos-restart-windows.service", 0o644),
        (data / "xfconf/ristretto.xml", "etc/xdg/xfce4/xfconf/xfce-perchannel-xml/ristretto.xml", 0o644),
        (data / "grub-defaults/50-polyos-menu.cfg", "etc/default/grub.d/50-polyos-menu.cfg", 0o644),
        (data / "polkit/50-polyos-recover.rules", "usr/share/polkit-1/rules.d/50-polyos-recover.rules", 0o644),
        # the update service (polyos/autoupdate.py): an hourly timer, and check/tonight/now on request
        (data / "polkit/50-polyos-update.rules", "usr/share/polkit-1/rules.d/50-polyos-update.rules", 0o644),
        *[(p, f"usr/lib/systemd/system/{p.name}", 0o644) for p in sorted((data / "systemd").glob("polyos-update*"))],
        *[(p, f"usr/lib/systemd/system/{p.name}", 0o644) for p in sorted((data / "systemd").glob("polyos-first-start*"))],
        *_tree(data / "store", "usr/share/polyos/store"),
        *_tree(data / "vara", "usr/share/polyos/vara"),
        (data / "xgreeters/polyos-greeter.desktop", "usr/share/xgreeters/polyos-greeter.desktop", 0o644),
        (data / "xsessions/polyos.desktop", "usr/share/xsessions/polyos.desktop", 0o644),
        *_tree(data / "applications", "usr/share/applications"),
        # cloud gaming: one launcher per service, left out of your apps until switched on in Settings
        *[(gaming.shortcut(cid).encode(), f"usr/share/applications/{gaming.PREFIX}{cid}.desktop", 0o644) for cid in gaming.CLOUD],
        *_tree(data / "openbox", "usr/share/polyos/openbox"),
        *_tree(data / "themes", "usr/share/themes"),
        *_tree(data / "picom", "usr/share/polyos/picom"),
        *_tree(data / "xdg", "usr/share/polyos/xdg"),
        *_tree(data / "wallpapers", "usr/share/polyos/wallpapers"),
        (ROOT / "ui/img/logo.svg", "usr/share/icons/hicolor/scalable/apps/polyos.svg", 0o644),
        (ROOT / "ui/img/settings.svg", "usr/share/icons/hicolor/scalable/apps/polyos-settings.svg", 0o644),
        (ROOT / "ui/img/files.svg", "usr/share/icons/hicolor/scalable/apps/polyos-files.svg", 0o644),
        (ROOT / "ui/img/logo.svg", "usr/share/icons/hicolor/scalable/apps/polyos-setup.svg", 0o644),
        (ROOT / "ui/img/cloud-gaming.svg", "usr/share/icons/hicolor/scalable/apps/polyos-cloud-gaming.svg", 0o644),
        # the boot menu on installed computers (installer.GRUB_THEME)
        (data / "grub/theme.txt", "usr/share/grub/themes/polyos/theme.txt", 0o644),
        (data / "boot/splash.png", "usr/share/grub/themes/polyos/background.png", 0o644),
        *[(p, f"usr/share/grub/themes/polyos/{p.name}", 0o644) for p in sorted((data / "boot").glob("select_*.png"))],
        *[(ROOT / f"ui/img/{n}.svg", f"usr/share/icons/hicolor/scalable/apps/polyos-{n}.svg", 0o644)
          for n in ("taskmgr", "drivers", "store", "camera", "install")],
        # Poppins (SIL OFL 1.1) for window titles and the login screen, not just the web UI
        *[(p, f"usr/share/fonts/truetype/polyos/{p.name}", 0o644) for p in sorted((ROOT / "ui/fonts").glob("*.ttf"))],
        (ROOT / "ui/fonts/OFL.txt", "usr/share/doc/polyos-shell/Poppins-OFL.txt", 0o644),
        copyright_file,
    ]
    return files


# ---- .deb writer (pure Python, identical output on Windows and Linux) ------------------

def _epoch() -> int:
    return int(os.environ.get("SOURCE_DATE_EPOCH", "1767225600"))  # 2026-01-01, reproducible


def _tarinfo(name: str, size: int = 0, mode: int = 0o644, is_dir: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o755 if is_dir else mode
    info.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = _epoch()
    return info


def _tar_xz(entries: list[tuple[str, bytes, int]], dirs: list[str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz", format=tarfile.GNU_FORMAT) as tar:
        for d in dirs:
            tar.addfile(_tarinfo(d, is_dir=True))
        for name, data, mode in entries:
            tar.addfile(_tarinfo(name, len(data), mode), io.BytesIO(data))
    return buf.getvalue()


def _ar(members: list[tuple[str, bytes]]) -> bytes:
    out = io.BytesIO()
    out.write(b"!<arch>\n")
    for name, data in members:
        header = f"{name:<16}{_epoch():<12}{0:<6}{0:<6}{100644:<8}{len(data):<10}`\n".encode()
        assert len(header) == 60
        out.write(header + data + (b"\n" if len(data) % 2 else b""))
    return out.getvalue()


def build_deb(name: str, out_dir: Path) -> Path:
    spec = PACKAGES[name]
    entries: list[tuple[str, bytes, int]] = []
    for src, dest, mode in package_files(name):
        data = src if isinstance(src, bytes) else src.read_bytes()
        if Path(dest).suffix in TEXT_SUFFIXES and b"\0" not in data:
            data = data.replace(b"\r\n", b"\n")  # checkouts on Windows may have CRLF
        entries.append((dest, data, mode))
    entries.sort()

    dirs = {"."}
    for dest, _, _ in entries:
        parts = dest.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("./" + "/".join(parts[:i]))
    dirs_sorted = sorted(dirs)
    data_tar = _tar_xz([(f"./{d}", b, m) for d, b, m in entries], dirs_sorted)

    installed_kb = sum((len(b) + 1023) // 1024 for _, b, _ in entries) + len(dirs_sorted)
    depends = ", ".join(d.format(version=VERSION) for d in spec["depends"])
    control = (
        f"Package: {name}\nVersion: {VERSION}\nArchitecture: all\nMaintainer: {MAINTAINER}\n"
        f"Installed-Size: {installed_kb}\nDepends: {depends}\n"
        + (f"Recommends: {', '.join(spec['recommends'])}\n" if spec["recommends"] else "")
        + f"Section: {spec['section']}\nPriority: optional\nHomepage: {HOMEPAGE}\n"
        f"Description: {spec['summary']}\n"
        + "".join(f" {line}\n" for line in spec["description"].splitlines())
    )
    md5sums = "".join(f"{hashlib.md5(b).hexdigest()}  {d}\n" for d, b, _ in entries)
    control_entries = [("./control", control.encode(), 0o644), ("./md5sums", md5sums.encode(), 0o644)]
    for script, body in SCRIPTS[name].items():
        control_entries.append((f"./{script}", body.encode(), 0o755))
    control_tar = _tar_xz(control_entries, ["."])

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}_{VERSION}_all.deb"
    path.write_bytes(_ar([("debian-binary", b"2.0\n"), ("control.tar.xz", control_tar), ("data.tar.xz", data_tar)]))
    return path


def build_debs(out_dir: Path = DIST) -> list[Path]:
    debs = [build_deb(name, out_dir) for name in PACKAGES]
    for deb in debs:
        shown = deb.relative_to(ROOT) if deb.is_relative_to(ROOT) else deb
        print(f"  built {shown}  ({deb.stat().st_size / 1024:.0f} KiB)")
        if shutil.which("dpkg-deb"):  # validate with the real tool when available
            subprocess.run(["dpkg-deb", "--info", str(deb)], check=True, stdout=subprocess.DEVNULL)
    return debs


# ---- helpers for commands that must run on Debian as root ------------------------------

def require_debian_root(what: str) -> None:
    if not sys.platform.startswith("linux") or not Path("/etc/debian_version").exists():
        sys.exit(f"'{what}' runs on Debian. On other systems use 'python main.py deb' and copy dist/ over.")
    if os.geteuid() != 0:
        sys.exit(f"'{what}' needs root:  sudo python3 main.py {what}")


def sh(*args: str, **kwargs) -> None:
    print("  $", " ".join(args))
    subprocess.run(args, check=True, **kwargs)


def target_user() -> str | None:
    user = os.environ.get("SUDO_USER")
    return user if user and user != "root" else None


def set_default_session(user: str) -> None:
    """Pick PolyOS for this user at the login screen (AccountsService, else ~/.dmrc)."""
    accounts = Path("/var/lib/AccountsService/users")
    if accounts.is_dir():
        path = accounts / user
        cfg = configparser.ConfigParser(interpolation=None)
        cfg.optionxform = str
        if path.exists():
            cfg.read(path)
        if not cfg.has_section("User"):
            cfg.add_section("User")
        cfg["User"]["Session"] = "polyos"
        cfg["User"]["XSession"] = "polyos"
        with open(path, "w") as fh:
            cfg.write(fh, space_around_delimiters=False)
        os.chmod(path, 0o600)
    else:
        import pwd

        pw = pwd.getpwnam(user)
        dmrc = Path(pw.pw_dir) / ".dmrc"
        dmrc.write_text("[Desktop]\nSession=polyos\n")
        os.chown(dmrc, pw.pw_uid, pw.pw_gid)
    print(f"  PolyOS is now the default session for {user}")


def hand_network_to_networkmanager() -> None:
    """Interfaces configured by the Debian installer in /etc/network/interfaces are
    ignored by NetworkManager, which would leave the PolyOS Wi-Fi menu empty."""
    path = Path("/etc/network/interfaces")
    if not path.exists():
        return
    text = path.read_text()
    names = set(re.findall(r"^\s*(?:auto|allow-hotplug|iface)\s+([^\s]+)", text, re.M)) - {"lo"}
    if not names:
        return
    backup = path.with_name("interfaces.polyos-backup")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(
        "# PolyOS: network interfaces are managed by NetworkManager.\n"
        f"# The previous configuration is saved in {backup}\n\n"
        "source /etc/network/interfaces.d/*\n\nauto lo\niface lo inet loopback\n"
    )
    print(f"  NetworkManager will manage {', '.join(sorted(names))} after a reboot "
          f"(old config: {backup}). Wi-Fi passwords from the installer must be re-entered in PolyOS.")


# ---- commands ------------------------------------------------------------------------

def cmd_dev(args) -> None:
    from polyos.core import EventBus, Settings
    from polyos.mock import MockBackend
    from polyos.server import Server

    BUILD.mkdir(exist_ok=True)
    settings_path = BUILD / ("dev-settings-live.json" if args.live else "dev-settings.json")
    if args.live:
        settings_path.unlink(missing_ok=True)  # a live USB starts fresh every boot
    backend = MockBackend(Settings(settings_path), EventBus(), live=args.live, dual_boot=args.dual_boot)
    server = Server(backend, ROOT / "ui", secrets.token_urlsafe(24), dev=True, port=args.port)
    server.start()
    url = f"{server.base_url}/"
    print(f"PolyOS dev UI:  {url}   (Ctrl+C to stop)")
    print(f"  login screen: {server.base_url}/index.html?surface=greeter   (password: polyos)")
    print(f"  first-run:    {server.base_url}/index.html?surface=setup")
    print(f"  Files:        {server.base_url}/index.html?surface=files")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()


def cmd_test(_args) -> None:
    import unittest

    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


def cmd_check(_args) -> None:
    import xml.dom.minidom

    problems = 0
    python_files = [p for p in ROOT.rglob("*.py") if "build" not in p.parts] + list((ROOT / "data/bin").iterdir())
    for path in sorted(python_files):
        try:
            compile(path.read_text("utf-8"), str(path), "exec")
        except SyntaxError as exc:
            print(f"  python: {path.relative_to(ROOT)}: {exc}")
            problems += 1
    node = shutil.which("node")
    for path in sorted((ROOT / "ui/js").rglob("*.js")):
        if node:
            proc = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
            if proc.returncode:
                print(f"  js: {path.relative_to(ROOT)}\n{proc.stderr}")
                problems += 1
    for path in [*ROOT.glob("data/**/*.xml"), *ROOT.glob("data/**/*.svg"), *ROOT.glob("ui/**/*.svg")]:
        try:
            xml.dom.minidom.parse(str(path))
        except Exception as exc:  # noqa: BLE001 - report any parse failure
            print(f"  xml: {path.relative_to(ROOT)}: {exc}")
            problems += 1
    for path in [*ROOT.glob("data/**/*"), *ROOT.glob("iso/**/*")]:
        text_file = path.suffix in TEXT_SUFFIXES | {".xbm", ".cfg", ".chroot"}  # skip images and fonts
        if path.is_file() and text_file and b"\r\n" in path.read_bytes():
            print(f"  crlf: {path.relative_to(ROOT)} (Linux configs need LF line endings)")
            problems += 1
    if not node:
        print("  (node not found: skipped JS syntax checks)")
    print("check: ok" if not problems else f"check: {problems} problem(s)")
    sys.exit(1 if problems else 0)


def cmd_deb(args) -> None:
    print(f"Building PolyOS {VERSION} packages")
    out = Path(args.out).resolve()
    debs = build_debs(out)
    if args.manifest:  # for online updates: the release lists its packages and their checksums
        from polyos import updates
        path = out / updates.MANIFEST
        notes_file = ROOT / "RELEASE_NOTES.md"  # shown in Settings › Updates before installing
        notes = notes_file.read_text("utf-8").strip() if notes_file.exists() else ""
        import polyos
        data = (json.dumps(updates.build_manifest(VERSION, debs, notes, polyos.__editions__), indent=2) + "\n").encode()
        path.write_bytes(data)
        print(f"  wrote {path}")
        key = os.environ.get("POLYOS_UPDATE_KEY", "")
        if key:  # Poly's release key (a GitHub Actions secret): PolyOS only installs signed updates
            signature = updates.sign(data, key)
            if not updates.verify(data, signature):
                sys.exit("The signing key doesn't match the public key in polyos/updates.py (TRUSTED_KEYS).")
            (out / updates.SIGNATURE).write_bytes(signature)
            print(f"  signed {path.name}")
        else:
            print("  not signed (POLYOS_UPDATE_KEY isn't set): PolyOS won't install this release as an online update")


def cmd_install(args) -> None:
    require_debian_root("install")
    print(f"Installing PolyOS {VERSION}")
    debs = build_debs(DIST)
    if args.shell_only:
        debs = [d for d in debs if d.name.startswith("polyos-shell_")]
    # apt's sandbox user must be able to read the files, so stage them in a public temp dir.
    stage = Path(tempfile.mkdtemp(prefix="polyos-"))
    stage.chmod(0o755)
    staged = []
    for deb in debs:
        target = stage / deb.name
        shutil.copy(deb, target)
        target.chmod(0o644)
        staged.append(str(target))
    try:
        sh("apt-get", "update")
        sh("apt-get", "install", "-y", *staged)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    if not args.shell_only and not args.keep_ifupdown:
        hand_network_to_networkmanager()
    user = target_user()
    if user:
        set_default_session(user)
    print("\nDone. Reboot (or run: sudo systemctl restart lightdm) and sign in to PolyOS.")


def cmd_uninstall(_args) -> None:
    require_debian_root("uninstall")
    sh("apt-get", "remove", "-y", "polyos-desktop", "polyos-shell")
    backup = Path("/etc/network/interfaces.polyos-backup")
    if backup.exists():
        print(f"  Your previous network config is still in {backup}; restore it with:\n"
              f"    sudo cp {backup} /etc/network/interfaces")


def cmd_iso(args) -> None:
    require_debian_root("iso")
    if not shutil.which("lb"):
        sys.exit("live-build is not installed:  sudo apt install live-build")
    if not (ROOT / "data/plymouth/polyos/logo.png").exists() or not (ROOT / "data/boot/splash.png").exists():
        sys.exit("Branding images are missing; run `python main.py branding` first.")
    debs = build_debs(DIST)
    if args.workdir:
        work = Path(args.workdir).expanduser().resolve()
    elif str(ROOT).startswith("/mnt/"):  # WSL: a Windows drive can't hold a Linux chroot
        work = Path.home() / "polyos-iso"
    else:
        work = BUILD / "iso"
    print(f"  work folder: {work}")
    if work.exists():
        subprocess.run(["lb", "clean", "--purge"], cwd=work, check=False)
        shutil.rmtree(work)
    work.mkdir(parents=True)
    mirror = args.mirror.rstrip("/") + "/"
    arch = args.arch or debian_arch()
    host = debian_arch()
    if arch != host:
        sys.exit(f"This is a {host} computer; build the {arch} ISO on a {arch} one (GitHub Actions builds both).")
    # ARM computers start through UEFI only (no PC BIOS): GRUB for EFI, no ISOLINUX
    arch_opts = ["--bootloaders", "grub-efi", "--linux-flavours", "arm64"] if arch == "arm64" else []
    sh("lb", "config",
       "--distribution", args.dist,
       "--architecture", arch,
       *arch_opts,
       "--archive-areas", "main contrib non-free non-free-firmware",
       "--binary-images", "iso-hybrid",
       "--debian-installer", "none",
       "--memtest", "none",
       "--mirror-bootstrap", mirror,
       "--mirror-binary", mirror,
       "--bootappend-live", "boot=live components quiet splash noeject username=polyos hostname=polyos",
       "--iso-application", "PolyOS",
       "--iso-publisher", "PolyOS Team",
       "--iso-volume", f"PolyOS {VERSION}",
       "--image-name", "polyos",
       "--chroot-squashfs-compression-type", "xz",
       cwd=work)
    shutil.copytree(ROOT / "iso" / "config", work / "config", dirs_exist_ok=True)
    branding = work / "config/includes.chroot/etc/calamares/branding/polyos"
    shutil.copytree(ROOT / "data/calamares/branding/polyos", branding, dirs_exist_ok=True)
    desc = branding / "branding.desc"
    desc.write_text(desc.read_text("utf-8").replace("@VERSION@", VERSION), "utf-8")
    for hook in [*(work / "config/hooks/live").glob("*.hook.chroot"), *(work / "config/hooks/live").glob("*.hook.binary")]:
        hook.chmod(0o755)
    # the boot menu's artwork; the 0600-polyos-bootmenu hook moves it into GRUB's theme
    shutil.copytree(ROOT / "data/boot", work / "config/includes.binary/boot/grub/polyos-theme", dirs_exist_ok=True)
    packages = work / "config/packages.chroot"
    packages.mkdir(parents=True, exist_ok=True)
    for deb in debs:
        shutil.copy(deb, packages / deb.name)
    log_path = work / "build.log"
    print(f"  $ lb build   (this takes a while; log: {log_path})")
    with open(log_path, "w") as log:
        proc = subprocess.Popen(["lb", "build"], cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            sys.stdout.write(line)
            log.write(line)
        if proc.wait():
            sys.exit(f"lb build failed; see {log_path}")
    isos = sorted(work.glob("*.iso"))
    if not isos:
        sys.exit(f"no ISO was produced; see {log_path}")
    DIST.mkdir(exist_ok=True)
    out = DIST / f"polyos-{VERSION}-{args.dist}-{arch}.iso"
    shutil.move(str(isos[0]), out)
    if arch == "arm64":
        usb_bootable(out, work)
    print(f"\nISO ready: {out}  ({out.stat().st_size / 1024 ** 3:.2f} GiB)\n"
          f"Write it to a USB stick with:  sudo dd if={out} of=/dev/sdX bs=4M status=progress oflag=sync")


def usb_bootable(iso: Path, work: Path) -> None:
    """Give an ARM64 ISO a partition table with its EFI system partition, so it starts from a USB stick.

    On PCs, live-build's iso-hybrid images already carry one. ARM64 images come out CD-only: fine
    for virtual machines, but ARM firmware (U-Boot, many UEFI boards) looks for an EFI partition
    on a USB drive. xorriso copies the ISO's own EFI boot image in as partition 2 (type 0xEF),
    leaving the ISO 9660 part untouched, so the result still starts as a CD or ISO too.
    """
    efi = work / "efi.img"
    new = iso.with_suffix(".usb.iso")
    sh("xorriso", "-osirrox", "on", "-indev", str(iso), "-extract", "/boot/grub/efi.img", str(efi))
    sh("xorriso", "-indev", str(iso), "-outdev", str(new),
       "-boot_image", "any", "replay",
       "-append_partition", "2", "0xef", str(efi),
       "-boot_image", "any", "partition_cyl_align=all",
       "-changes_pending", "yes", "-commit")
    if efi_partition(new) is None:
        sys.exit(f"{new} has no EFI system partition; the USB step failed")
    new.replace(iso)
    print("  added an EFI system partition: the ISO starts from a USB stick on ARM64 UEFI computers")


def efi_partition(image: Path) -> int | None:
    """Number (1-4) of the EFI system partition in a disk image's MBR, or None."""
    with open(image, "rb") as f:
        mbr = f.read(512)
    if len(mbr) < 512 or mbr[510:512] != b"\x55\xaa":
        return None
    for n in range(4):
        entry = mbr[446 + 16 * n: 462 + 16 * n]
        if entry[4] == 0xEF and int.from_bytes(entry[12:16], "little"):
            return n + 1
    return None


def cmd_nested(args) -> None:
    if not sys.platform.startswith("linux"):
        sys.exit("'nested' needs Linux with X11 (Xephyr). On Windows use 'python main.py dev'.")
    if not shutil.which("Xephyr"):
        sys.exit("Xephyr is missing:  sudo apt install xserver-xephyr")
    display = args.display
    xephyr = subprocess.Popen(["Xephyr", display, "-screen", args.size, "-ac", "-br", "-noreset", "-resizeable"])
    try:
        time.sleep(1.0)
        env = dict(os.environ, DISPLAY=display, PYTHONPATH=str(ROOT))
        env.pop("WAYLAND_DISPLAY", None)
        subprocess.run([sys.executable, "-m", "polyos.session"], env=env, cwd=ROOT, check=False)
    finally:
        xephyr.terminate()


def _logo_polygons(size: int, offset=(0, 0)) -> list[list[tuple[float, float]]]:
    """The PolyOS pinwheel (ui/img/logo.svg) as polygons scaled to `size` px, for Pillow."""
    svg = (ROOT / "ui/img/logo.svg").read_text("utf-8")
    tx, ty = (float(v) for v in re.search(r'translate\(([-\d.]+) ([-\d.]+)\)', svg).groups())
    vb = float(re.search(r'viewBox="0 0 ([\d.]+)', svg).group(1))
    k = size / vb
    polys = []
    for d in re.findall(r'<path d="([^"]+)"', svg):
        tokens = re.findall(r"[MmLlCcHhVvZz]|-?\d*\.?\d+(?:e-?\d+)?", d)
        pts, x, y, cmd, i = [], 0.0, 0.0, "", 0
        nums = lambda n: [float(t) for t in tokens[i:i + n]]  # noqa: E731
        while i < len(tokens):
            if re.match(r"[A-Za-z]", tokens[i]):
                cmd, i = tokens[i], i + 1
                if cmd in "Zz":
                    continue
            rel = cmd.islower()
            c = cmd.upper()
            if c in "ML":
                dx, dy = nums(2); i += 2
                x, y = (x + dx, y + dy) if rel else (dx, dy)
                pts.append((x, y))
                if c == "M":
                    cmd = "l" if rel else "L"
            elif c == "H":
                (v,) = nums(1); i += 1
                x = x + v if rel else v
                pts.append((x, y))
            elif c == "V":
                (v,) = nums(1); i += 1
                y = y + v if rel else v
                pts.append((x, y))
            elif c == "C":
                x1, y1, x2, y2, x3, y3 = nums(6); i += 6
                if rel:
                    x1, y1, x2, y2, x3, y3 = x + x1, y + y1, x + x2, y + y2, x + x3, y + y3
                for step in range(1, 13):  # flatten the curve
                    t = step / 12
                    mt = 1 - t
                    pts.append((mt ** 3 * x + 3 * mt * mt * t * x1 + 3 * mt * t * t * x2 + t ** 3 * x3,
                                mt ** 3 * y + 3 * mt * mt * t * y1 + 3 * mt * t * t * y2 + t ** 3 * y3))
                x, y = x3, y3
            else:
                i += 1
        polys.append([((px + tx) * k + offset[0], (py + ty) * k + offset[1]) for px, py in pts])
    return polys


def _crystal_wallpaper(path: Path, w: int = 2560, h: int = 1600, seed: int = 7) -> None:
    """PolyOS 7 "Crystal": an original low-poly crystal field in the PolyOS palette (setup backdrop)."""
    import math
    import random

    from PIL import Image, ImageDraw, ImageFilter

    rnd = random.Random(seed)
    ss = 2
    W, H = w * ss, h * ss
    stops = [(0.00, (30, 20, 64)), (0.28, (84, 58, 170)), (0.50, (103, 143, 217)), (0.70, (95, 196, 196)),
             (0.86, (190, 130, 220)), (1.00, (230, 140, 184))]

    def field(x: float, y: float) -> tuple[float, float, float]:
        t = 0.55 * x + 0.45 * (1 - y) + 0.08 * math.sin(6 * x + 3 * y) + 0.06 * math.sin(11 * y - 4 * x)
        t = min(1.0, max(0.0, t))
        for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
            if t <= t1:
                k = (t - t0) / (t1 - t0)
                return tuple(a + (b - a) * k for a, b in zip(c0, c1))
        return stops[-1][1]

    cols, rows = 30, 19
    pts = {}
    for r in range(rows + 1):
        for c in range(cols + 1):
            x, y = c / cols, r / rows
            if 0 < c < cols:
                x += rnd.uniform(-0.38, 0.38) / cols
            if 0 < r < rows:
                y += rnd.uniform(-0.38, 0.38) / rows
            pts[r, c] = (x, y, rnd.uniform(0, 1))  # z: facet height, for lighting
    light = (-0.45, -0.65, 0.62)
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    for r in range(rows):
        for c in range(cols):
            a, b, cc, d = pts[r, c], pts[r, c + 1], pts[r + 1, c + 1], pts[r + 1, c]
            tris = [(a, b, cc), (a, cc, d)] if rnd.random() < 0.5 else [(a, b, d), (b, cc, d)]
            for tri in tris:
                (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = tri
                ux, uy, uz = x2 - x1, y2 - y1, (z2 - z1) * 0.06
                vx, vy, vz = x3 - x1, y3 - y1, (z3 - z1) * 0.06
                nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
                n = math.sqrt(nx * nx + ny * ny + nz * nz) or 1
                shade = (nx * light[0] + ny * light[1] + nz * light[2]) / n
                shade = shade if nz >= 0 else -shade
                cx, cy = (x1 + x2 + x3) / 3, (y1 + y2 + y3) / 3
                base = field(cx, cy)
                k = 0.78 + 0.42 * shade + rnd.uniform(-0.04, 0.04)
                glint = 1.0 + (0.35 if rnd.random() < 0.035 else 0.0)  # a few bright crystal faces
                color = tuple(max(0, min(255, int(v * k * glint))) for v in base)
                draw.polygon([(x1 * W, y1 * H), (x2 * W, y2 * H), (x3 * W, y3 * H)], fill=color)
    img = img.resize((w, h), Image.Resampling.LANCZOS)
    glow = img.filter(ImageFilter.GaussianBlur(60))
    img = Image.blend(img, glow, 0.22)
    # soft vignette so white text stays readable on top
    shade = Image.new("L", (w, h), 0)
    sd = ImageDraw.Draw(shade)
    for i in range(24):
        inset = int(i * min(w, h) / 60)
        sd.rectangle([inset, inset, w - inset, h - inset], outline=int(150 - i * 6.2))
    shade = shade.filter(ImageFilter.GaussianBlur(80))
    img = Image.composite(Image.new("RGB", (w, h), (12, 10, 24)), img, shade)
    img.save(path, quality=88, optimize=True, progressive=True)


def cmd_branding(_args) -> None:
    """Render the PNG artwork for the boot splash and the installer (needs Pillow)."""
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont
    except ImportError:
        sys.exit("This needs Pillow:  python -m pip install pillow")
    _crystal_wallpaper(ROOT / "data/wallpapers/polyos-crystal.jpg")
    print("  wrote data/wallpapers/polyos-crystal.jpg")
    ss = 4  # supersample for smooth edges

    def logo(size: int, color=(255, 255, 255, 255)) -> Image.Image:
        img = Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        for poly in _logo_polygons(size * ss):
            draw.polygon(poly, fill=color)
        return img.resize((size, size), Image.Resampling.LANCZOS)

    font = lambda w, s: ImageFont.truetype(str(ROOT / f"ui/fonts/Poppins-{w}.ttf"), s)  # noqa: E731
    out_splash = ROOT / "data/plymouth/polyos"
    out_cal = ROOT / "data/calamares/branding/polyos"
    out_splash.mkdir(parents=True, exist_ok=True)
    out_cal.mkdir(parents=True, exist_ok=True)

    logo(160).save(out_splash / "logo.png")
    logo(256).save(out_cal / "logo.png")

    wall = Image.open(ROOT / "data/wallpapers/polyos-dusk.jpg").convert("RGB")

    def slide(name: str, title: str, text: str) -> None:
        w, h = 800, 440
        img = wall.resize((w, int(w * wall.height / wall.width))).crop((0, 0, w, h)).filter(ImageFilter.GaussianBlur(6))
        img = Image.blend(img, Image.new("RGB", (w, h), (18, 18, 20)), 0.45).convert("RGBA")
        img.alpha_composite(logo(120), (w - 170, h // 2 - 60))
        draw = ImageDraw.Draw(img)
        draw.text((56, 150), title, font=font("Bold", 38), fill="white")
        y = 212
        for line in text.split("\n"):
            draw.text((58, y), line, font=font("Medium", 18), fill=(225, 225, 230))
            y += 30
        img.convert("RGB").save(out_cal / name)

    slide("welcome.png", "It’s time to get started.", "Install PolyOS on this computer.\nIt only takes a few minutes.")
    slide("slide1.png", "Welcome to PolyOS", "The PolyOS 7 desktop, now on real hardware.\nBuilt on Debian, so it just works.")
    slide("slide2.png", "Your files, your way", "Files, Settings and the launcher are ready\nthe moment you sign in.")
    slide("slide3.png", "Meet Vara", "Ask Vara to open apps, change settings\nor answer questions.")

    # The USB stick's boot menu (GRUB, and ISOLINUX on PCs), 800x600. data/boot/ becomes the GRUB
    # theme's folder (iso/config/hooks/live/0600-polyos-bootmenu.hook.binary puts it in place).
    out_boot = ROOT / "data/boot"
    out_boot.mkdir(parents=True, exist_ok=True)
    w, h = 800, 600
    crystal = Image.open(ROOT / "data/wallpapers/polyos-crystal.jpg").convert("RGB")
    scale = max(w / crystal.width, h / crystal.height)
    base = crystal.resize((round(crystal.width * scale), round(crystal.height * scale)), Image.Resampling.LANCZOS)
    base = base.crop(((base.width - w) // 2, (base.height - h) // 2, (base.width + w) // 2, (base.height + h) // 2))
    base = Image.blend(base.filter(ImageFilter.GaussianBlur(3)), Image.new("RGB", (w, h), (14, 10, 30)), 0.35).convert("RGBA")

    def title(img: Image.Image) -> Image.Image:
        img.alpha_composite(logo(84), ((w - 84) // 2, 70))
        draw = ImageDraw.Draw(img)
        draw.text((w // 2, 200), "PolyOS 7", font=font("Bold", 40), fill="white", anchor="mm")
        draw.text((w // 2, 240), "for Debian", font=font("Medium", 15), fill=(215, 208, 240), anchor="mm")
        return img

    # the menu: its entries sit in the dark panel (four on the USB drive, up to five on a computer with
    # Windows too: PolyOS, its advanced options, Windows, firmware settings)
    splash = title(base.copy())
    panel = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(panel).rounded_rectangle((200, 292, 600, 452), 14, fill=(10, 8, 24, 170))
    splash.alpha_composite(panel)
    splash.convert("RGB").save(out_boot / "splash.png", optimize=True)
    # after choosing an entry, while PolyOS loads (GRUB's full-screen text area shows this)
    loading = title(base.copy())
    ImageDraw.Draw(loading).text((w // 2, 330), "Starting PolyOS…", font=font("Medium", 17), fill=(235, 230, 250), anchor="mm")
    loading.convert("RGB").save(out_boot / "boot.png", optimize=True)
    # the text area itself has no frame, so nothing covers the picture
    for side in ("c", "n", "s", "e", "w", "ne", "nw", "se", "sw"):
        Image.new("RGBA", (4, 4), (0, 0, 0, 0)).save(out_boot / f"terminal_box_{side}.png")
    # the selected entry: a rounded purple bar, cut into GRUB's nine slices
    r = 6
    bar = Image.new("RGBA", (2 * r + 1, 2 * r + 1), (0, 0, 0, 0))
    ImageDraw.Draw(bar).rounded_rectangle((0, 0, 2 * r, 2 * r), r, fill=(143, 124, 240, 200))
    slices = {"nw": (0, 0, r, r), "n": (r, 0, r + 1, r), "ne": (r + 1, 0, 2 * r + 1, r),
              "w": (0, r, r, r + 1), "c": (r, r, r + 1, r + 1), "e": (r + 1, r, 2 * r + 1, r + 1),
              "sw": (0, r + 1, r, 2 * r + 1), "s": (r, r + 1, r + 1, 2 * r + 1), "se": (r + 1, r + 1, 2 * r + 1, 2 * r + 1)}
    for side, box in slices.items():
        bar.crop(box).save(out_boot / f"select_{side}.png")
    for f in sorted([*out_splash.glob("*.png"), *out_cal.glob("*.png"), *out_boot.glob("*.png")]):
        print(f"  wrote {f.relative_to(ROOT)}")


def cmd_clean(_args) -> None:
    for path in (BUILD, DIST):
        if path.exists():
            shutil.rmtree(path)
            print(f"  removed {path.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser("dev", help="run the UI in a browser with a simulated system")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--live", action="store_true", help="simulate the live USB session (installer card)")
    p.add_argument("--dual-boot", action="store_true", help="with --live: the dual-boot ISO's installer")
    p.set_defaults(fn=cmd_dev)
    sub.add_parser("test", help="run unit tests").set_defaults(fn=cmd_test)
    sub.add_parser("check", help="static checks").set_defaults(fn=cmd_check)
    p = sub.add_parser("deb", help="build .deb packages")
    p.add_argument("--out", default=str(DIST))
    p.add_argument("--manifest", action="store_true", help="also write polyos-update.json (online updates)")
    p.set_defaults(fn=cmd_deb)
    p = sub.add_parser("install", help="install on this Debian system (root)")
    p.add_argument("--shell-only", action="store_true", help="only polyos-shell: add a PolyOS session to an existing desktop")
    p.add_argument("--keep-ifupdown", action="store_true", help="leave /etc/network/interfaces untouched")
    p.set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="remove PolyOS packages (root)").set_defaults(fn=cmd_uninstall)
    p = sub.add_parser("iso", help="build a live ISO with live-build (root, Debian host)")
    p.add_argument("--dist", default="trixie", choices=["trixie", "bookworm"])
    p.add_argument("--arch", choices=["amd64", "arm64"],
                   help="amd64 (Intel/AMD PCs) or arm64 (ARM64 UEFI computers); default: this computer's. "
                        "Build on a computer of the same kind (live-build doesn't cross-build)")
    p.add_argument("--mirror", default="http://deb.debian.org/debian/")
    p.add_argument("--workdir", help="where live-build works (default build/iso; ~/polyos-iso under WSL)")
    p.set_defaults(fn=cmd_iso)
    p = sub.add_parser("nested", help="run the session inside Xephyr")
    p.add_argument("--size", default="1366x768")
    p.add_argument("--display", default=":5")
    p.set_defaults(fn=cmd_nested)
    sub.add_parser("branding", help="render boot splash and installer artwork (needs Pillow)").set_defaults(fn=cmd_branding)
    sub.add_parser("clean", help="remove build outputs").set_defaults(fn=cmd_clean)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
