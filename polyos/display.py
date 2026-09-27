"""Settings > Display: each screen's resolution, refresh rate, orientation and which is the main one.

Reads `xrandr --query` and changes modes with `xrandr --output ...`. What you choose is kept in
the "displays" setting and applied again when PolyOS starts, so it survives restarts.
"""

from __future__ import annotations

import re

ROTATIONS = ("normal", "left", "right", "inverted")
_OUTPUT = re.compile(r"^(\S+) (connected|disconnected)( primary)?(?: (\d+)x(\d+)\+(\d+)\+(\d+))?(?: (normal|left|right|inverted))?")
_MODE = re.compile(r"^\s+(\d+)x(\d+)i?\s+(.*)$")
_RATE = re.compile(r"(\d+(?:\.\d+)?)(\*)?(\+)?")


def parse_xrandr(text: str) -> list[dict]:
    """Connected outputs: name, primary, current mode and rate, rotation, and every mode with its rates."""
    outputs: list[dict] = []
    current = None
    for line in text.splitlines():
        m = _OUTPUT.match(line)
        if m:
            current = None
            if m.group(2) != "connected":
                continue
            current = {"name": m.group(1), "primary": bool(m.group(3)), "active": m.group(4) is not None,
                       "rotation": m.group(8) or "normal", "mode": None, "rate": None, "preferred": None, "modes": [],
                       "x": int(m.group(6) or 0), "y": int(m.group(7) or 0)}
            outputs.append(current)
            continue
        if current is None:
            continue
        m = _MODE.match(line)
        if not m:
            continue
        size = f"{m.group(1)}x{m.group(2)}"
        rates = []
        for token in m.group(3).split():  # "60.01*+", "59.95", and sometimes a lone "+" or "*" after a rate
            r = _RATE.fullmatch(token)
            if r:
                rates.append(round(float(r.group(1)), 2))
            marks = token if token in ("+", "*", "*+") else (r.group(2) or "") + (r.group(3) or "") if r else ""
            if "*" in marks and rates:
                current["mode"], current["rate"] = size, rates[-1]
            if "+" in marks and not current["preferred"]:
                current["preferred"] = size
        known = next((md for md in current["modes"] if md["size"] == size), None)
        if known:
            known["rates"] = sorted(set(known["rates"] + rates), reverse=True)
        elif rates:
            current["modes"].append({"size": size, "rates": sorted(set(rates), reverse=True)})
    return outputs


def validate(outputs: list[dict], name: str, size: str | None, rate: float | None, rotation: str | None) -> None:
    """Refuse anything the screen doesn't offer (so a bad value can't blank it)."""
    out = next((o for o in outputs if o["name"] == name), None)
    if out is None:
        raise ValueError("That screen isn't connected.")
    if size is not None:
        mode = next((md for md in out["modes"] if md["size"] == size), None)
        if mode is None:
            raise ValueError(f"{name} can't show {size}.")
        if rate is not None and not any(abs(r - rate) < 0.05 for r in mode["rates"]):
            raise ValueError(f"{name} can't run {size} at {rate:g} Hz.")
    if rotation is not None and rotation not in ROTATIONS:
        raise ValueError("Choose an orientation.")


def command(name: str, size: str | None = None, rate: float | None = None, rotation: str | None = None,
            primary: bool = False) -> list[str]:
    args = ["xrandr", "--output", name]
    if size:
        args += ["--mode", size]
        if rate:
            args += ["--rate", f"{rate:g}"]
    if rotation:
        args += ["--rotate", rotation]
    if primary:
        args.append("--primary")
    return args


# Night light: the screens' gamma, warmer (less blue), like about 4500 K. Off is 1:1:1.
NIGHT_GAMMA = "1:0.86:0.68"


def night_light_commands(outputs: list[dict], on: bool) -> list[list[str]]:
    """xrandr commands that turn night light on or off on every connected screen."""
    gamma = NIGHT_GAMMA if on else "1:1:1"
    return [["xrandr", "--output", o["name"], "--gamma", gamma] for o in outputs if o.get("connected", True)]


