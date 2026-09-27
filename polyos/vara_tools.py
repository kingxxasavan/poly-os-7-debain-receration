"""Vara's tools: what the agent can do on the computer, and how much each action needs a yes.

Every tool has a risk level. "read" tools only look (list and read files, search, check a 3D
model's size, list ROS topics) and run without asking. "write" tools change files (write, edit,
render an OpenSCAD model to a file). "run" tools start programs or reach hardware (terminal
commands, Blender scripts, `git commit`, `ros2 topic pub`, `arduino-cli upload`). Settings >
Vara chooses what needs approval; by default Vara asks before anything that isn't "read".

Files Vara must never see (SSH and GPG keys, browser profiles, saved logins, its own API key)
are refused whatever the setting. Commands run as the signed-in person, never as root.
"""

from __future__ import annotations

import glob
import html
import json
import os
import re
import shlex
import shutil
import signal
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .core import ApiError

READ, WRITE, RUN = "read", "write", "run"
OUTPUT_LIMIT = 12_000  # characters of tool output sent back to the model
READ_LIMIT = 200_000  # bytes read_file will look at
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist", ".cache", "target"}

# Relative to the home folder. Never read or written, whatever the approval setting.
PRIVATE = (".ssh", ".gnupg", ".password-store", ".local/share/keyrings", ".mozilla", ".config/google-chrome",
           ".config/chromium", ".config/BraveSoftware", ".netrc", ".git-credentials", ".aws", ".config/gh",
           ".docker/config.json", ".config/polyos/vara.json", ".config/polyos/recovery", ".pki")


def local_bin() -> str:
    return os.path.expanduser("~/.local/bin")  # where pip --user, arduino-cli's installer and others put programs


def which(program: str) -> str | None:
    return shutil.which(program) or shutil.which(program, path=local_bin())


class ToolError(Exception):
    """A problem to report back to the model (and the person) as the tool's result."""


@dataclass
class ToolContext:
    home: Path
    workspace: Path
    backend: object = None
    skills: object = None
    memory: object = None
    schedule: object = None  # reminders and routines (vara_schedule.Schedule)
    index: object = None  # the documents in the person's folders (vara_index.DocIndex)
    expert: dict | None = None  # the expert helper model (Settings > Vara), for consult_expert
    plan: Callable[[list], None] | None = None  # shows the plan in the chat (and the HUD)
    cancel: threading.Event = field(default_factory=threading.Event)
    gentle: bool = False  # background activity limited: programs run at low CPU priority


@dataclass
class Tool:
    name: str
    label: str  # for the approval card and Settings: "Run a command"
    description: str
    params: dict
    required: list[str]
    risk: str | Callable[[dict], str]
    run: Callable[[ToolContext, dict], str]
    title: Callable[[dict], str]  # one line for the step card: "Ran git status"
    detail: Callable[[dict], str] = lambda args: ""  # noqa: E731 - the command, script or path
    targets: Callable[[ToolContext, dict], list[Path]] = lambda ctx, args: []  # noqa: E731 - files a write touches
    needs: Callable[[], bool] | None = None  # installed?
    icon: str = "tool"

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": self.params, "required": self.required}}}

    def risk_for(self, args: dict) -> str:
        return self.risk(args) if callable(self.risk) else self.risk

    def available(self) -> bool:
        return self.needs is None or bool(self.needs())


# ---- paths ------------------------------------------------------------------------------------

def resolve(ctx: ToolContext, path: str, write: bool = False) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise ToolError("A path is needed.")
    raw = path.strip()
    if raw == "~" or raw.startswith("~/"):
        p = ctx.home / raw[2:]  # the person's home (the dev preview has its own)
    else:
        p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = ctx.workspace / p
    p = Path(os.path.realpath(p))
    home = Path(os.path.realpath(ctx.home))
    for private in PRIVATE:
        blocked = home / private
        if p == blocked or blocked in p.parents:
            raise ToolError(f"{path} is private (keys, passwords or browser data); Vara doesn't open it.")
    if write:
        tmp = Path(os.path.realpath(tempfile.gettempdir()))
        if not (p == home or home in p.parents or tmp in p.parents):
            raise ToolError(f"Vara only changes files in your home folder or {tmp}, not {p}.")
    return p


def inside(path: Path, folder: Path) -> bool:
    folder = Path(os.path.realpath(folder))
    return path == folder or folder in path.parents


def show(ctx: ToolContext, path: Path) -> str:
    """A path as the person would say it: ~/Projects/robot.py."""
    try:
        return "~/" + str(path.relative_to(os.path.realpath(ctx.home)))
    except ValueError:
        return str(path)


