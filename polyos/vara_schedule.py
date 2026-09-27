"""Vara around the clock: reminders ("remind me at 5 to call Sam") and routines, requests Vara runs on
its own at a set time ("every weekday at 8, tell me the weather and my first meeting").

Both live in ~/.config/polyos/vara-schedule.json. The shell checks every 20 seconds: a reminder is
shown (and spoken, when Vara Voice is on); a routine runs like a request typed into Vara, and its
answer is shown and spoken the same way. Times are the computer's local time.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from .core import ApiError

KINDS = ("reminder", "routine")
REPEATS = ("once", "hourly", "daily", "weekdays", "weekly")
MAX_ITEMS = 100
MAX_TEXT = 500


def parse_when(at: str | None, in_minutes, now: datetime.datetime | None = None) -> datetime.datetime:
    """"2026-09-27 17:30", "17:30" (the next time it's 17:30) or in_minutes -> a local datetime."""
    now = now or datetime.datetime.now()
    if in_minutes not in (None, ""):
        try:
            minutes = float(in_minutes)
        except (TypeError, ValueError):
            raise ApiError("in_minutes must be a number.") from None
        if not 0 < minutes <= 60 * 24 * 366:
            raise ApiError("Pick a time within the next year.")
        return now + datetime.timedelta(minutes=minutes)
    text = str(at or "").strip().replace("T", " ")
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2} )?(\d{1,2}):(\d{2})(?::\d{2})?", text)
    if not m:
        raise ApiError("Give the time as “YYYY-MM-DD HH:MM”, “HH:MM” or in_minutes.")
    hour, minute = int(m.group(2)), int(m.group(3))
    if hour > 23 or minute > 59:
        raise ApiError("That isn't a time of day.")
    if m.group(1):
        try:
            day = datetime.date.fromisoformat(m.group(1).strip())
        except ValueError:
            raise ApiError("That date doesn't exist.") from None
        when = datetime.datetime.combine(day, datetime.time(hour, minute))
        if when <= now - datetime.timedelta(minutes=1):
            raise ApiError("That time has already passed.")
        return when
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return when if when > now else when + datetime.timedelta(days=1)


def next_time(when: datetime.datetime, repeat: str, now: datetime.datetime) -> datetime.datetime | None:
    """The next time a repeating item is due, after now (None for "once")."""
    step = {"hourly": datetime.timedelta(hours=1), "daily": datetime.timedelta(days=1),
            "weekly": datetime.timedelta(days=7), "weekdays": datetime.timedelta(days=1)}.get(repeat)
    if step is None:
        return None
    nxt = when
    while nxt <= now or (repeat == "weekdays" and nxt.weekday() >= 5):
        nxt += step
    return nxt


def describe(item: dict) -> str:
    when = datetime.datetime.fromtimestamp(item["at"])
    repeat = {"once": "", "hourly": ", every hour", "daily": ", every day", "weekdays": ", every weekday",
              "weekly": ", every week"}[item["repeat"]]
    return f"[{item['id']}] {item['kind']} {when:%a %Y-%m-%d %H:%M}{repeat}: {item['text']}"


class Schedule:
    def __init__(self, path: Path, clock=time.time):
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()

    def items(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return []
        return [i for i in data if isinstance(i, dict) and i.get("kind") in KINDS and isinstance(i.get("at"), (int, float))] \
            if isinstance(data, list) else []

    def _write(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(sorted(items, key=lambda i: i["at"]), fh, indent=1)
        os.replace(tmp, self.path)

    def add(self, kind: str, text: str, when: datetime.datetime, repeat: str = "once") -> dict:
        text = " ".join(str(text or "").split())[:MAX_TEXT]
        if kind not in KINDS:
            raise ApiError("Choose a reminder or a routine.")
        if not text:
            raise ApiError("Say what the reminder is for." if kind == "reminder" else "Say what Vara should do.")
        if repeat not in REPEATS:
            raise ApiError(f"repeat is one of: {', '.join(REPEATS)}.")
        with self._lock:
            items = self.items()
            if len(items) >= MAX_ITEMS:
                raise ApiError(f"Vara keeps up to {MAX_ITEMS} reminders and routines. Cancel some first.")
            item = {"id": uuid.uuid4().hex[:6], "kind": kind, "text": text, "at": when.timestamp(), "repeat": repeat}
            self._write([*items, item])
        return item

    def cancel(self, item_id: str) -> dict:
        with self._lock:
            items = self.items()
            found = next((i for i in items if i["id"] == item_id), None)
            if found is None:
                raise ApiError(f"There's no reminder or routine {item_id}.")
            self._write([i for i in items if i["id"] != item_id])
        return found

    def due(self) -> list[dict]:
        """What's due now; repeating items move on to their next time, the rest are removed."""
        now_ts = self.clock()
        with self._lock:
            items = self.items()
            due = [i for i in items if i["at"] <= now_ts]
            if not due:
                return []
            now = datetime.datetime.fromtimestamp(now_ts)
            keep = [i for i in items if i["at"] > now_ts]
            for item in due:
                nxt = next_time(datetime.datetime.fromtimestamp(item["at"]), item.get("repeat", "once"), now)
                if nxt is not None:
                    keep.append({**item, "at": nxt.timestamp()})
            self._write(keep)
        # something missed by more than a day (the computer was off) isn't worth announcing
        return [i for i in due if now_ts - i["at"] < 24 * 3600]