# ---- more than one screen: duplicate, extend, or one of them (like Windows' Win+P) --------------
MODES = ("duplicate", "extend", "main", "second")
MODE_NAMES = {"duplicate": "Duplicate", "extend": "Extend", "main": "This screen only", "second": "Second screen only"}
INTERNAL = ("eDP", "LVDS", "DSI")


def still_on(text: str) -> list[str]:
    """Outputs xrandr still shows as on (with a position) after their screen was unplugged."""
    out = []
    for line in text.splitlines():
        m = _OUTPUT.match(line)
        if m and m.group(2) == "disconnected" and m.group(4) is not None:
            out.append(m.group(1))
    return out


def main_output(outputs: list[dict]) -> dict:
    """The main screen: a laptop's own panel, else the one marked primary, else the first."""
    return (next((o for o in outputs if o["name"].startswith(INTERNAL)), None)
            or next((o for o in outputs if o["primary"]), None) or outputs[0])


def _size(o: dict) -> str | None:
    return o["preferred"] or (o["modes"][0]["size"] if o["modes"] else None)


def _mode_args(o: dict, saved: dict) -> list[str]:
    cfg = saved.get(o["name"]) or {}
    size = cfg.get("size") if any(md["size"] == cfg.get("size") for md in o["modes"]) else None
    args = ["--mode", size] if size else ["--auto"]
    if size and cfg.get("rate") and any(abs(r - cfg["rate"]) < 0.05 for md in o["modes"] if md["size"] == size for r in md["rates"]):
        args += ["--rate", f"{cfg['rate']:g}"]
    if cfg.get("rotation") in ROTATIONS:
        args += ["--rotate", cfg["rotation"]]
    return args


def common_size(outputs: list[dict]) -> str | None:
    """The biggest resolution every screen can show (for duplicating)."""
    sets = [{md["size"] for md in o["modes"]} for o in outputs]
    shared = set.intersection(*sets) if sets else set()
    return max(shared, key=lambda sz: tuple(int(x) for x in sz.split("x")), default=None)


def layout_command(outputs: list[dict], mode: str, saved: dict | None = None, stale: list[str] | None = None) -> list[str]:
    """One xrandr command for the whole arrangement (one step, so no screen is left blank midway)."""
    saved = saved or {}
    if not outputs:
        return []
    main = main_output(outputs)
    others = [o for o in outputs if o is not main]
    args = ["xrandr"]
    for name in stale or []:
        args += ["--output", name, "--off"]
    if not others or mode not in MODES:
        mode = "extend"
    if mode == "duplicate" and others:
        size = common_size(outputs)
        if size:
            args += ["--output", main["name"], "--mode", size, "--pos", "0x0", "--rotate", "normal", "--primary"]
            for o in others:
                args += ["--output", o["name"], "--mode", size, "--same-as", main["name"], "--rotate", "normal"]
        else:  # nothing in common: the others show the main screen scaled to fit
            msize = _size(main) or "1920x1080"
            args += ["--output", main["name"], "--mode", msize, "--pos", "0x0", "--primary"]
            for o in others:
                args += ["--output", o["name"], "--auto", "--same-as", main["name"], "--scale-from", msize]
    elif mode == "main":
        args += ["--output", main["name"], *_mode_args(main, saved), "--pos", "0x0", "--primary"]
        for o in others:
            args += ["--output", o["name"], "--off"]
    elif mode == "second":
        second = others[0]
        args += ["--output", second["name"], *_mode_args(second, saved), "--pos", "0x0", "--primary",
                 "--output", main["name"], "--off"]
        for o in others[1:]:
            args += ["--output", o["name"], "--off"]
    else:  # extend: the main screen on the left, the others to its right, in order
        args += ["--output", main["name"], *_mode_args(main, saved), "--pos", "0x0", "--primary"]
        left = main["name"]
        for o in others:
            args += ["--output", o["name"], *_mode_args(o, saved), "--right-of", left]
            left = o["name"]
    return args


def connectors(drm: "Path | None" = None) -> tuple:
    """What's plugged in, from the kernel (cheap to read every few seconds, unlike xrandr --query)."""
    from pathlib import Path as _P
    root = drm or _P("/sys/class/drm")
    out = []
    for status in sorted(root.glob("card*-*/status")):
        try:
            out.append((status.parent.name, status.read_text().strip()))
        except OSError:
            continue
    return tuple(out)