def clip(text: str, limit: int = OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    head = limit // 3
    return f"{text[:head]}\n… ({len(text) - limit} characters cut) …\n{text[-(limit - head):]}"


# ---- running programs -------------------------------------------------------------------------

def _low_priority() -> None:
    os.nice(10)


def execute(ctx: ToolContext, argv: list[str], cwd: Path, timeout: float, stdin: str | None = None) -> str:
    """Run a program; its output and exit code as text. Stops with the chat's Stop button."""
    env = dict(os.environ, TERM="dumb", NO_COLOR="1", GIT_TERMINAL_PROMPT="0", PAGER="cat", GIT_PAGER="cat")
    if local_bin() not in env.get("PATH", "").split(":"):
        env["PATH"] = f"{local_bin()}:{env.get('PATH', '/usr/bin:/bin')}"
    argv = [which(argv[0]) or argv[0], *argv[1:]]
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                start_new_session=True, preexec_fn=_low_priority if ctx.gentle else None)
    except FileNotFoundError:
        raise ToolError(f"{argv[0]} isn't installed.") from None
    except OSError as exc:
        raise ToolError(f"Couldn't start {argv[0]}: {exc.strerror}") from None
    chunks: list[bytes] = []

    def pump():
        if stdin is not None:
            try:
                proc.stdin.write(stdin.encode())
                proc.stdin.close()
            except OSError:
                pass
        for chunk in iter(lambda: proc.stdout.read(4096), b""):
            chunks.append(chunk)
            if sum(map(len, chunks)) > 4 * OUTPUT_LIMIT:  # keep the start and the newest output
                del chunks[1:len(chunks) // 2]

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    note = ""
    while proc.poll() is None:
        if ctx.cancel.is_set() or time.monotonic() > deadline:
            note = "\n(stopped by the person)" if ctx.cancel.is_set() else f"\n(stopped after {timeout:.0f} s)"
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            break
        time.sleep(0.1)
    proc.wait()
    reader.join(2)
    if not reader.is_alive():  # a background child can keep the pipe open; its reader then ends with it
        proc.stdout.close()
    out = b"".join(chunks).decode("utf-8", "replace").strip()
    return clip(f"exit code {proc.returncode}{note}\n{out}" if out else f"exit code {proc.returncode}{note}")


def _cwd(ctx: ToolContext, args: dict) -> Path:
    cwd = resolve(ctx, args["cwd"]) if args.get("cwd") else ctx.workspace
    if not cwd.is_dir():
        raise ToolError(f"There's no folder {show(ctx, cwd)}.")
    return cwd


def _timeout(args: dict, default: int, most: int) -> int:
    try:
        return max(1, min(most, int(args.get("timeout") or default)))
    except (TypeError, ValueError):
        return default


# ---- files ------------------------------------------------------------------------------------

def list_files(ctx: ToolContext, args: dict) -> str:
    folder = resolve(ctx, args.get("path") or ".")
    if not folder.is_dir():
        raise ToolError(f"{show(ctx, folder)} isn't a folder.")
    hidden = bool(args.get("hidden"))
    lines = []
    for entry in sorted(folder.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
        if entry.name.startswith(".") and not hidden:
            continue
        try:
            lines.append(f"{entry.name}/" if entry.is_dir() else f"{entry.name}  ({entry.stat().st_size} bytes)")
        except OSError:
            continue
        if len(lines) >= 400:
            lines.append("… more entries not shown")
            break
    return f"{show(ctx, folder)}:\n" + ("\n".join(lines) or "(empty)")


def read_file(ctx: ToolContext, args: dict) -> str:
    path = resolve(ctx, args["path"])
    if not path.is_file():
        raise ToolError(f"There's no file {show(ctx, path)}.")
    with open(path, "rb") as fh:
        raw = fh.read(READ_LIMIT + 1)
    if b"\0" in raw[:8000]:
        raise ToolError(f"{show(ctx, path)} is a binary file; use model_info for 3D models.")
    text = raw[:READ_LIMIT].decode("utf-8", "replace")
    lines = text.splitlines()
    start = max(1, int(args.get("start_line") or 1))
    count = max(1, min(2000, int(args.get("max_lines") or 400)))
    chosen = lines[start - 1:start - 1 + count]
    body = "\n".join(f"{n:>5}  {line}" for n, line in enumerate(chosen, start))
    more = len(lines) - (start - 1 + len(chosen))
    tail = f"\n… {more} more lines (use start_line)" if more > 0 else ""
    if len(raw) > READ_LIMIT:
        tail += "\n… file continues past 200 KB"
    return clip(f"{show(ctx, path)} ({len(lines)} lines)\n{body}{tail}")


def search_files(ctx: ToolContext, args: dict) -> str:
    root = resolve(ctx, args.get("path") or ".")
    try:
        pattern = re.compile(args["query"], re.I)
    except re.error:
        pattern = re.compile(re.escape(args["query"]), re.I)
    name_glob = args.get("glob") or "*"
    hits: list[str] = []
    files = [root] if root.is_file() else None
    walker = ((str(root.parent), [], [root.name]),) if files else os.walk(root)
    for dirpath, dirnames, filenames in walker:
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if not path.match(name_glob):
                continue
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                resolve(ctx, str(path))
                with open(path, "rb") as fh:
                    raw = fh.read()
            except (OSError, ToolError):
                continue
            if b"\0" in raw[:4000]:
                continue
            for n, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{show(ctx, path)}:{n}: {line.strip()[:200]}")
                    if len(hits) >= 150:
                        return "\n".join(hits) + "\n… more matches not shown"
    return "\n".join(hits) or "No matches."


def write_file(ctx: ToolContext, args: dict) -> str:
    path = resolve(ctx, args["path"], write=True)
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("content must be text.")
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, "utf-8")
    return f"{'Replaced' if existed else 'Created'} {show(ctx, path)} ({len(content.encode())} bytes)."


def edit_file(ctx: ToolContext, args: dict) -> str:
    path = resolve(ctx, args["path"], write=True)
    if not path.is_file():
        raise ToolError(f"There's no file {show(ctx, path)}.")
    text = path.read_text("utf-8")
    old, new = args.get("old_text", ""), args.get("new_text", "")
    if not old:
        raise ToolError("old_text can't be empty.")
    count = text.count(old)
    if count == 0:
        raise ToolError("old_text wasn't found; read the file again and copy the exact text.")
    if count > 1 and not args.get("replace_all"):
        raise ToolError(f"old_text appears {count} times; add more surrounding lines, or set replace_all.")
    path.write_text(text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1), "utf-8")
    return f"Edited {show(ctx, path)} ({count if args.get('replace_all') else 1} change)."


# ---- 3D models ----------------------------------------------------------------------------------

def mesh_stats(path: Path) -> dict:
    """Size and triangle count of an STL (text or binary) or OBJ file, in the file's units."""
    lo, hi = [float("inf")] * 3, [float("-inf")] * 3
    count = 0

    def add(x, y, z):
        for i, v in enumerate((x, y, z)):
            lo[i] = min(lo[i], v)
            hi[i] = max(hi[i], v)

    suffix = path.suffix.lower()
    with open(path, "rb") as fh:
        data = fh.read(200_000_000)
    if suffix == ".stl" and len(data) >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        if 84 + 50 * n == len(data):  # binary STL
            for i in range(n):
                v = struct.unpack_from("<12f", data, 84 + 50 * i)
                add(*v[3:6]); add(*v[6:9]); add(*v[9:12])  # noqa: E702
            count = n
        else:
            for m in re.finditer(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data):
                add(*map(float, m.groups()))
            count = len(re.findall(rb"endfacet", data))
    elif suffix == ".obj":
        for m in re.finditer(rb"^v\s+(\S+)\s+(\S+)\s+(\S+)", data, re.M):
            add(*map(float, m.groups()))
        count = len(re.findall(rb"^f\s", data, re.M))
    else:
        raise ToolError("model_info reads .stl and .obj files.")
    if lo[0] == float("inf"):
        raise ToolError(f"{path.name} has no geometry.")
    size = [round(h - lo_, 3) for h, lo_ in zip(hi, lo)]
    return {"triangles" if suffix == ".stl" else "faces": count, "size": size,
            "min": [round(v, 3) for v in lo], "max": [round(v, 3) for v in hi]}


def model_info(ctx: ToolContext, args: dict) -> str:
    path = resolve(ctx, args["path"])
    if not path.is_file():
        raise ToolError(f"There's no file {show(ctx, path)}.")
    stats = mesh_stats(path)
    x, y, z = stats["size"]
    return (f"{show(ctx, path)}: {x} × {y} × {z} (X × Y × Z, usually millimeters), "
            + json.dumps({k: v for k, v in stats.items() if k != "size"}))


def openscad(ctx: ToolContext, args: dict) -> str:
    out = resolve(ctx, args["output"], write=True)
    if out.suffix.lower() not in (".stl", ".3mf", ".off", ".amf", ".png", ".svg", ".dxf"):
        raise ToolError("output must end in .stl, .3mf, .png, .svg or .dxf.")
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.get("file"):
        source = resolve(ctx, args["file"])
    elif args.get("code"):
        source = out.with_suffix(".scad")  # listed in the approval's targets
        source.write_text(args["code"], "utf-8")
    else:
        raise ToolError("Give OpenSCAD code or a .scad file.")
    argv = ["openscad", "-o", str(out), str(source)]
    if out.suffix.lower() == ".png":
        argv[1:1] = ["--autocenter", "--viewall", "--imgsize=1024,768", "--colorscheme=Tomorrow Night"]
    result = execute(ctx, argv, source.parent, _timeout(args, 180, 900))
    if out.exists() and out.suffix.lower() == ".stl":
        try:
            x, y, z = mesh_stats(out)["size"]
            result += f"\nModel size: {x} × {y} × {z} mm"
        except (ToolError, OSError, ValueError):
            pass
    return f"source {show(ctx, source)} → {show(ctx, out)}\n{result}"


def blender(ctx: ToolContext, args: dict) -> str:
    script = args.get("script", "")
    if not script.strip():
        raise ToolError("Give Blender a Python script (bpy).")
    argv = ["blender", "--background"]
    if args.get("blend_file"):
        blend = resolve(ctx, args["blend_file"])
        if not blend.is_file():
            raise ToolError(f"There's no file {show(ctx, blend)}.")
        argv.append(str(blend))
    else:
        argv.append("--factory-startup")
    with tempfile.NamedTemporaryFile("w", suffix=".py", prefix="vara-", delete=False) as fh:
        fh.write(script)
    try:
        argv += ["--python-exit-code", "1", "--python", fh.name]
        result = execute(ctx, argv, ctx.workspace, _timeout(args, 300, 1800))
    finally:
        os.unlink(fh.name)
    # Blender's start-up banner and "Blender quit" lines are noise
    lines = [ln for ln in result.splitlines() if not ln.startswith(("Blender ", "Read prefs", "Read blend"))]
    return "\n".join(lines)


# ---- code -------------------------------------------------------------------------------------

