"""Running polyos-admin as root, from the user's session.

polyos-admin is started through sudo. On the live USB the PolyOS user may use sudo without
a password; on an installed system the setup account is in the "sudo" group, and the UI
asks for the password in a PolyOS dialog. PolyOS keeps it in memory for a few minutes (so a
series of installs asks only once) and hands it to each polyos-admin run (sudo -S -k); sudo
itself never caches it, so no other program started from this session (an app, a command
Vara runs) can use sudo without asking. Locking the screen forgets it.

Long operations run as jobs: polyos-admin prints one JSON object per line
({"progress": 0.4, "message": "..."}), and every update is published to the UI as a
"job" event.
"""

from __future__ import annotations

import itertools
import json
import logging
import subprocess
import threading
import time
from typing import Callable

from . import paths
from .core import ApiError, EventBus

log = logging.getLogger("polyos.privileged")


class NeedPassword(ApiError):
    def __init__(self):
        super().__init__("Enter your password to continue.", 401)


def admin_argv(args: list[str]) -> list[str]:
    helper = paths.ADMIN
    base = ["python3", str(helper)] if paths.IN_REPO else [str(helper)]
    return [*base, *args]


def _sudo_error(stderr: str) -> ApiError:
    text = stderr.lower()
    if "password is required" in text or "a terminal is required" in text:
        return NeedPassword()
    if "incorrect password" in text or "sorry, try again" in text:
        return ApiError("That password isn't right. Try again.", 403)
    if "not in the sudoers" in text or "not allowed to" in text:
        return ApiError("Your account isn't an administrator, so it can't install software.", 403)
    return ApiError(stderr.strip().splitlines()[-1] if stderr.strip() else "Couldn't get administrator access.", 500)


