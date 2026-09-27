"""Filesystem operations for the PolyOS Files app.

Pure standard library, so the real shell and the dev mock (which points it at a sandbox
home folder) share every code path. Deleting always goes through the freedesktop.org
Trash (~/.local/share/Trash), which other Linux file managers understand too.
"""

from __future__ import annotations

import datetime
import mimetypes
import os
import re
import shutil
import stat
import urllib.parse
from pathlib import Path

from .core import ApiError

KINDS = {  # mime prefix / exact type -> icon category used by the UI
    "image/": "image", "video/": "video", "audio/": "audio", "text/": "text", "font/": "font",
    "application/pdf": "pdf",
    "application/zip": "archive", "application/x-tar": "archive", "application/gzip": "archive",
    "application/x-7z-compressed": "archive", "application/x-rar-compressed": "archive",
    "application/x-xz": "archive", "application/x-bzip2": "archive", "application/vnd.debian.binary-package": "package",
    "application/json": "code", "application/javascript": "code", "text/x-python": "code", "application/x-sh": "code",
    "application/msword": "doc", "application/vnd.oasis.opendocument.text": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "doc",
    "application/vnd.ms-excel": "sheet", "application/vnd.oasis.opendocument.spreadsheet": "sheet",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "sheet",
    "application/vnd.ms-powerpoint": "slides", "application/vnd.oasis.opendocument.presentation": "slides",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "slides",
    "application/x-iso9660-image": "disc",
}
CODE_SUFFIXES = {".py", ".js", ".ts", ".c", ".h", ".cpp", ".rs", ".go", ".java", ".sh", ".css", ".html",
                 ".json", ".yml", ".yaml", ".toml", ".xml", ".md", ".sql", ".lua", ".rb", ".php"}
KIND_BY_SUFFIX = {  # common types some systems' mime tables miss
    ".docx": "doc", ".odt": "doc", ".rtf": "doc", ".xlsx": "sheet", ".ods": "sheet", ".csv": "sheet",
    ".pptx": "slides", ".odp": "slides", ".iso": "disc", ".img": "disc", ".deb": "package",
    ".7z": "archive", ".rar": "archive", ".xz": "archive", ".zst": "archive", ".sb3": "archive",
    ".mkv": "video", ".webm": "video", ".flac": "audio", ".ogg": "audio", ".opus": "audio", ".webp": "image",
    ".jfif": "image", ".avif": "image", ".heic": "image",
}
BAD_NAME = re.compile(r"[/\x00]")
USER_DIRS = [("desktop", "Desktop"), ("documents", "Documents"), ("download", "Downloads"),
             ("music", "Music"), ("pictures", "Pictures"), ("videos", "Videos")]
SEARCH_LIMIT = 300


def _friendly(exc: OSError, action: str) -> ApiError:
    if isinstance(exc, PermissionError):
        return ApiError(f"You don't have permission to {action}.", 403)
    if isinstance(exc, FileExistsError):
        return ApiError(f"Can't {action}: something with that name already exists.", 409)
    if isinstance(exc, FileNotFoundError):
        return ApiError(f"Can't {action}: it no longer exists.", 404)
    if getattr(exc, "errno", None) == 28:
        return ApiError(f"Can't {action}: the disk is full.", 507)
    return ApiError(f"Can't {action}: {exc.strerror or exc}", 500)


def P(path: Path | str) -> str:
    """Path as a string with "/" separators (native on Linux; keeps the dev mock sane on Windows)."""
    return Path(path).as_posix() if os.name == "nt" else str(path)


def kind_of(path: Path, is_dir: bool) -> tuple[str, str]:
    """(mime type, icon category) for a file, from its name."""
    if is_dir:
        return "inode/directory", "folder"
    mime = mimetypes.guess_type(path.name)[0] or ""
    suffix = path.suffix.lower()
    if suffix in CODE_SUFFIXES:
        return mime or "text/plain", "code"
    if suffix in KIND_BY_SUFFIX:
        return mime or "application/octet-stream", KIND_BY_SUFFIX[suffix]
    if mime in KINDS:
        return mime, KINDS[mime]
    for prefix, kind in KINDS.items():
        if prefix.endswith("/") and mime.startswith(prefix):
            return mime, kind
    return mime or "application/octet-stream", "file"