def run_command(ctx: ToolContext, args: dict) -> str:
    command = args.get("command", "")
    if not command.strip():
        raise ToolError("Give a command.")
    return execute(ctx, ["bash", "-lc", command], _cwd(ctx, args), _timeout(args, 120, 1800))


GIT_READ = {"status", "diff", "log", "show", "blame", "ls-files", "rev-parse", "describe", "shortlog", "grep"}


def _git_args(args: dict) -> list[str]:
    try:
        words = shlex.split(args.get("args", ""))
    except ValueError as exc:
        raise ToolError(f"Couldn't read the git arguments: {exc}") from None
    if words[:1] == ["git"]:
        words = words[1:]
    if not words:
        raise ToolError("Give git arguments, like “status”.")
    return words


def git_risk(args: dict) -> str:
    try:
        words = _git_args(args)
    except ToolError:
        return RUN
    if any(w.startswith(("--output", "--ext-diff", "--textconv", "-O")) for w in words):
        return RUN  # writes a file or runs a configured program
    if words[0] in GIT_READ:
        return READ
    if words[0] == "branch" and all(w in ("-a", "-r", "-v", "-vv", "--list") for w in words[1:]):
        return READ  # listing branches, not making or deleting one
    if words in (["remote"], ["remote", "-v"]):
        return READ
    return RUN


def git(ctx: ToolContext, args: dict) -> str:
    return execute(ctx, ["git", "--no-pager", *_git_args(args)], _cwd(ctx, args), _timeout(args, 120, 900))


# ---- robots and boards ------------------------------------------------------------------------

def ros_setup() -> str | None:
    found = sorted(glob.glob("/opt/ros/*/setup.bash"))
    return found[-1] if found else None


def ros_available() -> bool:
    return bool(which("ros2") or ros_setup())


ROS_READ = {("topic", "list"), ("topic", "info"), ("topic", "echo"), ("topic", "hz"), ("topic", "type"),
            ("node", "list"), ("node", "info"), ("service", "list"), ("service", "type"), ("action", "list"),
            ("action", "info"), ("param", "list"), ("param", "get"), ("param", "describe"), ("pkg", "list"),
            ("pkg", "executables"), ("pkg", "prefix"), ("interface", "list"), ("interface", "show"),
            ("doctor",), ("wtf",)}


def _words(args: dict, key: str = "args") -> list[str]:
    try:
        return shlex.split(args.get(key, ""))
    except ValueError as exc:
        raise ToolError(f"Couldn't read the arguments: {exc}") from None


def ros_risk(args: dict) -> str:
    try:
        words = [w for w in _words(args) if w != "ros2"]
    except ToolError:
        return RUN
    return READ if tuple(words[:2]) in ROS_READ or tuple(words[:1]) in ROS_READ else RUN


def ros2(ctx: ToolContext, args: dict) -> str:
    words = [w for w in _words(args) if w != "ros2"]
    if not words:
        raise ToolError("Give ros2 arguments, like “topic list”.")
    command = "ros2 " + shlex.join(words)
    setup = ros_setup()
    if setup:
        command = f"source {shlex.quote(setup)} && {command}"
        ws_setup = _cwd(ctx, args) / "install" / "setup.bash"  # a colcon workspace's own packages
        if ws_setup.is_file():
            command = f"source {shlex.quote(setup)} && source {shlex.quote(str(ws_setup))} && ros2 {shlex.join(words)}"
    return execute(ctx, ["bash", "-c", command], _cwd(ctx, args), _timeout(args, 20, 1800))


ARDUINO_READ = {("board", "list"), ("board", "listall"), ("board", "details"), ("board", "search"),
                ("core", "list"), ("core", "search"), ("lib", "list"), ("lib", "search"), ("version",),
                ("sketch", "new")}


def arduino_risk(args: dict) -> str:
    try:
        words = [w for w in _words(args) if w != "arduino-cli"]
    except ToolError:
        return RUN
    if words[:1] == ["compile"] and not any(w in ("-u", "--upload") for w in words):
        return WRITE
    return READ if tuple(words[:2]) in ARDUINO_READ or tuple(words[:1]) in ARDUINO_READ else RUN


def arduino(ctx: ToolContext, args: dict) -> str:
    words = [w for w in _words(args) if w != "arduino-cli"]
    if not words:
        raise ToolError("Give arduino-cli arguments, like “board list”.")
    return execute(ctx, ["arduino-cli", *words], _cwd(ctx, args), _timeout(args, 300, 1800))


# ---- the desktop and the web --------------------------------------------------------------------

