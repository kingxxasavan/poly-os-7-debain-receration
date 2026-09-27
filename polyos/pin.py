"""PIN sign-in: 4 to 6 digits for the sign-in and lock screens, like Windows Hello's PIN.

The password stays the key to everything that changes the system: sudo, pkexec, installing apps
and updates never accept the PIN (their PAM services don't include it).

Only an scrypt hash of the PIN is kept, in /var/lib/polyos/pin/<user> (root only), with the
count of wrong tries: after MAX_FAILS the PIN stops working until the password is used once.
Two ways in, both run as root so the hash and the count are out of the person's reach:

  polyos-pin check      pam_exec for the sign-in screen (/etc/pam.d/polyos-login): the PIN on stdin
  polyos-pin serve      polyos-pin.socket: the lock screen asks here; the caller is known from the
                        socket (SO_PEERCRED), so a session can only try its own PIN
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import pwd
import re
import secrets
import socket
import struct
import sys
import time
from pathlib import Path

STORE = Path("/var/lib/polyos/pin")
SOCKET = "/run/polyos-pin.sock"
PIN_RE = re.compile(r"^\d{4,6}$")
MAX_FAILS = 5
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def valid(pin: str) -> bool:
    return isinstance(pin, str) and bool(PIN_RE.match(pin))


def make_record(pin: str) -> dict:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, dklen=32, **SCRYPT)
    return {"v": 1, "salt": salt.hex(), "hash": digest.hex(), **SCRYPT, "fails": 0, "created": int(time.time())}


def _path(user: str, store: Path) -> Path:
    if not USER_RE.match(user or ""):
        raise ValueError("bad user name")
    return store / user


def load(user: str, store: Path = STORE) -> dict | None:
    try:
        record = json.loads(_path(user, store).read_text("utf-8"))
        return record if isinstance(record, dict) else None
    except (OSError, ValueError):
        return None


def save(user: str, record: dict | None, store: Path = STORE) -> None:
    """Write (or with None, remove) a person's PIN record; root only."""
    path = _path(user, store)
    if record is None:
        path.unlink(missing_ok=True)
        return
    store.mkdir(parents=True, exist_ok=True)
    os.chmod(store, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(record, fh)
    os.replace(tmp, path)


def status(user: str, store: Path = STORE) -> dict:
    record = load(user, store)
    return {"set": record is not None, "blocked": bool(record and record.get("fails", 0) >= MAX_FAILS)}


def verify(user: str, pin: str, store: Path = STORE) -> bool:
    """Check a PIN and keep count of wrong ones (the record is updated either way)."""
    record = load(user, store)
    if record is None or not valid(pin) or record.get("fails", 0) >= MAX_FAILS:
        return False
    try:
        digest = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(record["salt"]), dklen=32,
                                n=int(record["n"]), r=int(record["r"]), p=int(record["p"]))
        ok = hmac.compare_digest(digest.hex(), record["hash"])
    except (KeyError, ValueError, TypeError):
        return False
    record["fails"] = 0 if ok else record.get("fails", 0) + 1
    save(user, record, store)
    if not ok:
        time.sleep(1)  # slows guessing down further
    return ok


def reset_fails(user: str, store: Path = STORE) -> None:
    """After the password was used: the PIN works again."""
    record = load(user, store)
    if record and record.get("fails"):
        record["fails"] = 0
        save(user, record, store)


# ---- the root service for the lock screen ------------------------------------------------------
def _peer_user(conn: socket.socket) -> str | None:
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


def handle(request: dict, peer: str | None, store: Path = STORE) -> dict:
    """One request from a session: its own PIN only. The login screen (lightdm) may ask whether a
    user has a PIN, to show the PIN field."""
    op = request.get("op")
    if op == "status":
        user = request.get("user") if peer in ("lightdm", "root") and isinstance(request.get("user"), str) else peer
        return status(user, store) if user and USER_RE.match(user) else {"set": False, "blocked": False}
    if not peer or peer == "root":
        return {"ok": False}
    if op == "verify":
        return {"ok": verify(peer, str(request.get("pin") or ""), store)}
    if op == "reset":  # the session signed in or unlocked with the password
        reset_fails(peer, store)
        return {"ok": True}
    return {"ok": False, "error": "unknown request"}


def serve(listener: socket.socket, store: Path = STORE, idle: float = 300) -> None:
    listener.settimeout(idle)
    while True:
        try:
            conn, _ = listener.accept()
        except socket.timeout:
            return  # socket activation starts it again when needed
        with conn:
            conn.settimeout(10)
            try:
                data = conn.recv(512)
                reply = handle(json.loads(data.decode() or "{}"), _peer_user(conn), store)
            except (OSError, ValueError, UnicodeDecodeError):
                reply = {"ok": False}
            try:
                conn.sendall((json.dumps(reply) + "\n").encode())
            except OSError:
                pass


def ask(request: dict, path: str = SOCKET, timeout: float = 10) -> dict:
    """From a session (the lock screen): talk to the PIN service."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall(json.dumps(request).encode())
        return json.loads(s.makefile().readline() or "{}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if os.geteuid() != 0:
        print("polyos-pin runs as root", file=sys.stderr)
        return 1
    if argv[:1] == ["check"]:  # pam_exec expose_authtok: the typed secret on stdin, the user in PAM_USER
        secret = sys.stdin.buffer.read(64).split(b"\0", 1)[0].decode(errors="ignore").strip()
        user = os.environ.get("PAM_USER", "")
        if not valid(secret) or not USER_RE.match(user) or not status(user)["set"]:
            return 1  # not a PIN: the password check that follows handles it
        return 0 if verify(user, secret) else 1
    if argv[:1] == ["serve"]:
        fds = int(os.environ.get("LISTEN_FDS", "0") or 0)
        if fds < 1:
            print("polyos-pin serve needs polyos-pin.socket", file=sys.stderr)
            return 1
        serve(socket.socket(fileno=3))
        return 0
    print("usage: polyos-pin check|serve", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
