"""Online updates: new PolyOS versions install on the computer, without a new ISO, account or not.

Every release carries PolyOS's own packages (polyos-shell, polyos-desktop, ...), polyos-update.json
listing them with their SHA-256 checksums, and polyos-update.json.sig, an Ed25519 signature of
that manifest made with Poly's release key (GitHub Actions holds the private half):

    {"version": "0.9.0", "published": "...", "packages": [{"name": "polyos-shell",
     "file": "polyos-shell_0.9.0_all.deb", "sha256": "...", "size": 123456}, ...]}

Where to look: the Poly update server (the website's /api/v1/updates/check), which is told only
the version, channel and architecture. If the website can't be reached, the stable channel falls
back to the release files themselves, so the updater never depends on the account service.

Nothing installs unless the manifest's signature checks out against a key built into PolyOS, and
every package matches its name, version and checksum. That holds even if the website or the
download host were compromised. Checking the signature uses the openssl command.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__, __editions__

SERVER = "https://poly-os-7.vercel.app"  # the website: downloads, updates, Poly Account
RELEASES_REPO = "kingxxasavan/poly-os-7-debain-receration"  # public release files (fallback when the website is down)
MANIFEST = "polyos-update.json"
SIGNATURE = "polyos-update.json.sig"
CHANNELS = ("stable", "beta", "developer")
EDITIONS = ("regular", "developer", "gaming")
EDITION_PATH = Path("/etc/polyos/edition")  # written by the installer
USER_AGENT = f"PolyOS/{__version__}"
PACKAGE_FILE = re.compile(r"^(polyos-[a-z]+)_(\d+\.\d+\.\d+)_all\.deb$")
CACHE = Path("/var/cache/polyos-update")
MAX_BYTES = 200 * 1024 * 1024

# Poly's release keys (Ed25519). A release is only installed when its manifest is signed by one of
# these; a new key can be added here one release before the old one retires.
TRUSTED_KEYS = [
    "-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEAFpI6g731nViDu+oWUvMa1vvI4VPYZbtrNV9R0nXpxNk=\n-----END PUBLIC KEY-----\n",
]


def server() -> str:
    """The update server: /etc/polyos/server (one line) or $POLYOS_SERVER overrides the built-in one."""
    value = os.environ.get("POLYOS_SERVER", "")
    if not value:
        try:
            value = Path("/etc/polyos/server").read_text("utf-8").strip()
        except OSError:
            value = ""
    return (value or SERVER).rstrip("/")


def local_edition(home_root: Path = Path("/home"), edition_path: Path | None = None) -> str:
    """This computer's edition, for edition updates: Developer if anyone here uses the Developer edition
    or developer mode, else Gaming if anyone uses it, else the one the installer wrote, else Regular."""
    found = set()
    try:
        homes = [p for p in home_root.iterdir() if p.is_dir()]
    except OSError:
        homes = []
    for home in homes + [Path.home()]:
        try:
            s = json.loads((home / ".config/polyos/settings.json").read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(s, dict):
            if s.get("edition") in EDITIONS:
                found.add(s["edition"])
            if s.get("developerMode") is True:
                found.add("developer")
    for edition in ("developer", "gaming"):
        if edition in found:
            return edition
    try:
        written = (edition_path or EDITION_PATH).read_text("utf-8").strip()
        if written in EDITIONS:
            return written
    except OSError:
        pass
    return "regular"


def version_tuple(v: str) -> tuple[int, ...]:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def newer(latest: str, current: str = __version__) -> bool:
    return version_tuple(latest) > version_tuple(current)


def _get(url: str, timeout: float = 20, accept: str = "application/json") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https addresses only
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("download too large")
    return data


# ---- signatures --------------------------------------------------------------------------------
def verify(manifest: bytes, signature: bytes, keys: list[str] | None = None) -> bool:
    """True when one of Poly's keys signed exactly these manifest bytes."""
    keys = TRUSTED_KEYS if keys is None else keys
    if len(signature) != 64:
        return False
    with tempfile.TemporaryDirectory(prefix="polyos-verify-") as tmp:
        data, sig = Path(tmp) / "manifest", Path(tmp) / "sig"
        data.write_bytes(manifest)
        sig.write_bytes(signature)
        for n, pem in enumerate(keys):
            key = Path(tmp) / f"key{n}.pem"
            key.write_text(pem, "utf-8")
            try:
                proc = subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(key), "-rawin",
                                       "-in", str(data), "-sigfile", str(sig)], capture_output=True, timeout=20)
            except (OSError, subprocess.TimeoutExpired):
                return False
            if proc.returncode == 0:
                return True
    return False


def sign(manifest: bytes, private_key_pem: str) -> bytes:
    """For the release workflow: the Ed25519 signature of a manifest."""
    with tempfile.TemporaryDirectory(prefix="polyos-sign-") as tmp:
        key, data, sig = Path(tmp) / "key.pem", Path(tmp) / "manifest", Path(tmp) / "sig"
        key.touch(mode=0o600)
        key.write_text(private_key_pem if private_key_pem.endswith("\n") else private_key_pem + "\n", "utf-8")
        data.write_bytes(manifest)
        subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(key), "-rawin", "-in", str(data), "-out", str(sig)],
                       check=True, capture_output=True, timeout=20)
        return sig.read_bytes()


# ---- finding the newest release ------------------------------------------------------------------
def _base(url: str) -> str:
    return url.rsplit("/", 1)[0] + "/"