def open_item(ctx: ToolContext, args: dict) -> str:
    backend = ctx.backend
    target = (args.get("target") or "").strip()
    if not target:
        raise ToolError("Say what to open.")
    app = (args.get("app") or "").strip().lower()
    if app in ("code", "vscode", "vs code", "visual studio code") and which("code"):
        path = resolve(ctx, target)
        subprocess.Popen(["code", str(path)], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Opened {show(ctx, path)} in VS Code."
    looks_like_path = "/" in target or target.startswith("~") or "." in Path(target).name
    if looks_like_path:
        path = resolve(ctx, target)
        if not path.exists():
            raise ToolError(f"There's no {show(ctx, path)}.")
        if path.is_file() and (os.access(path, os.X_OK) or path.suffix == ".desktop"):
            raise ToolError("Vara doesn't open programs or launchers directly; use run_command.")
        backend.open_path(str(path))
        return f"Opened {show(ctx, path)}."
    from .vara import match_app  # the same matching as "open firefox"

    found = match_app(backend.apps(), target)
    if not found:
        raise ToolError(f"No app called “{target}” is installed. PolyMarket may have it.")
    backend.launch(found["id"])
    return f"Opened {found['name']}."


def list_windows(ctx: ToolContext, args: dict) -> str:
    wins = ctx.backend.windows()
    return "\n".join(f"- {w['title']} ({w.get('appId') or 'unknown app'}){' [active]' if w.get('active') else ''}"
                     for w in wins) or "No windows are open."


def fetch_url(ctx: ToolContext, args: dict) -> str:
    url = (args.get("url") or "").strip()
    if not re.match(r"^https?://[^\s/]+", url):
        raise ToolError("Give an http:// or https:// address.")
    req = urllib.request.Request(url, headers={"User-Agent": "PolyOS-Vara/1.0", "Accept": "text/html,text/plain,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=25) as res:
            kind = res.headers.get_content_type()
            raw = res.read(1_500_000)
    except urllib.error.HTTPError as exc:
        raise ToolError(f"{url} answered {exc.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ToolError(f"Couldn't reach {url}: {getattr(exc, 'reason', exc)}") from None
    text = raw.decode("utf-8", "replace")
    links = page_links(text, url) if kind == "text/html" and args.get("links") else []
    if kind == "text/html":
        text = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", text)
        text = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr|/pre)\b[^>]*>", "\n", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    elif not kind.startswith("text/") and kind not in ("application/json", "application/xml"):
        raise ToolError(f"{url} is {kind}, not a page Vara can read.")
    listed = "\n\nLinks on the page:\n" + "\n".join(f"- {t}: {u}" for t, u in links) if links else ""
    return clip(f"{url}\n\n{text}", 16_000 - min(len(listed), 5000)) + listed[:5000]


def page_links(page: str, base: str, limit: int = 50) -> list[tuple[str, str]]:
    """[(text, absolute address)] of a page's links, for following them."""
    out, seen = [], set()
    for href, label in re.findall(r'(?is)<a\b[^>]*?href="([^"#][^"]*)"[^>]*>(.*?)</a>', page):
        target = urllib.parse.urljoin(base, html.unescape(href))
        text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", label)).split())
        if target.startswith(("http://", "https://")) and text and target not in seen:
            seen.add(target)
            out.append((text[:80], target))
            if len(out) >= limit:
                break
    return out


def research(ctx: ToolContext, args: dict) -> str:
    """Search the web and read the best few pages in one go."""
    query = " ".join(str(args.get("query") or "").split())
    if not query:
        raise ToolError("Say what to research.")
    data = urllib.parse.urlencode({"q": query, "kl": "us-en"}).encode()
    req = urllib.request.Request("https://html.duckduckgo.com/html/", data=data, method="POST",
                                 headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) PolyOS-Vara/1.0",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            results = parse_search_results(res.read(2_000_000).decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ToolError(f"The web search didn't answer ({getattr(exc, 'reason', exc)}).") from None
    pages = int(args.get("pages") or 3) if str(args.get("pages") or "3").isdigit() else 3
    parts = []
    for r in results[:max(1, min(pages, 5))]:
        try:
            parts.append(clip(fetch_url(ctx, {"url": r["url"]}), 3500))
        except ToolError as exc:
            parts.append(f"{r['url']}\n(couldn't read: {exc})")
    if not parts:
        raise ToolError("The search found nothing. Try other words.")
    return "\n\n---\n\n".join(parts) + "\n\nCite the addresses you used."


def search_documents(ctx: ToolContext, args: dict) -> str:
    if ctx.index is None:
        raise ToolError("Searching your documents is turned off (Settings › Vara).")
    found = ctx.index.search(str(args.get("query") or ""))
    if not found:
        stats = ctx.index.stats()
        return "No documents match." + ("" if stats["files"] else " (The index is still being built.)")
    return "\n".join(f"- {show(ctx, Path(f['path']))}: {f['snippet']}" for f in found) + \
        "\n\nRead one with read_document."


def read_document(ctx: ToolContext, args: dict) -> str:
    """Any document as text: PDFs, Word and LibreOffice files, presentations, plain text."""
    from .vara_index import extract
    path = resolve(ctx, args.get("path", ""))
    if not path.is_file():
        raise ToolError(f"There's no file {show(ctx, path)}.")
    text = extract(path)
    if not text.strip():
        raise ToolError(f"{show(ctx, path)} has no text Vara can read (a scan or an unknown format).")
    return clip(f"{show(ctx, path)}\n\n{text}", 20_000)


def parse_search_results(page: str, limit: int = 8) -> list[dict]:
    """DuckDuckGo's HTML results page -> [{title, url, snippet}] (ads left out)."""
    out = []
    for block in re.split(r'<div class="result ', page)[1:]:
        if block.startswith(("result--ad", "results_links_deep result--ad")) or "result--ad" in block[:200]:
            continue
        link = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        href = html.unescape(link.group(1))
        target = re.search(r"[?&]uddg=([^&]+)", href)
        url = urllib.parse.unquote(target.group(1)) if target else ("https:" + href if href.startswith("//") else href)
        if not url.startswith(("http://", "https://")):
            continue
        snippet = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        clean = lambda t: " ".join(html.unescape(re.sub(r"<[^>]+>", "", t)).split())  # noqa: E731
        out.append({"title": clean(link.group(2)), "url": url, "snippet": clean(snippet.group(1)) if snippet else ""})
        if len(out) >= limit:
            break
    return out


def web_search(ctx: ToolContext, args: dict) -> str:
    query = " ".join(str(args.get("query") or "").split())
    if not query:
        raise ToolError("Say what to search for.")
    data = urllib.parse.urlencode({"q": query, "kl": "us-en"}).encode()
    req = urllib.request.Request("https://html.duckduckgo.com/html/", data=data, method="POST",
                                 headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) PolyOS-Vara/1.0",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            page = res.read(2_000_000).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ToolError(f"The web search didn't answer ({getattr(exc, 'reason', exc)}). Check the internet connection.") from None
    results = parse_search_results(page)
    if not results:
        raise ToolError("The search found nothing (or the search service is busy). Try other words, or fetch_url a known site.")
    return "\n\n".join(f"{n}. {r['title']}\n{r['url']}\n{r['snippet']}" for n, r in enumerate(results, 1)) + \
        "\n\nRead a result with fetch_url before relying on it."


# ---- the web browser, by keyboard shortcuts (xdotool) -------------------------------------------------

BROWSER_CLASS = "firefox|Navigator|chromium|google-chrome|brave|microsoft-edge|vivaldi|librewolf"
BROWSER_KEYS = {"back": "alt+Left", "forward": "alt+Right", "reload": "F5", "new_tab": "ctrl+t", "close_tab": "ctrl+w",
                "next_tab": "ctrl+Tab", "previous_tab": "ctrl+shift+Tab", "scroll_down": "Page_Down", "scroll_up": "Page_Up",
                "top": "Home", "bottom": "End", "zoom_in": "ctrl+plus", "zoom_out": "ctrl+minus", "zoom_reset": "ctrl+0"}
BROWSER_ACTIONS = ("open", "search", *BROWSER_KEYS, "find", "read_page", "current")


def _xdotool(*args: str, timeout: float = 10) -> str:
    proc = subprocess.run(["xdotool", *args], capture_output=True, text=True, timeout=timeout)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def browser_window() -> str | None:
    """The browser window to act on: the active one if it's a browser, else the newest browser window."""
    ids = _xdotool("search", "--onlyvisible", "--class", BROWSER_CLASS).split()
    if not ids:
        return None
    active = _xdotool("getactivewindow")
    return active if active in ids else ids[-1]


def _web_address(text: str) -> str:
    text = text.strip()
    if re.match(r"^https?://\S+$", text):
        return text
    if re.match(r"^[\w-]+(\.[\w-]+)+(/\S*)?$", text):  # youtube.com, docs.python.org/3/
        return "https://" + text
    raise ToolError("Give a web address like https://example.com or example.com.")


def _clipboard() -> str:
    proc = subprocess.run(["xclip", "-o", "-selection", "clipboard"], capture_output=True, text=True, timeout=5)
    return proc.stdout if proc.returncode == 0 else ""


def browser(ctx: ToolContext, args: dict) -> str:
    action = str(args.get("action") or "").strip()
    if action not in BROWSER_ACTIONS:
        raise ToolError(f"action is one of: {', '.join(BROWSER_ACTIONS)}.")
    if action in ("open", "search"):
        url = _web_address(str(args.get("url") or "")) if action == "open" else \
            "https://duckduckgo.com/?" + urllib.parse.urlencode({"q": str(args.get("query") or args.get("text") or "")})
        if action == "search" and not (args.get("query") or args.get("text")):
            raise ToolError("Say what to search for.")
        subprocess.Popen(["xdg-open", url], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Opened {url} in the web browser."
    win = browser_window()
    if not win:
        raise ToolError("No web browser window is open. Use action “open” first.")
    title = _xdotool("getwindowname", win)
    if action == "current":
        return f"The browser is showing: {title}"
    _xdotool("windowactivate", "--sync", win)
    if action in BROWSER_KEYS:
        _xdotool("key", "--clearmodifiers", "--window", win, BROWSER_KEYS[action])
        return f"Browser: {action.replace('_', ' ')} (on “{_short(title, 60)}”)."
    if action == "find":
        text = str(args.get("text") or "").strip()
        if not text:
            raise ToolError("Say what to find on the page.")
        _xdotool("key", "--clearmodifiers", "ctrl+f")
        _xdotool("type", "--delay", "8", text[:200])
        _xdotool("key", "Return")
        return f"Looking for “{text[:200]}” on the page."
    # read_page: the address bar's address (restoring the clipboard), then the page itself
    if not which("xclip"):
        raise ToolError("Reading the open page needs xclip (sudo apt install xclip).")
    saved = _clipboard()
    _xdotool("key", "--clearmodifiers", "ctrl+l")
    _xdotool("key", "--clearmodifiers", "ctrl+c")
    _xdotool("key", "Escape")
    time.sleep(0.3)
    url = _clipboard().strip()
    if saved:
        subprocess.run(["xclip", "-selection", "clipboard"], input=saved, text=True, timeout=5, check=False)
    if not re.match(r"^https?://", url):
        raise ToolError(f"Couldn't read the address of “{title}”.")
    return fetch_url(ctx, {"url": url})


# ---- Vara's own web browser (vara_browser.py, Playwright), for real browsing -------------------------

WEB_ACTIONS = ("open", "search", "snapshot", "click", "type", "select", "check", "press", "scroll", "back", "forward",
               "reload", "extract", "tabs", "tab", "new_tab", "close_tab", "screenshot", "wait", "close")
WEB_ACTS = ("click", "type", "select", "check", "press")  # these act on a page (forms, buttons): approval "write"


def web_python() -> Path:
    from .vara_voice import PYTHON
    return Path(os.environ.get("POLYOS_VARA_PYTHON") or PYTHON)


def web_available() -> bool:
    py = web_python()
    return py.exists() and any(py.parent.parent.glob("lib/python3*/site-packages/playwright"))


class WebBrowser:
    """The browser process: started on first use, closed after IDLE seconds without a command."""

    IDLE = 600

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._n = 0
        self._last = 0.0

    def call(self, cmd: dict, timeout: float = 60) -> str:
        import select
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
                self.proc = subprocess.Popen([str(web_python()), "-m", "polyos.vara_browser"], stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env,
                                             start_new_session=True)
                threading.Thread(target=self._idle_watch, daemon=True).start()
            self._n += 1
            self._last = time.monotonic()
            self.proc.stdin.write(json.dumps({**cmd, "id": self._n}) + "\n")
            self.proc.stdin.flush()
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                ready, _, _ = select.select([self.proc.stdout], [], [], 0.5)
                if ready:
                    line = self.proc.stdout.readline()
                    if not line:
                        break
                    reply = json.loads(line)
                    if reply.get("id") != self._n:
                        continue
                    self._last = time.monotonic()
                    if "error" in reply:
                        raise ToolError(f"Browser: {reply['error']}")
                    return reply.get("result", "")
            self.proc.kill()
            self.proc = None
            raise ToolError("The browser stopped answering and was closed. Try again.")

    def close(self) -> None:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                try:
                    self.proc.stdin.write(json.dumps({"action": "quit"}) + "\n")
                    self.proc.stdin.flush()
                    self.proc.wait(10)
                except (OSError, subprocess.TimeoutExpired):
                    self.proc.kill()
            self.proc = None

    def _idle_watch(self) -> None:
        while self.proc is not None and self.proc.poll() is None:
            time.sleep(30)
            if time.monotonic() - self._last > self.IDLE:
                self.close()
                return


WEB = WebBrowser()


def web(ctx: ToolContext, args: dict) -> str:
    action = str(args.get("action") or "").strip()
    if action not in WEB_ACTIONS:
        raise ToolError(f"action is one of: {', '.join(WEB_ACTIONS)}.")
    if action == "close":
        WEB.close()
        return "Closed the browser."
    if action in ("click", "type", "select", "check") and args.get("ref") in (None, ""):
        raise ToolError(f"{action} needs ref: the number of the thing on the page (from the last snapshot).")
    if action == "open" and not str(args.get("url") or "").strip():
        raise ToolError("open needs a url.")
    cmd = {k: args[k] for k in ("url", "query", "ref", "text", "submit", "option", "on", "key", "direction", "index",
                                "full_page", "seconds") if k in args}
    return clip(WEB.call({"action": action, **cmd}, timeout=90), 16_000)


def _web_title(args: dict) -> str:
    """For the step card: "Web: open example.com", "Web: click [4]"."""
    what = args.get("url") or args.get("query") or args.get("text") or (f"[{args['ref']}]" if args.get("ref") is not None else "")
    return f"Web: {args.get('action', '')} {_short(what, 45)}".strip()


def web_risk(args: dict) -> str:
    return WRITE if args.get("action") in WEB_ACTS else READ


# ---- music and videos (MPRIS, through playerctl) ----------------------------------------------------

MEDIA_ACTIONS = ("play", "pause", "play-pause", "next", "previous", "stop", "status")


def media(ctx: ToolContext, args: dict) -> str:
    action = str(args.get("action") or "").strip()
    if action not in MEDIA_ACTIONS:
        raise ToolError(f"action is one of: {', '.join(MEDIA_ACTIONS)}.")
    if action != "status":
        proc = subprocess.run(["playerctl", action], capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            raise ToolError("Nothing is playing (no music or video app is open).")
    status = subprocess.run(["playerctl", "status"], capture_output=True, text=True, timeout=10).stdout.strip()
    what = subprocess.run(["playerctl", "metadata", "--format", "{{artist}} - {{title}}"], capture_output=True, text=True,
                          timeout=10).stdout.strip().strip(" -")
    return f"{status or 'Stopped'}{f': {what}' if what else ''}."


# ---- around the clock: reminders and routines ------------------------------------------------------

def set_reminder(ctx: ToolContext, args: dict) -> str:
    from .vara_schedule import describe, parse_when
    item = ctx.schedule.add("reminder", args.get("text", ""), parse_when(args.get("at"), args.get("in_minutes")),
                            args.get("repeat") or "once")
    return "Set: " + describe(item)


def schedule_routine(ctx: ToolContext, args: dict) -> str:
    from .vara_schedule import describe, parse_when
    item = ctx.schedule.add("routine", args.get("request", ""), parse_when(args.get("at"), args.get("in_minutes")),
                            args.get("repeat") or "once")
    return "Scheduled: " + describe(item)


def list_scheduled(ctx: ToolContext, args: dict) -> str:
    from .vara_schedule import describe
    return "\n".join(describe(i) for i in ctx.schedule.items()) or "No reminders or routines."


def cancel_scheduled(ctx: ToolContext, args: dict) -> str:
    from .vara_schedule import describe
    return "Cancelled: " + describe(ctx.schedule.cancel(str(args.get("id") or "").strip()))


# ---- the plan: a checklist the person watches --------------------------------------------------------

PLAN_STATES = ("pending", "in_progress", "done")


def plan(ctx: ToolContext, args: dict) -> str:
    steps = args.get("steps")
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except ValueError:
            steps = None
    if not isinstance(steps, list) or not steps or len(steps) > 30:
        raise ToolError("steps is a list of up to 30 {step, status} items.")
    clean = []
    for item in steps:
        text = " ".join(str((item or {}).get("step") if isinstance(item, dict) else item).split())[:160]
        status = item.get("status") if isinstance(item, dict) and item.get("status") in PLAN_STATES else "pending"
        if text:
            clean.append({"step": text, "status": status})
    if ctx.plan:
        ctx.plan(clean)
    done = sum(1 for s in clean if s["status"] == "done")
    current = next((s["step"] for s in clean if s["status"] == "in_progress"), None)
    return f"Plan updated: {done} of {len(clean)} done." + (f" Now: {current}" if current else "")


# ---- the tool maker (vara_toolmaker.py) and the expert helper ----------------------------------------

def make_tool(ctx: ToolContext, args: dict) -> str:
    from . import vara_toolmaker
    try:
        spec = vara_toolmaker.make(ctx.home, args.get("name", ""), args.get("description", ""), args.get("params") or {},
                                   args.get("code", ""), set(TOOLS))
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    return (f"Made {spec['name']} in {show(ctx, Path(spec['folder']))}. Test it with test_tool; after a test passes it's "
            f"yours to use as my_{spec['name']}. (open with app “vscode” shows its code to the person.)")


def test_tool(ctx: ToolContext, args: dict) -> str:
    from . import vara_toolmaker
    raw = args.get("args") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise ToolError("args is a JSON object of the tool's arguments.") from None
    try:
        out = vara_toolmaker.run(ctx.home, str(args.get("name", "")), raw, record=True)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    tested = any(t["name"] == args.get("name") and t.get("tested") for t in vara_toolmaker.load(ctx.home))
    return out + ("\n\nThe test passed: the tool is ready as my_" + str(args.get("name")) if tested else
                  "\n\nThe test failed: fix the code with make_tool (same name) and test again.")


def remove_tool(ctx: ToolContext, args: dict) -> str:
    from . import vara_toolmaker
    vara_toolmaker.remove(ctx.home, str(args.get("name", "")))
    return f"Removed the {args.get('name')} tool."


def consult_expert(ctx: ToolContext, args: dict) -> str:
    """Ask the expert helper model (e.g. Claude) for code or a second opinion."""
    from .vara import complete
    if not ctx.expert:
        raise ToolError("No expert helper is set up. The person can add one (e.g. Claude) in Settings › Vara.")
    question = str(args.get("question") or "").strip()
    if not question:
        raise ToolError("Say what to ask.")
    code = str(args.get("code") or "")
    prompt = question + (f"\n\nThe code:\n```\n{code[:30000]}\n```" if code else "")
    try:
        answer = complete(ctx.expert, [{"role": "system", "content": "You are an expert software engineer helping another "
                                        "AI agent. Answer precisely; give complete, working code when asked."},
                                       {"role": "user", "content": prompt}], timeout=180)
    except RuntimeError as exc:
        raise ToolError(f"The expert helper didn't answer: {exc}") from None
    return clip(answer or "(no answer)", 20_000)


def custom_tools(home: Path) -> dict[str, "Tool"]:
    """The tools Vara made and tested, offered as my_<name> (each run asks first, like any program)."""
    from . import vara_toolmaker
    out = {}
    for spec in vara_toolmaker.load(home):
        if not spec.get("tested"):
            continue
        name, params = spec["name"], spec.get("params") or {}

        def runner(ctx, args, _name=name):
            try:
                return vara_toolmaker.run(ctx.home, _name, args)
            except ValueError as exc:
                raise ToolError(str(exc)) from None
        out[vara_toolmaker.PREFIX + name] = Tool(
            vara_toolmaker.PREFIX + name, f"Your tool: {name}", f"(A tool you made) {spec['description']}",
            {k: _p(v) for k, v in params.items()}, [], RUN, runner, lambda a, _n=name: f"Ran your {_n} tool",
            detail=lambda a: json.dumps(a)[:2000], icon="tool")
    return out


# ---- memory and skills --------------------------------------------------------------------------

def remember(ctx: ToolContext, args: dict) -> str:
    ctx.memory.add(args.get("note", ""), args.get("kind") or "fact", args.get("replaces") or "")
    return "Saved to memory." + (" (Replaced the older note.)" if args.get("replaces") else "")


def recall(ctx: ToolContext, args: dict) -> str:
    """Everything remembered that matches, and matching documents on this computer."""
    query = " ".join(str(args.get("query") or "").split())
    if not query:
        raise ToolError("Say what to look for.")
    found = ctx.memory.search(query)
    lines = [f"- ({n.get('kind', 'fact')}, {n.get('added', '')}) {n['note']}" for n in found]
    return "From memory:\n" + "\n".join(lines) if lines else f"Nothing in memory about “{query}”."


def forget(ctx: ToolContext, args: dict) -> str:
    """Forget the notes that contain some text (the person asked Vara to forget something)."""
    about = " ".join(str(args.get("about") or "").split()).casefold()
    if not about:
        raise ToolError("Say what to forget.")
    notes = ctx.memory.notes()
    gone = [n["note"] for n in notes if about in n["note"].casefold()]
    for index in sorted((i for i, n in enumerate(notes) if about in n["note"].casefold()), reverse=True):
        ctx.memory.forget(index)
    return f"Forgot: {'; '.join(gone)}" if gone else f"Nothing in memory mentions “{about}”."


def load_skill(ctx: ToolContext, args: dict) -> str:
    return ctx.skills.load(args.get("name", ""))


def save_skill(ctx: ToolContext, args: dict) -> str:
    path = ctx.skills.save(args.get("name", ""), args.get("description", ""), args.get("instructions", ""))
    return f"Saved the skill to {show(ctx, path)}."


# ---- the list ---------------------------------------------------------------------------------

def _p(desc: str, kind: str = "string") -> dict:
    return {"type": kind, "description": desc}


CWD = _p("Folder to run in (default: the workspace)")
TIMEOUT = _p("Seconds before it's stopped", "integer")


def _when(args: dict) -> str:
    """For the approval card: “daily at 08:00”, “once in 30 min”."""
    at = f"at {args['at']}" if args.get("at") else f"in {args.get('in_minutes')} min"
    return f"{args.get('repeat') or 'once'} {at}"


def _short(text: str, n: int = 70) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


# Vara's tools before 1.2, for every edition. The rest (the web, reminders and routines, documents,
# making tools, the expert helper, plans) are the Developer edition's, as are voice and the HUD.
CLASSIC_TOOLS = frozenset({"list_files", "read_file", "search_files", "write_file", "edit_file", "run_command", "git",
                           "model_info", "openscad", "blender", "ros2", "arduino", "open", "list_windows", "fetch_url",
                           "remember", "load_skill", "save_skill"})


def developer(settings) -> bool:
    """The Developer edition, or developer mode: Vara's 1.2 features (voice, the HUD, tools beyond the classic ones)."""
    try:
        return settings is not None and (settings.get("edition") == "developer" or settings.get("developerMode") is True)
    except Exception:  # noqa: BLE001 - no settings: the classic Vara
        return False


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("plan", "Plan", "Write or update your plan for a task with more than two or three steps: the whole "
         "list each time, each step pending, in_progress or done. The person sees it as a checklist.",
         {"steps": {"type": "array", "description": "The steps, in order",
                    "items": {"type": "object", "properties": {"step": _p("What to do"),
                                                               "status": _p("pending, in_progress or done")},
                              "required": ["step", "status"]}}},
         ["steps"], READ, plan, lambda a: "Updated the plan", icon="check"),
    Tool("list_files", "List a folder", "List a folder's files and subfolders (with sizes).",
         {"path": _p("Folder; relative paths start in the workspace"), "hidden": _p("Include hidden files", "boolean")},
         [], READ, list_files, lambda a: f"Looked in {a['path'] if a.get('path') not in (None, '', '.', './') else 'the workspace'}", icon="folder"),
    Tool("read_file", "Read a file", "Read a text file with line numbers. Long files: use start_line and max_lines.",
         {"path": _p("File path"), "start_line": _p("First line (default 1)", "integer"),
          "max_lines": _p("How many lines (default 400)", "integer")},
         ["path"], READ, read_file, lambda a: f"Read {a.get('path', '')}", icon="folder"),
    Tool("search_files", "Search files", "Search file contents under a folder (regular expression, case-insensitive).",
         {"query": _p("Text or regular expression"), "path": _p("Folder or file (default: the workspace)"),
          "glob": _p("Only file names like this, e.g. *.py")},
         ["query"], READ, search_files, lambda a: f"Searched for “{_short(a.get('query', ''), 40)}”", icon="search"),
    Tool("write_file", "Write a file", "Create or replace a text file (makes missing folders).",
         {"path": _p("File path"), "content": _p("The whole new content")},
         ["path", "content"], WRITE, write_file, lambda a: f"Wrote {a.get('path', '')}",
         detail=lambda a: clip(a.get("content", ""), 3000),
         targets=lambda ctx, a: [resolve(ctx, a.get("path", ""), write=True)], icon="code"),
    Tool("edit_file", "Edit a file", "Replace exact text in a file. old_text must match once (or set replace_all).",
         {"path": _p("File path"), "old_text": _p("Exact text to replace, with enough lines to be unique"),
          "new_text": _p("Replacement"), "replace_all": _p("Replace every match", "boolean")},
         ["path", "old_text", "new_text"], WRITE, edit_file, lambda a: f"Edited {a.get('path', '')}",
         detail=lambda a: clip(f"- {a.get('old_text', '')}\n+ {a.get('new_text', '')}", 3000),
         targets=lambda ctx, a: [resolve(ctx, a.get("path", ""), write=True)], icon="code"),
    Tool("run_command", "Run a command", "Run a bash command (as the person, not root) and get its output and exit "
         "code. For builds, tests, package managers (pip, npm, cargo, colcon), scripts. No interactive programs.",
         {"command": _p("The command"), "cwd": CWD, "timeout": TIMEOUT},
         ["command"], RUN, run_command, lambda a: f"Ran {_short(a.get('command', ''), 50)}",
         detail=lambda a: a.get("command", ""), icon="terminal"),
    Tool("git", "Use git", "Run git. status, diff, log and show just look; other commands need approval.",
         {"args": _p("Arguments, e.g. “status” or “commit -m 'Add gripper'”"), "cwd": CWD},
         ["args"], git_risk, git, lambda a: f"git {_short(a.get('args', ''), 50)}",
         detail=lambda a: f"git {a.get('args', '')}", needs=lambda: which("git"), icon="code"),
    Tool("model_info", "Measure a 3D model", "Size (bounding box) and triangle count of an .stl or .obj model.",
         {"path": _p("Model file")}, ["path"], READ, model_info, lambda a: f"Measured {a.get('path', '')}", icon="cube"),
    Tool("openscad", "Render with OpenSCAD", "Render an OpenSCAD model to .stl/.3mf (for printing) or .png (a "
         "preview). Pass code (saved next to the output as .scad) or an existing .scad file. Units are millimeters.",
         {"output": _p("Output file, e.g. parts/bracket.stl"), "code": _p("OpenSCAD source"),
          "file": _p("An existing .scad file instead of code"), "timeout": TIMEOUT},
         ["output"], WRITE, openscad, lambda a: f"Rendered {a.get('output', '')} with OpenSCAD",
         detail=lambda a: clip(a.get("code") or a.get("file", ""), 3000),
         targets=lambda ctx, a: [resolve(ctx, a.get("output", ""), write=True),
                                 *([resolve(ctx, a["output"], write=True).with_suffix(".scad")] if a.get("code") else [])],
         needs=lambda: which("openscad"), icon="cube"),
    Tool("blender", "Run a Blender script", "Run a Python (bpy) script in Blender without its window: build or "
         "change scenes, import/export models (STL, OBJ, glTF, FBX), render images. Save results with "
         "bpy.ops.wm.save_as_mainfile or an export operator; print() what you need to see.",
         {"script": _p("Python using bpy"), "blend_file": _p("A .blend file to open first (optional)"), "timeout": TIMEOUT},
         ["script"], RUN, blender, lambda a: "Ran a Blender script", detail=lambda a: a.get("script", ""),
         needs=lambda: which("blender"), icon="brush"),
    Tool("ros2", "Use ROS 2", "Run the ros2 command line (the newest /opt/ros setup and the workspace's install/ are "
         "sourced). topic/node/service/param list, info, echo and interface show just look; run, launch, "
         "topic pub, service call and param set move real robots and need approval. Use --once with topic echo.",
         {"args": _p("Arguments, e.g. “topic list” or “topic echo /odom --once”"), "cwd": CWD, "timeout": TIMEOUT},
         ["args"], ros_risk, ros2, lambda a: f"ros2 {_short(a.get('args', ''), 50)}",
         detail=lambda a: f"ros2 {a.get('args', '')}", needs=ros_available, icon="robot"),
    Tool("arduino", "Use arduino-cli", "Run arduino-cli: board list, compile, upload, lib/core install. Uploading "
         "and installing need approval.",
         {"args": _p("Arguments, e.g. “compile --fqbn arduino:avr:uno blink”"), "cwd": CWD, "timeout": TIMEOUT},
         ["args"], arduino_risk, arduino, lambda a: f"arduino-cli {_short(a.get('args', ''), 45)}",
         detail=lambda a: f"arduino-cli {a.get('args', '')}", needs=lambda: which("arduino-cli"), icon="chip"),
    Tool("open", "Open an app or file", "Open an installed app by name, or a file or folder in its usual app. "
         "Set app to “vscode” to open a folder or file in VS Code.",
         {"target": _p("App name, or a file or folder path"), "app": _p("Optional: “vscode”")},
         ["target"], READ, open_item, lambda a: f"Opened {_short(a.get('target', ''), 50)}", icon="external"),
    Tool("list_windows", "List open windows", "The windows open on the desktop right now.",
         {}, [], READ, list_windows, lambda a: "Checked the open windows", icon="window"),
    Tool("fetch_url", "Read a web page", "Download a web page or text file (docs, datasheets, READMEs) as plain text. "
         "Set links to also get the page's links, to follow them (advanced browsing).",
         {"url": _p("http(s) address"), "links": _p("Also list the page's links", "boolean")}, ["url"], READ, fetch_url,
         lambda a: f"Read {_short(a.get('url', ''), 55)}", icon="globe"),
    Tool("research", "Research", "Search the web and read the top pages in one step (default 3, up to 5). Best for "
         "questions that need current or detailed information; cite the addresses you used.",
         {"query": _p("What to research"), "pages": _p("How many pages to read (1-5)", "integer")}, ["query"], READ,
         research, lambda a: f"Researched “{_short(a.get('query', ''), 40)}”", icon="search"),
    Tool("search_documents", "Search your documents", "Search the words in the person's documents and files "
         "(Documents, Desktop, Downloads, Projects, the workspace: text, code, PDFs, Word and LibreOffice files, "
         "slides). Returns paths and matching snippets.",
         {"query": _p("Words to look for")}, ["query"], READ, search_documents,
         lambda a: f"Searched your files for “{_short(a.get('query', ''), 40)}”", icon="folder"),
    Tool("read_document", "Read a document", "Read a document as text: PDF, .docx, .odt, .pptx, .odp, .rtf or any "
         "text file.", {"path": _p("File path")}, ["path"], READ, read_document,
         lambda a: f"Read {a.get('path', '')}", icon="folder"),
    Tool("remember", "Remember", "Save a short lasting note about the person: a preference (how they like answers, "
         "units, favorite apps, music, sites, work hours), a fact (their board, printer, pets' names), a project, a "
         "person they mention, or a habit. Save preferences you notice without asking. When something changed, pass "
         "replaces with words from the old note. Never passwords, keys or one-off details.",
         {"note": _p("The note, one sentence"), "kind": _p("preference, fact, project, person or habit"),
          "replaces": _p("Optional: words from an older note this one replaces")}, ["note"], READ, remember,
         lambda a: f"Remembered: {_short(a.get('note', ''), 50)}", icon="star"),
    Tool("recall", "Recall", "Search everything you remember about the person (more than the notes shown to you).",
         {"query": _p("What to look for")}, ["query"], READ, recall,
         lambda a: f"Recalled {_short(a.get('query', ''), 40)}", icon="star"),
    Tool("forget", "Forget", "Remove notes from memory that mention some text, when the person asks you to forget "
         "something.", {"about": _p("Text the notes to forget contain")}, ["about"], READ, forget,
         lambda a: f"Forgot notes about {_short(a.get('about', ''), 40)}", icon="star"),
    Tool("web_search", "Search the web", "Search the web (DuckDuckGo) for current information, news, docs or "
         "answers. Returns titles, addresses and snippets; read a result with fetch_url before relying on it.",
         {"query": _p("What to search for")}, ["query"], READ, web_search,
         lambda a: f"Searched the web for “{_short(a.get('query', ''), 40)}”", icon="search"),
    Tool("web", "Browse the web", "Your own web browser (a real one, in a window the person can watch): read pages, "
         "click, fill in forms, log in when asked, compare, shop, book. Every action returns the page as text with "
         "numbered things to use; act by number. Actions: open (url), search (query), snapshot, click (ref), type (ref, "
         "text, submit), select (ref, option), check (ref, on), press (key, e.g. Enter), scroll (direction up/down), "
         "back, forward, reload, extract (all the text), tabs, tab (index), new_tab (url), close_tab, screenshot, "
         "wait (seconds), close. Numbers change when the page changes: use the latest snapshot.",
         {"action": _p("One of: " + ", ".join(WEB_ACTIONS)), "url": _p("For open / new_tab"), "query": _p("For search"),
          "ref": _p("The number of the thing on the page", "integer"), "text": _p("For type"),
          "submit": _p("For type: press Enter after", "boolean"), "option": _p("For select: the option's text"),
          "on": _p("For check: true or false", "boolean"), "key": _p("For press"), "direction": _p("For scroll: up or down"),
          "index": _p("For tab", "integer"), "full_page": _p("For screenshot", "boolean"), "seconds": _p("For wait", "number")},
         ["action"], web_risk, web,
         _web_title,
         detail=lambda a: json.dumps({k: v for k, v in a.items() if k != "action"})[:1000],
         needs=web_available, icon="globe"),
    Tool("browser", "Use the web browser", "Control the web browser on screen: open an address or a search in a "
         "new tab; back, forward, reload, new_tab, close_tab, next_tab, previous_tab, scroll_down, scroll_up, top, "
         "bottom, zoom_in, zoom_out, zoom_reset; find text on the page; current (the page title); read_page (the "
         "open page as text).",
         {"action": _p("One of: " + ", ".join(BROWSER_ACTIONS)), "url": _p("For open: the address"),
          "query": _p("For search: what to search for"), "text": _p("For find: the text")},
         ["action"], READ, browser, lambda a: f"Browser: {a.get('action', '').replace('_', ' ')} {_short(a.get('url') or a.get('query') or a.get('text') or '', 40)}".strip(),
         needs=lambda: which("xdotool"), icon="globe"),
    Tool("media", "Control music and video", "Play, pause, skip or check what's playing in any music or video app "
         "(Spotify, the browser, VLC...).", {"action": _p("One of: " + ", ".join(MEDIA_ACTIONS))}, ["action"], READ,
         media, lambda a: f"Media: {a.get('action', '')}", needs=lambda: which("playerctl"), icon="music"),
    Tool("set_reminder", "Set a reminder", "Remind the person at a time (shown, and spoken if Vara Voice is on). "
         "Use the local time from “Now” to work out at, or in_minutes for “in 10 minutes”.",
         {"text": _p("What to remind them about"), "at": _p("“YYYY-MM-DD HH:MM” or “HH:MM” (24-hour, local)"),
          "in_minutes": _p("Or: minutes from now", "number"), "repeat": _p("once (default), hourly, daily, weekdays or weekly")},
         ["text"], READ, set_reminder, lambda a: f"Reminder: {_short(a.get('text', ''), 50)}", icon="clock"),
    Tool("schedule_routine", "Schedule a routine", "Run a request on your own later or on repeat, e.g. every "
         "weekday at 08:00 “summarize today's weather and news”. You'll do it with your tools and the person "
         "sees and hears the answer.",
         {"request": _p("The request, as the person would ask it"), "at": _p("“YYYY-MM-DD HH:MM” or “HH:MM”"),
          "in_minutes": _p("Or: minutes from now", "number"), "repeat": _p("once, hourly, daily, weekdays or weekly")},
         ["request"], WRITE, schedule_routine, lambda a: f"Routine: {_short(a.get('request', ''), 50)}",
         detail=lambda a: _when(a) + f": {a.get('request', '')}",
         icon="clock"),
    Tool("list_scheduled", "List reminders and routines", "Every reminder and routine with its id and next time.",
         {}, [], READ, list_scheduled, lambda a: "Checked reminders and routines", icon="clock"),
    Tool("cancel_scheduled", "Cancel a reminder or routine", "Cancel a reminder or routine by its id (list_scheduled).",
         {"id": _p("The id")}, ["id"], READ, cancel_scheduled, lambda a: f"Cancelled {a.get('id', '')}", icon="clock"),
    Tool("make_tool", "Make a tool", "Make yourself a new tool when no tool fits and the job will come up again (a "
         "weather lookup, a converter, a home-automation call...). Write Python: main.py reads its arguments as JSON "
         "from stdin and prints the result. Then test it with test_tool; after a passing test it's yours as my_<name>. "
         "Same name again replaces it (to fix it).",
         {"name": _p("lowercase_with_underscores"), "description": _p("One sentence: what it does and when to use it"),
          "params": _p("Its arguments, as an object: {\"city\": \"Which city\"}", "object"), "code": _p("main.py")},
         ["name", "description", "code"], WRITE, make_tool, lambda a: f"Made the {a.get('name', '')} tool",
         detail=lambda a: clip(a.get("code", ""), 3000), icon="tool"),
    Tool("test_tool", "Test a tool", "Run a tool you made with sample arguments, to see that it works.",
         {"name": _p("The tool's name (without my_)"), "args": _p("Arguments, as an object", "object")},
         ["name"], RUN, test_tool, lambda a: f"Tested the {a.get('name', '')} tool",
         detail=lambda a: json.dumps(a.get("args") or {})[:2000], icon="tool"),
    Tool("remove_tool", "Remove a tool", "Delete a tool you made.", {"name": _p("The tool's name (without my_)")},
         ["name"], WRITE, remove_tool, lambda a: f"Removed the {a.get('name', '')} tool", icon="tool"),
    Tool("consult_expert", "Ask the expert helper", "Ask your expert helper model (set in Settings › Vara, e.g. "
         "Claude) to write or debug code, or for a second opinion on something hard. Include the code.",
         {"question": _p("What to ask"), "code": _p("Code it should look at (optional)")}, ["question"], READ,
         consult_expert, lambda a: f"Asked the expert: {_short(a.get('question', ''), 45)}", icon="sparkle"),
    Tool("load_skill", "Load a skill", "Read one of your skills (step-by-step know-how) before a task it covers.",
         {"name": _p("Skill name from the list")}, ["name"], READ, load_skill,
         lambda a: f"Loaded the {a.get('name', '')} skill", icon="sparkle"),
    Tool("save_skill", "Save a skill", "Save a reusable procedure you worked out as a skill, when the person agrees.",
         {"name": _p("lowercase-with-dashes"), "description": _p("One line: when to use it"),
          "instructions": _p("Markdown steps")},
         ["name", "description", "instructions"], WRITE, save_skill,
         lambda a: f"Saved the {a.get('name', '')} skill", detail=lambda a: clip(a.get("instructions", ""), 3000),
         targets=lambda ctx, a: [ctx.skills.user_dir], icon="sparkle"),
]}


# Programs worth mentioning to the model (installed or not), for coding, 3D and robotics work.
PROGRAMS = [("python3", "Python"), ("git", "git"), ("code", "VS Code"), ("node", "Node.js"), ("gcc", "gcc"),
            ("cargo", "Rust"), ("blender", "Blender"), ("openscad", "OpenSCAD"), ("freecad", "FreeCAD"),
            ("freecadcmd", "FreeCAD (command line)"), ("kicad-cli", "KiCad"), ("prusa-slicer", "PrusaSlicer"),
            ("arduino-cli", "arduino-cli"), ("platformio", "PlatformIO"), ("ros2", "ROS 2"), ("docker", "Docker")]


def installed_programs() -> tuple[list[str], list[str]]:
    have, missing = [], []
    for program, label in PROGRAMS:
        found = which(program) or (program == "ros2" and ros_setup())
        (have if found else missing).append(label)
    return have, missing
