"""Vara's skills and memory.

Skills are Markdown how-tos Vara reads when a task calls for one ("make a part in OpenSCAD",
"flash an Arduino"): a name and a one-line description in a front-matter block, then the steps.
PolyOS ships some in /usr/share/polyos/vara/skills; people (and Vara, when asked) add their own in
~/.config/polyos/vara/skills, as `name.md` or `name/SKILL.md` (the Agent Skills layout), and a
skill there replaces a built-in one of the same name. Only the names and descriptions go into
every conversation; Vara loads a skill's full text when it needs it.

Memory is a short list of lasting notes ("uses an Arduino Nano", "projects live in ~/robots")
that Vara sees in every conversation. Settings > Vara shows them and forgets them.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import threading
from pathlib import Path

from .core import ApiError

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
MAX_SKILL_BYTES = 32_000
MAX_NOTES = 250
CONTEXT_NOTES = 60  # how many go to the model with every request (all preferences first); recall finds the rest
KINDS = ("preference", "fact", "project", "person", "habit")
MAX_NOTE_CHARS = 300


def _front_matter(text: str) -> tuple[dict, str]:
    """`---\\nname: x\\ndescription: y\\n---\\nbody` -> ({name, description}, body)."""
    meta: dict[str, str] = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                key, sep, value = line.partition(":")
                if sep and key.strip() in ("name", "description"):
                    meta[key.strip()] = value.strip().strip("\"'")
            text = text[end + 4:].lstrip("\n")
    return meta, text


class Skills:
    def __init__(self, builtin_dir: Path, user_dir: Path):
        self.builtin_dir = builtin_dir
        self.user_dir = user_dir

    def _files(self) -> dict[str, tuple[Path, bool]]:
        found: dict[str, tuple[Path, bool]] = {}
        for folder, own in ((self.builtin_dir, False), (self.user_dir, True)):
            if not folder.is_dir():
                continue
            for path in sorted([*folder.glob("*.md"), *folder.glob("*/SKILL.md")]):
                name = path.parent.name if path.name == "SKILL.md" else path.stem
                if NAME.match(name):
                    found[name] = (path, own)  # the person's own skill wins
        return found

    def list(self) -> list[dict]:
        skills = []
        for name, (path, own) in sorted(self._files().items()):
            try:
                meta, _body = _front_matter(path.read_text("utf-8")[:4000])
            except (OSError, UnicodeDecodeError):
                continue
            skills.append({"name": name, "description": meta.get("description", "")[:300], "own": own})
        return skills

    def load(self, name: str) -> str:
        entry = self._files().get(name.strip().lower())
        if entry is None:
            names = ", ".join(s["name"] for s in self.list()) or "none"
            raise ApiError(f"There's no skill called “{name}”. Skills: {names}.")
        _meta, body = _front_matter(entry[0].read_text("utf-8")[:MAX_SKILL_BYTES])
        return body

    def save(self, name: str, description: str, body: str) -> Path:
        name = name.strip().lower()
        if not NAME.match(name):
            raise ApiError("A skill name uses lowercase letters, digits and dashes, like “blender-render”.")
        description = " ".join(description.split())[:300]
        if not description or not body.strip():
            raise ApiError("A skill needs a description and instructions.")
        if len(body.encode()) > MAX_SKILL_BYTES:
            raise ApiError("That skill is too long; keep it under 32 KB.")
        path = self.user_dir / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body.strip()}\n", "utf-8")
        return path


class Memory:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def notes(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return []
        return [n for n in data if isinstance(n, dict) and isinstance(n.get("note"), str)] if isinstance(data, list) else []

    def _write(self, notes: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(notes, fh, indent=1)

    def add(self, note: str, kind: str = "fact", replaces: str = "") -> list[dict]:
        """Save a note; `replaces` drops older notes containing that text (a preference that changed)."""
        note = " ".join(note.split())[:MAX_NOTE_CHARS]
        if not note:
            raise ApiError("There's nothing to remember.")
        kind = kind if kind in KINDS else "fact"
        old = " ".join(str(replaces or "").split()).casefold()
        with self._lock:
            notes = [n for n in self.notes() if n["note"].casefold() != note.casefold()
                     and not (old and old in n["note"].casefold())]
            notes.append({"note": note, "kind": kind, "added": datetime.date.today().isoformat()})
            keep = [n for n in notes if n.get("kind") == "preference"][-MAX_NOTES // 2:]  # preferences last longest
            others = [n for n in notes if n.get("kind") != "preference"][-(MAX_NOTES - len(keep)):]
            self._write([n for n in notes if n in keep or n in others])
            return self.notes()

    def for_context(self) -> list[dict]:
        """Every preference, then the newest other notes, up to CONTEXT_NOTES."""
        notes = self.notes()
        prefs = [n for n in notes if n.get("kind") == "preference"]
        rest = [n for n in notes if n.get("kind") != "preference"]
        room = max(0, CONTEXT_NOTES - len(prefs))
        return prefs[-CONTEXT_NOTES:] + (rest[-room:] if room else [])

    def search(self, query: str, limit: int = 12) -> list[dict]:
        """Notes sharing the most words with the query (recall)."""
        words = {w for w in re.findall(r"[a-z0-9]+", query.casefold()) if len(w) > 2}
        scored = []
        for n in self.notes():
            text = n["note"].casefold()
            score = sum(1 for w in words if w in text)
            if score:
                scored.append((score, n))
        return [n for _s, n in sorted(scored, key=lambda x: -x[0])[:limit]]

    def forget(self, index: int | None = None) -> list[dict]:
        with self._lock:
            notes = self.notes()
            if index is None:
                notes = []
            elif 0 <= index < len(notes):
                notes.pop(index)
            else:
                raise ApiError("That note doesn't exist.", 404)
            self._write(notes)
            return notes


class Habits:
    """Which apps the person opens, and at what time of day, so Vara can adapt (all local; kept only while
    Settings keeps activity history)."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def opened(self, name: str, when: datetime.datetime | None = None) -> None:
        when = when or datetime.datetime.now()
        with self._lock:
            data = self.load()
            item = data.get(name) if isinstance(data.get(name), dict) else {"count": 0, "hours": [0] * 24}
            item["count"] = int(item.get("count", 0)) + 1
            hours = item.get("hours") if isinstance(item.get("hours"), list) and len(item["hours"]) == 24 else [0] * 24
            hours[when.hour] += 1
            item["hours"], item["last"] = hours, when.date().isoformat()
            data[name] = item
            if len(data) > 200:  # the least used go
                data = dict(sorted(data.items(), key=lambda kv: -kv[1].get("count", 0))[:150])
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data), "utf-8")

    def clear(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)


def _part_of_day(hours: list[int]) -> str:
    spans = {"mornings": range(5, 12), "afternoons": range(12, 17), "evenings": range(17, 22), "late at night": [22, 23, 0, 1, 2, 3, 4]}
    totals = {name: sum(hours[h] for h in span) for name, span in spans.items()}
    best = max(totals, key=totals.get)
    return best if totals[best] >= 0.5 * max(1, sum(hours)) else "throughout the day"


def habit_summary(habits: Habits, top: int = 6) -> list[str]:
    data = habits.load()
    ranked = sorted(((k, v) for k, v in data.items() if isinstance(v, dict) and v.get("count", 0) >= 3),
                    key=lambda kv: -kv[1]["count"])[:top]
    return [f"- Opens {name} often ({item['count']} times), mostly {_part_of_day(item.get('hours') or [0] * 24)}"
            for name, item in ranked]