def find(channel: str = "stable", get=_get, edition: str | None = None) -> dict:
    """{version, notes, published, manifest, signature}: from the update server, or the release files."""
    channel = channel if channel in CHANNELS else "stable"
    query = urllib.parse.urlencode({"channel": channel, "version": __version__, "arch": os.uname().machine,
                                    "edition": edition or local_edition()})
    try:
        info = json.loads(get(f"{server()}/api/v1/updates/check?{query}"))
        if info.get("version"):
            return {"version": info["version"], "notes": info.get("notes") or "", "published": info.get("published") or "",
                    "manifest": info.get("manifest"), "signature": info.get("signature")}
    except (OSError, ValueError):
        if channel != "stable":
            raise
    base = f"https://github.com/{RELEASES_REPO}/releases/latest/download/"
    manifest = json.loads(get(base + MANIFEST))
    return {"version": str(manifest.get("version", "")), "notes": manifest.get("notes") or "",
            "published": manifest.get("published") or "", "manifest": base + MANIFEST, "signature": base + SIGNATURE}


def validate_manifest(manifest: dict, base: str) -> list[dict]:
    """The packages to download: known names, the manifest's version and a checksum each."""
    version = manifest.get("version")
    packages = manifest.get("packages")
    if not isinstance(version, str) or not isinstance(packages, list) or not packages:
        raise ValueError("The update's package list is damaged.")
    out = []
    for item in packages:
        file = item.get("file") if isinstance(item, dict) else None
        m = PACKAGE_FILE.match(file or "")
        if not m or m.group(2) != version or m.group(1) != item.get("name"):
            raise ValueError(f"Unexpected package in the update: {file}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))):
            raise ValueError(f"No checksum for {file}.")
        out.append({"name": item["name"], "file": file, "sha256": item["sha256"], "url": base + file,
                    "size": int(item.get("size") or 0)})
    return out


def signed_manifest(found: dict, get=_get, edition: str | None = None) -> tuple[dict, list[dict]]:
    """The manifest, once its signature is checked, and its packages (only if it's for this edition)."""
    if not found.get("manifest"):
        raise ValueError("This release can only be installed from its ISO.")
    if not found.get("signature"):
        raise ValueError("This release isn’t signed, so PolyOS won’t install it. Install it from its ISO.")
    raw = get(found["manifest"])
    try:
        sig = get(found["signature"], accept="application/octet-stream")
        if len(sig) != 64:  # raw 64 bytes, or the same in base64
            sig = base64.b64decode(sig.strip(), validate=True)
    except (OSError, ValueError):
        raise ValueError("This release isn’t signed, so PolyOS won’t install it. Install it from its ISO.") from None
    if not verify(raw, sig):
        raise ValueError("This update’s signature doesn’t match Poly’s release key, so it wasn’t installed.")
    manifest = json.loads(raw)
    if str(manifest.get("version")) != found["version"]:
        raise ValueError("This update’s package list is for a different version.")
    editions = [e for e in manifest.get("editions") or [] if e in EDITIONS]
    mine = edition or local_edition()
    if editions and mine not in editions:  # signed, so this can't be faked by the server
        raise ValueError(f"PolyOS {found['version']} is an update for the {' and '.join(e.title() for e in editions)} "
                         f"edition only.")
    return manifest, validate_manifest(manifest, _base(found["manifest"]))


def check(channel: str = "stable", get=_get, edition: str | None = None) -> dict:
    """{current, latest, available, notes, size, packages} for Settings > Updates."""
    edition = edition or local_edition()
    found = find(channel, get, edition)
    out = {"current": __version__, "latest": found["version"], "notes": found["notes"], "published": found["published"],
           "channel": channel, "available": False, "size": 0}
    if newer(found["version"]):
        try:
            _manifest, packages = signed_manifest(found, get, edition)
            out.update(available=True, size=sum(p["size"] for p in packages), packages=[p["name"] for p in packages])
        except ValueError as exc:
            out["reason"] = str(exc)
    return out


def download(emit, channel: str = "stable", get=_get, cache: Path = CACHE,
             edition: str | None = None) -> tuple[str, list[Path]]:
    """(version, verified .deb files), for polyos-admin. Raises ValueError with a readable message."""
    edition = edition or local_edition()
    found = find(channel, get, edition)
    if not newer(found["version"]):
        raise ValueError("PolyOS is already up to date.")
    _manifest, packages = signed_manifest(found, get, edition)
    cache.mkdir(parents=True, exist_ok=True)
    have = {p.name for p in cache.glob("*.deb")}
    for old in cache.glob("*.deb"):
        if old.name not in {p["file"] for p in packages}:
            old.unlink()
    files = []
    for n, pkg in enumerate(packages):
        path = cache / pkg["file"]
        if pkg["file"] in have and hashlib.sha256(path.read_bytes()).hexdigest() == pkg["sha256"]:
            files.append(path)  # downloaded earlier (automatic download), still intact
            continue
        emit({"progress": 0.05 + 0.4 * n / len(packages), "message": f"Downloading {pkg['name']}…"})
        data = get(pkg["url"], timeout=120, accept="application/octet-stream")
        if hashlib.sha256(data).hexdigest() != pkg["sha256"]:
            raise ValueError(f"{pkg['file']} didn't download correctly (checksum mismatch). Try again.")
        path.write_bytes(data)
        path.chmod(0o644)
        files.append(path)
    return found["version"], files


def build_manifest(version: str, debs: list[Path], notes: str = "", editions=()) -> dict:
    """What the release workflow publishes as polyos-update.json (editions: [] = everyone)."""
    items = []
    for deb in sorted(debs):
        m = PACKAGE_FILE.match(deb.name)
        if not m:
            continue
        items.append({"name": m.group(1), "file": deb.name, "size": deb.stat().st_size,
                      "sha256": hashlib.sha256(deb.read_bytes()).hexdigest()})
    return {"version": version, "published": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "notes": notes, "editions": [e for e in editions if e in EDITIONS], "packages": items}