class Admin:
    REMEMBER = 300  # seconds the password is kept (in this process only)

    def __init__(self):
        self._password: str | None = None
        self._until = 0.0

    def _secret(self) -> str | None:
        if self._password is not None and time.monotonic() < self._until:
            return self._password
        self._password = None
        return None

    def forget(self) -> None:
        """The screen locked (or the person signed out): ask again next time."""
        self._password, self._until = None, 0.0

    def ready(self) -> bool:
        """True when polyos-admin can run without asking (live USB, or a password entered recently)."""
        if self._secret() is not None:
            return True
        try:  # -k: a sudo ticket from somewhere else doesn't count
            return subprocess.run(["sudo", "-n", "-k", "true"], capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def authenticate(self, password: str) -> None:
        try:
            proc = subprocess.run(["sudo", "-S", "-k", "-p", "", "true"], input=password + "\n", capture_output=True,
                                  text=True, timeout=30)
        except FileNotFoundError:
            raise ApiError("sudo is not installed.", 500) from None
        except subprocess.TimeoutExpired:
            raise ApiError("Checking the password took too long.", 500) from None
        if proc.returncode != 0:
            raise _sudo_error(proc.stderr)
        self._password, self._until = password, time.monotonic() + self.REMEMBER

    def stream(self, args: list[str], on_event: Callable[[dict], None], cancel: threading.Event | None = None) -> int:
        """Run polyos-admin as root, calling on_event for each JSON line. Returns the exit code."""
        secret = self._secret()
        argv = (["sudo", "-S", "-k", "-p", "", "--"] if secret is not None else ["sudo", "-n", "-k", "--"]) + admin_argv(args)
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE if secret is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        except FileNotFoundError:
            raise ApiError("sudo is not installed.", 500) from None
        if secret is not None:
            try:
                proc.stdin.write(secret + "\n")
                proc.stdin.close()  # polyos-admin itself reads nothing from stdin
            except (BrokenPipeError, OSError):
                pass
        stderr_tail: list[str] = []

        def drain():
            for line in proc.stderr:
                stderr_tail.append(line)
                del stderr_tail[:-40]

        threading.Thread(target=drain, daemon=True).start()
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                event = {"log": line}
            if isinstance(event, dict):
                on_event(event)
            if cancel is not None and cancel.is_set():
                proc.terminate()
                break
        rc = proc.wait()
        err = "".join(stderr_tail)
        if rc != 0 and "sudo:" in err:
            raise _sudo_error(err)
        if rc != 0 and err.strip():
            log.warning("polyos-admin %s: %s", args[0], err.strip()[-2000:])
        return rc

    def call(self, args: list[str], timeout: float = 300) -> dict:
        """A short polyos-admin command that returns {"result": ...}."""
        result: dict = {}
        errors: list[str] = []

        def on_event(event):
            if "result" in event:
                result.update(event)
            if "error" in event:
                errors.append(event["error"])

        rc = self.stream(args, on_event)
        if errors:
            raise ApiError(errors[-1], 500)
        if rc != 0 or "result" not in result:
            raise ApiError("The PolyOS helper failed. Details are in the system log.", 500)
        return result["result"]


class Jobs:
    """Background root operations, one at a time, reported as "job" events."""

    def __init__(self, bus: EventBus, admin: Admin | None = None):
        self.bus = bus
        self.admin = admin or Admin()
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._queue: list[tuple] = []  # (job, args, on_done, runner) waiting for the running job

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(j) for j in self._jobs.values()]

    def running(self) -> dict | None:
        with self._lock:
            return next((dict(j) for j in self._jobs.values() if j["state"] == "running"), None)

    def _publish(self, job: dict) -> None:
        self.bus.publish("job", job=dict(job))

    def start(self, kind: str, title: str, args: list[str], target: str | None = None,
              on_done: Callable[[dict], None] | None = None, runner: Callable | None = None, queue: bool = False) -> dict:
        """runner(job, update) replaces polyos-admin (the dev mock uses it). With queue, a job asked for while
        another runs waits its turn ("queued") instead of being refused (PolyMarket: install several apps)."""
        with self._lock:
            busy = any(j["state"] == "running" for j in self._jobs.values())
            if busy and not queue:
                raise ApiError("Another installation is still running. Wait for it to finish.", 409)
            same = next((j for j in self._jobs.values() if target and j["state"] in ("running", "queued")
                         and j["kind"] == kind and j["target"] == target), None)
            if same is not None:
                return dict(same)  # asked twice: it's already on its way
            job = {"id": str(next(self._ids)), "kind": kind, "title": title, "target": target,
                   "state": "queued" if busy else "running", "progress": 0.0,
                   "message": "Waiting for the others to finish…" if busy else "Starting…", "error": None,
                   "restart": False, "started": time.time()}
            self._jobs = {k: v for k, v in self._jobs.items()
                          if v["state"] in ("running", "queued") or time.time() - v["started"] < 3600}
            self._jobs[job["id"]] = job
        if runner is None and not self.admin.ready():
            with self._lock:
                self._jobs.pop(job["id"], None)
            raise NeedPassword()
        if busy:
            with self._lock:
                self._queue.append((job, args, on_done, runner))
            self._publish(job)
            return dict(job)
        self._publish(job)
        self._run(job, args, on_done, runner)
        return dict(job)

    def cancel(self, job_id: str) -> dict:
        """Take a queued job out of the queue (one that's running finishes)."""
        with self._lock:
            item = next((q for q in self._queue if q[0]["id"] == job_id), None)
            if item is None:
                raise ApiError("Only a job that hasn't started can be cancelled.", 409)
            self._queue.remove(item)
            job = item[0]
            job.update(state="cancelled", message="Cancelled.")
            snapshot = dict(job)
        self._publish(snapshot)
        return snapshot

    def _next(self) -> None:
        with self._lock:
            if not self._queue or any(j["state"] == "running" for j in self._jobs.values()):
                return
            job, args, on_done, runner = self._queue.pop(0)
            job.update(state="running", message="Starting…", started=time.time())
        if runner is None and not self.admin.ready():  # the password ran out while it waited
            with self._lock:
                job.update(state="failed", error="Enter your password again to install it.")
            self._publish(job)
            return self._next()
        self._publish(job)
        self._run(job, args, on_done, runner)

    def _run(self, job: dict, args: list[str], on_done, runner) -> None:
        title, kind = job["title"], job["kind"]

        def update(event: dict):
            with self._lock:
                if "progress" in event and isinstance(event["progress"], (int, float)):
                    job["progress"] = max(0.0, min(1.0, float(event["progress"])))
                if isinstance(event.get("message"), str):
                    job["message"] = event["message"][:300]
                if event.get("restart"):
                    job["restart"] = True
                if isinstance(event.get("error"), str):
                    job["error"] = event["error"][:600]
                snapshot = dict(job)
            if "log" not in event or len(event) > 1:
                self.bus.publish("job", job=snapshot)

        def work():
            try:
                rc = runner(job, update) if runner else self.admin.stream(args, update)
                failed = rc != 0 or job["error"]
            except ApiError as exc:
                job["error"], failed = str(exc), True
            except Exception as exc:  # noqa: BLE001 - reported to the UI instead of crashing the shell
                log.exception("job %s failed", title)
                job["error"], failed = f"Unexpected error: {exc}", True
            with self._lock:
                job["state"] = "failed" if failed else "done"
                if failed and not job["error"]:
                    job["error"] = "It didn't finish. Check your internet connection and try again."
                if not failed:
                    job["progress"] = 1.0
                snapshot = dict(job)
            self.bus.publish("job", job=snapshot)
            if on_done:
                try:
                    on_done(snapshot)
                except Exception:  # noqa: BLE001
                    log.exception("job callback failed")

        def work_then_next():
            work()
            self._next()

        threading.Thread(target=work_then_next, name=f"job-{kind}", daemon=True).start()
