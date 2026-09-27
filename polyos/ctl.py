"""polyos-ctl: control the running shell (used by keybindings, scripts and .desktop files)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import paths, system

STEP = 5


class ShellUnavailable(Exception):
    pass


def call(method: str, path: str, body: dict | None = None):
    info = paths.read_runtime_info()
    if not info:
        raise ShellUnavailable("the PolyOS shell is not running")
    try:
        os.kill(int(info["pid"]), 0)
    except (OSError, ValueError, KeyError):
        raise ShellUnavailable("the PolyOS shell is not running") from None
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-PolyOS-Token": info["token"], "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("error", exc.reason)
        except ValueError:
            message = exc.reason
        raise SystemExit(f"polyos-ctl: {message}") from None
    except urllib.error.URLError as exc:
        raise ShellUnavailable(str(exc.reason)) from None


def _level(value: str) -> dict:
    if value == "up":
        return {"delta": STEP}
    if value == "down":
        return {"delta": -STEP}
    if value.isdigit():
        return {"level": int(value)}
    raise SystemExit(f"polyos-ctl: expected up, down or 0-100, got {value!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="polyos-ctl", description="Control the running PolyOS shell.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start-menu", help="toggle the Start menu")
    p = sub.add_parser("popup", help="toggle a shell popup")
    p.add_argument("view", choices=["start", "launcher", "quick", "calendar", "run", "power", "vara", "quickmenu", "widgets", "project"])
    p = sub.add_parser("open", help="open a built-in app")
    p.add_argument("app", choices=["settings", "files", "setup", "taskmgr", "drivers", "store", "camera"])
    p.add_argument("page", nargs="?", help="Settings page, or the folder/URI for Files")
    p = sub.add_parser("volume", help="up | down | mute | 0-100")
    p.add_argument("value")
    p = sub.add_parser("brightness", help="up | down | 0-100")
    p.add_argument("value")
    p = sub.add_parser("power", help="lock, log out, sleep, restart or shut down")
    p.add_argument("action", choices=["lock", "logout", "suspend", "reboot", "poweroff"])
    p = sub.add_parser("run", help="open the default terminal, file manager or browser")
    p.add_argument("what", choices=["terminal", "files", "browser"])
    sub.add_parser("restart", help="restart the shell (apps keep running)")
    sub.add_parser("status", help="print the shell state as JSON")
    p = sub.add_parser("cloud", help="open a cloud gaming service (geforcenow, xcloud, luna, boosteroid)")
    p.add_argument("service")
    p = sub.add_parser("dev", help="developer mode: on | off (off also rescues a broken interface)")
    p.add_argument("state", choices=["on", "off"])
    args = parser.parse_args(argv)

    if args.command == "cloud":  # needs no shell: just the browser
        from . import gaming

        try:
            gaming.open_cloud(args.service)
        except (ValueError, OSError) as exc:
            print(f"polyos-ctl: {exc}", file=sys.stderr)
            return 1
        return 0
    try:
        if args.command == "dev":
            call("POST", "/api/settings", {"developerMode": args.state == "on"})
            if args.state == "off":
                call("POST", "/api/shell/restart", {})
            return 0
        if args.command == "start-menu":  # the Windows key: open Start, or close any open menu
            call("POST", "/api/popup", {"view": "start", "toggle": True})
        elif args.command == "popup":
            call("POST", "/api/popup", {"view": args.view})
        elif args.command == "open":
            page = args.page
            if page and page.startswith("file://"):  # .desktop %U hands us URIs
                page = urllib.parse.unquote(urllib.parse.urlsplit(page).path)
            if args.app == "files" and page and not os.path.isdir(page):
                call("POST", "/api/files/open", {"path": os.path.abspath(page)})  # a file: open it
            else:
                call("POST", "/api/open", {"app": args.app, "page": os.path.abspath(page) if args.app == "files" and page else page})
        elif args.command == "volume":
            body = {"toggleMute": True} if args.value == "mute" else _level(args.value)
            call("POST", "/api/volume", body)
        elif args.command == "brightness":
            call("POST", "/api/brightness", _level(args.value))
        elif args.command == "power":
            call("POST", "/api/power", {"action": args.action})
        elif args.command == "run":
            call("POST", "/api/run", {"what": args.what})
        elif args.command == "restart":
            call("POST", "/api/shell/restart", {})
        elif args.command == "status":
            state = call("GET", "/api/state")
            state.pop("apps", None)
            print(json.dumps(state, indent=2))
        return 0
    except ShellUnavailable as exc:
        return fallback(args, str(exc))


def fallback(args, reason: str) -> int:
    """Hardware keys and power actions keep working even if the shell is down."""
    try:
        if args.command == "volume":
            if args.value == "mute":
                system.Audio().set(toggle_mute=True)
            else:
                system.Audio().set(**_level(args.value))
        elif args.command == "brightness":
            system.Backlight().set(**_level(args.value))
        elif args.command == "power" and args.action != "logout":
            system.power(args.action)
        elif args.command == "run":
            system.run_default(args.what)
        elif args.command == "dev":  # the shell is down (maybe a broken override): edit the file directly
            from .core import Settings

            Settings(paths.config_dir() / "settings.json").update({"developerMode": args.state == "on"})
            print(f"Developer mode is {args.state}. Sign out and back in, or run: polyos-ctl restart")
        else:
            print(f"polyos-ctl: {reason}", file=sys.stderr)
            return 1
    except RuntimeError as exc:
        print(f"polyos-ctl: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