def check_name(name: str) -> str:
    name = (name or "").strip()
    if not name or name in (".", "..") or BAD_NAME.search(name) or len(name.encode()) > 255:
        raise ApiError("That name isn't allowed. Names can't be empty or contain “/”.")
    return name


def unique_path(folder: Path, name: str) -> Path:
    """folder/name, or 'name (2).ext', 'name (3).ext', ... if it is taken."""
    candidate = folder / name
    if not os.path.lexists(candidate):
        return candidate
    stem, suffix = os.path.splitext(name)
    if stem.endswith(".tar"):  # keep "archive.tar.gz" together
        stem, suffix = stem[:-4], ".tar" + suffix
    n = 2
    while os.path.lexists(folder / f"{stem} ({n}){suffix}"):
        n += 1
    return folder / f"{stem} ({n}){suffix}"


class FileSystem:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.trash = self.home / ".local/share/Trash"

    # ---- places -------------------------------------------------------------------------
    def user_dirs(self) -> dict[str, Path]:
        dirs = {}
        config = self.home / ".config/user-dirs.dirs"
        try:
            for line in config.read_text("utf-8").splitlines():
                m = re.match(r'XDG_(\w+)_DIR="(.*)"', line.strip())
                if m:
                    dirs[m.group(1).lower()] = Path(m.group(2).replace("$HOME", str(self.home)))
        except OSError:
            pass
        for key, default in USER_DIRS:
            dirs.setdefault(key, self.home / default)
        return dirs

    def places(self) -> dict:
        dirs = self.user_dirs()
        names = {"desktop": "Desktop", "documents": "Documents", "download": "Downloads",
                 "music": "Music", "pictures": "Pictures", "videos": "Videos"}
        places = [{"id": "home", "name": "Home", "path": P(self.home), "icon": "home"}]
        for key, label in names.items():
            path = dirs.get(key)
            if path and path.is_dir() and path != self.home:
                places.append({"id": key, "name": label, "path": P(path), "icon": key})
        return {"places": places, "devices": self.devices(), "home": P(self.home)}

    def devices(self) -> list[dict]:
        out = [{"id": "root", "name": "Computer", "path": "/", "icon": "drive"}] if os.name == "posix" else []
        try:
            lines = Path("/proc/mounts").read_text().splitlines()
        except OSError:
            return out
        user = self.home.name
        for line in lines:
            parts = line.split()
            if len(parts) < 3:
                continue
            mount = parts[1].replace("\\040", " ")
            if mount.startswith((f"/media/{user}/", f"/run/media/{user}/", "/mnt/")) and os.path.isdir(mount):
                out.append({"id": mount, "name": os.path.basename(mount), "path": mount, "icon": "usb"})
        return out

    # ---- listing ------------------------------------------------------------------------
    def entry(self, path: Path, st: os.stat_result | None = None) -> dict:
        try:
            lst = st or path.lstat()
        except OSError as exc:
            raise _friendly(exc, f"read “{path.name}”") from None
        is_link = stat.S_ISLNK(lst.st_mode)
        try:
            real = path.stat() if is_link else lst
        except OSError:
            real = lst  # broken link
        is_dir = stat.S_ISDIR(real.st_mode)
        mime, kind = kind_of(path, is_dir)
        return {
            "name": path.name or str(path),
            "path": P(path),
            "dir": is_dir,
            "link": is_link,
            "size": 0 if is_dir else real.st_size,
            "mtime": int(real.st_mtime),
            "mime": mime,
            "kind": kind,
            "hidden": path.name.startswith("."),
            "exec": not is_dir and bool(real.st_mode & 0o111),
        }

    def resolve(self, path: str | None) -> Path:
        if not path:
            return self.home
        p = Path(os.path.expanduser(path))
        if not p.is_absolute():
            raise ApiError("Paths must be absolute.")
        return p

    def list(self, path: str | None, hidden: bool = False) -> dict:
        folder = self.resolve(path)
        if not folder.is_dir():
            raise ApiError(f"“{folder}” isn't a folder.", 404)
        entries = []
        try:
            with os.scandir(folder) as it:
                for de in it:
                    if not hidden and de.name.startswith("."):
                        continue
                    try:
                        entries.append(self.entry(Path(de.path), de.stat(follow_symlinks=False)))
                    except ApiError:
                        continue
        except OSError as exc:
            raise _friendly(exc, f"open “{folder.name or folder}”") from None
        parent = P(folder.parent) if folder.parent != folder else None
        return {"path": P(folder), "parent": parent, "entries": entries,
                "writable": os.access(folder, os.W_OK), "inTrash": False}

    def search(self, path: str | None, query: str, hidden: bool = False) -> dict:
        root = self.resolve(path)
        q = query.strip().casefold()
        if not q:
            return self.list(path, hidden)
        found = []
        for dirpath, dirnames, filenames in os.walk(root):
            if not hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in dirnames + filenames:
                if (hidden or not name.startswith(".")) and q in name.casefold():
                    try:
                        found.append(self.entry(Path(dirpath) / name))
                    except ApiError:
                        continue
                    if len(found) >= SEARCH_LIMIT:
                        return {"path": P(root), "parent": None, "entries": found, "writable": False, "truncated": True}
        return {"path": P(root), "parent": None, "entries": found, "writable": False, "truncated": False}

    def info(self, path: str) -> dict:
        p = self.resolve(path)
        data = self.entry(p)
        if data["dir"]:
            total, count = 0, 0
            for dirpath, _dirs, files in os.walk(p):
                for name in files:
                    try:
                        total += os.lstat(os.path.join(dirpath, name)).st_size
                    except OSError:
                        pass
                    count += 1
                if count > 50000:  # don't stall on huge trees
                    break
            data.update(size=total, files=count)
        st = p.lstat()
        data.update(location=P(p.parent), mode=stat.filemode(st.st_mode),
                    writable=os.access(p, os.W_OK))
        return data

    # ---- changes ------------------------------------------------------------------------
    def mkdir(self, parent: str, name: str) -> dict:
        target = unique_path(self.resolve(parent), check_name(name))
        try:
            target.mkdir()
        except OSError as exc:
            raise _friendly(exc, "create the folder") from None
        return self.entry(target)

    def new_file(self, parent: str, name: str) -> dict:
        target = unique_path(self.resolve(parent), check_name(name))
        try:
            with open(target, "x"):
                pass
        except OSError as exc:
            raise _friendly(exc, "create the file") from None
        return self.entry(target)

    def rename(self, path: str, name: str) -> dict:
        src = self.resolve(path)
        dst = src.with_name(check_name(name))
        if dst == src:
            return self.entry(src)
        if os.path.lexists(dst):
            raise ApiError(f"“{dst.name}” already exists here.", 409)
        try:
            src.rename(dst)
        except OSError as exc:
            raise _friendly(exc, f"rename “{src.name}”") from None
        return self.entry(dst)

    def _transfer(self, sources: list[str], dest: str, move: bool) -> list[dict]:
        folder = self.resolve(dest)
        if not folder.is_dir():
            raise ApiError("Pick a folder to paste into.")
        done = []
        for raw in sources:
            src = self.resolve(raw)
            if src.is_dir() and (folder == src or src in folder.parents):
                raise ApiError(f"Can't put “{src.name}” inside itself.")
            if move and src.parent == folder:
                continue
            target = unique_path(folder, src.name)
            verb = "move" if move else "copy"
            try:
                if move:
                    shutil.move(str(src), str(target))
                elif src.is_dir() and not src.is_symlink():
                    shutil.copytree(src, target, symlinks=True)
                else:
                    shutil.copy2(src, target, follow_symlinks=False)
            except OSError as exc:
                raise _friendly(exc, f"{verb} “{src.name}”") from None
            done.append(self.entry(target))
        return done

    def copy(self, sources: list[str], dest: str) -> list[dict]:
        return self._transfer(sources, dest, move=False)

    def move(self, sources: list[str], dest: str) -> list[dict]:
        return self._transfer(sources, dest, move=True)

    # ---- trash (freedesktop.org Trash spec, home trash) ---------------------------------
    def trash_paths(self, paths: list[str]) -> int:
        files, info = self.trash / "files", self.trash / "info"
        files.mkdir(parents=True, exist_ok=True)
        info.mkdir(parents=True, exist_ok=True)
        count = 0
        for raw in paths:
            src = self.resolve(raw)
            if src == self.home or src == self.trash or self.trash in src.parents:
                raise ApiError(f"“{src.name}” can't be moved to the Trash.")
            target = unique_path(files, src.name)
            info_file = info / f"{target.name}.trashinfo"
            stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            try:
                info_file.write_text(
                    f"[Trash Info]\nPath={urllib.parse.quote(P(src))}\nDeletionDate={stamp}\n", "utf-8")
                shutil.move(str(src), str(target))
            except OSError as exc:
                info_file.unlink(missing_ok=True)
                raise _friendly(exc, f"move “{src.name}” to the Trash") from None
            count += 1
        return count

    def trash_list(self) -> dict:
        files, info = self.trash / "files", self.trash / "info"
        entries = []
        if files.is_dir():
            for item in files.iterdir():
                data = self.entry(item)
                meta = info / f"{item.name}.trashinfo"
                try:
                    text = meta.read_text("utf-8")
                    m = re.search(r"^Path=(.*)$", text, re.M)
                    d = re.search(r"^DeletionDate=(.*)$", text, re.M)
                    data["origin"] = urllib.parse.unquote(m.group(1)) if m else None
                    data["deleted"] = d.group(1) if d else None
                except OSError:
                    data["origin"] = data["deleted"] = None
                data["trashName"] = item.name
                entries.append(data)
        return {"path": "trash:///", "parent": None, "entries": entries, "writable": False, "inTrash": True}

    def restore(self, names: list[str]) -> int:
        count = 0
        for name in names:
            name = check_name(name)
            item, meta = self.trash / "files" / name, self.trash / "info" / f"{name}.trashinfo"
            try:
                m = re.search(r"^Path=(.*)$", meta.read_text("utf-8"), re.M)
            except OSError:
                m = None
            origin = Path(urllib.parse.unquote(m.group(1))) if m else self.home / name
            origin.parent.mkdir(parents=True, exist_ok=True)
            target = unique_path(origin.parent, origin.name)
            try:
                shutil.move(str(item), str(target))
            except OSError as exc:
                raise _friendly(exc, f"restore “{name}”") from None
            meta.unlink(missing_ok=True)
            count += 1
        return count

    def empty_trash(self) -> int:
        count = 0
        for sub in ("files", "info"):
            folder = self.trash / sub
            if not folder.is_dir():
                continue
            for item in folder.iterdir():
                try:
                    if item.is_dir() and not item.is_symlink():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                except OSError as exc:
                    raise _friendly(exc, "empty the Trash") from None
                count += sub == "files"
        return count

    def trash_count(self) -> int:
        folder = self.trash / "files"
        return sum(1 for _ in folder.iterdir()) if folder.is_dir() else 0


RECENT_XBEL = Path(".local/share/recently-used.xbel")


def recent_files(home: Path, limit: int = 8) -> list[dict]:
    """Files opened lately (newest first), for the Start menu's Recommended: {name, path, time}."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.parse(home / RECENT_XBEL).getroot()
    except (OSError, ET.ParseError):
        return []
    seen, out = set(), []
    for bm in root.iter("bookmark"):
        href = bm.get("href") or ""
        if not href.startswith("file://"):
            continue
        path = Path(urllib.parse.unquote(urllib.parse.urlsplit(href).path))
        if path in seen or any(part.startswith(".") for part in path.parts[1:]):
            continue
        seen.add(path)
        stamps = []
        for key in ("visited", "modified", "added"):
            try:
                stamps.append(datetime.datetime.fromisoformat((bm.get(key) or "").replace("Z", "+00:00")).timestamp())
            except ValueError:
                pass
        if stamps and path.is_file():
            out.append({"name": path.name, "path": str(path), "time": int(max(stamps)), "kind": kind_of(path, False)[1]})
    out.sort(key=lambda f: -f["time"])
    return out[:limit]
