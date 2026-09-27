"""Vara's tool maker: Vara writes new tools for itself, tests them, and then uses them like its own.

A tool is a folder in ~/.config/polyos/vara/tools/<name>/ with
  tool.json   {"name", "description", "params": {name: description}, "required": [...], "tested": bool}
  main.py     reads its arguments as JSON on stdin and prints the result
Vara makes one with make_tool (you approve writing it), runs it with test_tool (you approve running
it) until it works, and after a passing test it's offered to Vara as my_<name>. Every run of a
made tool is a "run" action, so it asks first like any program unless you've said otherwise. You
can open a tool's folder in VS Code, change it, or delete it in Settings > Vara.

For harder code Vara can ask a second model, the "expert helper" set in Settings > Vara (Claude, or
any OpenAI-compatible model), with consult_expert.
"""

from __future__ import annotations

import datetime
import json
import py_compile
import re
import shutil
import subprocess
import sys
from pathlib import Path

NAME = re.compile(r"^[a-z][a-z0-9_]{2,30}$")
PREFIX = "my_"
RUN_TIMEOUT = 120
MAX_CODE = 60_000


def tools_dir(home: Path) -> Path:
    return home / ".config" / "polyos" / "vara" / "tools"


def load(home: Path) -> list[dict]:
    out = []
    folder = tools_dir(home)
    for spec_path in sorted(folder.glob("*/tool.json")) if folder.is_dir() else []:
        try:
            spec = json.loads(spec_path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(spec, dict) and NAME.match(str(spec.get("name", ""))) and (spec_path.parent / "main.py").is_file():
            out.append({**spec, "folder": str(spec_path.parent)})
    return out


def make(home: Path, name: str, description: str, params, code: str, builtin: set[str]) -> dict:
    name = str(name or "").strip().lower()
    if not NAME.match(name):
        raise ValueError("A tool name is 3 to 31 lowercase letters, digits or _ and starts with a letter.")
    if name in builtin or PREFIX + name in builtin:
        raise ValueError(f"“{name}” is one of Vara's own tools. Pick another name.")
    description = " ".join(str(description or "").split())[:400]
    if not description:
        raise ValueError("Describe what the tool does, in one sentence.")
    if isinstance(params, str):
        try:
            params = json.loads(params) if params.strip() else {}
        except ValueError:
            raise ValueError("params is an object like {\"city\": \"Which city\"}.") from None
    if not isinstance(params, dict) or not all(isinstance(k, str) and re.fullmatch(r"[a-z_][a-z0-9_]{0,30}", k) for k in params):
        raise ValueError("params is an object like {\"city\": \"Which city\"} (lowercase names).")
    if not isinstance(code, str) or not code.strip() or len(code) > MAX_CODE:
        raise ValueError("Give the tool's Python code (main.py).")
    folder = tools_dir(home) / name
    folder.mkdir(parents=True, exist_ok=True)
    main = folder / "main.py"
    main.write_text(code if code.endswith("\n") else code + "\n", "utf-8")
    try:
        py_compile.compile(str(main), doraise=True, cfile=str(folder / ".check.pyc"))
    except py_compile.PyCompileError as exc:
        raise ValueError(f"The code doesn't compile: {exc.msg.strip().splitlines()[-1]}") from None
    finally:
        (folder / ".check.pyc").unlink(missing_ok=True)
    spec = {"name": name, "description": description, "params": {k: str(v)[:200] for k, v in params.items()},
            "required": [], "tested": False, "created": datetime.date.today().isoformat()}
    (folder / "tool.json").write_text(json.dumps(spec, indent=2) + "\n", "utf-8")
    return {**spec, "folder": str(folder)}


def run(home: Path, name: str, args: dict, timeout: float = RUN_TIMEOUT, record: bool = False) -> str:
    """Run a made tool with its arguments; `record` marks it tested when it succeeds."""
    folder = tools_dir(home) / str(name)
    spec_path = folder / "tool.json"
    if not NAME.match(str(name)) or not spec_path.is_file():
        raise ValueError(f"There's no tool called {name}.")
    try:
        proc = subprocess.run([sys.executable, "main.py"], cwd=folder, input=json.dumps(args or {}), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ValueError(f"{name} took longer than {int(timeout)} seconds and was stopped.") from None
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if record:
        spec = json.loads(spec_path.read_text("utf-8"))
        spec["tested"] = proc.returncode == 0
        spec_path.write_text(json.dumps(spec, indent=2) + "\n", "utf-8")
    if proc.returncode != 0:
        return f"Exit code {proc.returncode}.\n{out[-4000:]}\n{err[-4000:]}".strip()
    return (out[-8000:] or "(no output)") + (f"\n(stderr: {err[-1500:]})" if err else "")


def remove(home: Path, name: str) -> None:
    if not NAME.match(str(name)):
        raise ValueError("That isn't a tool name.")
    shutil.rmtree(tools_dir(home) / name, ignore_errors=True)
