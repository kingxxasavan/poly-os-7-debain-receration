"""Settings > Storage: how full each drive is, what takes the space in your folders, and clean-up.

Folder sizes are counted with a time budget, so a huge folder shows "at least" instead of making
the page wait. Clean-up only touches things that are safe to lose: the trash, thumbnails (made
again when needed) and, with your password, apt's downloaded packages.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

REAL_FS = {"ext4", "ext3", "ext2", "btrfs", "xfs", "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "f2fs"}
FOLDERS = ["Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"]


def drives(mounts_text: str | None = None) -> list[dict]:
    """Mounted drives: {mount, fs, total, used, free} (the system drive first)."""
    if mounts_text is None:
        try:
            mounts_text = Path("/proc/mounts").read_text()
        except OSError:
            mounts_text = ""
    out, seen = [], set()
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[2] not in REAL_FS or not parts[0].startswith("/dev/"):
            continue
        mount = parts[1].replace("\\040", " ")
        if parts[0] in seen or mount.startswith(("/snap", "/var/lib/docker", "/run/live")):
            continue
        seen.add(parts[0])
        try:
            st = os.statvfs(mount)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if total < 256 * 1024 * 1024 and mount != "/":  # a boot or EFI partition
            continue
        free = st.f_bavail * st.f_frsize
        out.append({"mount": mount, "device": parts[0], "fs": parts[2], "total": total, "free": free,
                    "used": total - st.f_bfree * st.f_frsize})
    return sorted(out, key=lambda d: (d["mount"] != "/", d["mount"]))


def dir_size(path: Path, deadline: float) -> tuple[int, bool]:
    """(bytes, complete): stops counting at the deadline."""
    total, stack = 0, [str(path)]
    while stack:
        if time.monotonic() > deadline:
            return total, False
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir():
                            stack.append(entry.path)
                        else:
                            total += entry.stat().st_blocks * 512
                    except OSError:
                        continue
        except OSError:
            continue
    return total, True


def breakdown(home: Path, budget: float = 4.0) -> list[dict]:
    """Your folders by size: {name, path, bytes, complete}, biggest first."""
    deadline = time.monotonic() + budget
    items = []
    for name, rel in [*[(f, f) for f in FOLDERS], ("Trash", ".local/share/Trash"), ("Thumbnails", ".cache/thumbnails"),
                      ("App caches", ".cache")]:
        path = home / rel
        if not path.is_dir():
            continue
        # each folder gets at least a moment, so a huge first one doesn't leave the rest at zero
        size, complete = dir_size(path, max(deadline, time.monotonic() + 0.3))
        items.append({"name": name, "path": str(path), "bytes": size, "complete": complete})
    thumbs = next((i["bytes"] for i in items if i["name"] == "Thumbnails"), 0)
    for i in items:
        if i["name"] == "App caches":
            i["bytes"] = max(0, i["bytes"] - thumbs)  # thumbnails are listed on their own
    return sorted(items, key=lambda i: -i["bytes"])


def clear_thumbnails(home: Path) -> int:
    folder = home / ".cache" / "thumbnails"
    size, _ = dir_size(folder, time.monotonic() + 10)
    shutil.rmtree(folder, ignore_errors=True)
    return size
