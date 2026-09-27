"""The PolyOS installer engine: puts the live system onto a disk.

probe() and install() run as root through polyos-admin (the setup UI never touches disks
itself). Everything that decides *what* to do (reading lsblk/sfdisk/os-prober output,
finding free space, sizing partitions, validating the plan) is plain Python with no side
effects, so it is unit-tested on any OS. The steps use standard Debian tools: sfdisk,
mkfs, ntfsresize, resize2fs, unsquashfs, chroot, grub-install and efibootmgr.

Four modes:
  erase      wipe one disk: EFI system partition + ext4 root (UEFI), or one ext4 root (BIOS)
  space      install in one stretch of unallocated space the person picked (the Windows-style
             "Where do you want to install PolyOS?" screen, where partitions can also be deleted
             and created); an EFI system partition is added there if the disk has none
  alongside  keep the other system: use free space, or shrink its NTFS/ext4 partition first;
             GRUB then offers both (os-prober)
  custom     the person decides per drive and partition: erase a drive for PolyOS, your files
             (/home) or extra storage; or use existing partitions for /, /home, /boot/efi, swap
             and storage (/mnt/NAME), formatted or kept as they are. Anything not chosen is kept.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import __version__, drivers, firststart, hwcheck, polyaccount, recovery, security
from .arch import EFI, debian_arch

KiB, MiB, GiB = 1024, 1024 ** 2, 1024 ** 3
MIN_ROOT = 20 * GiB          # smallest PolyOS partition the installer offers
SUGGESTED_ROOT = 64 * GiB    # default size when installing alongside another system
KEEP_FREE = 10 * GiB         # a shrunk system keeps at least this much free space
ESP_BYTES = 512 * MiB
BIOS_BOOT_BYTES = 1 * MiB
ALIGN = 1 * MiB

ESP_GUID = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
LINUX_GUID = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
BIOS_BOOT_GUID = "21686148-6449-6E6F-744E-656564454649"
MSR_GUID = "E3C9E316-0B5C-4DB8-817D-F92DF00215AE"  # Microsoft reserved
WINRE_GUID = "DE94BBA4-06D1-4D40-A16A-BFD50179D6AC"  # Windows recovery
MS_BASIC_GUID = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"
ESP_MBR_TYPES = {"ef", "0xef"}
EXTENDED_MBR_TYPES = {"5", "f", "85", "0x5", "0xf", "0x85"}

SQUASHFS = Path("/run/live/medium/live/filesystem.squashfs")
LIVE_MEDIUM = Path("/run/live/medium")
INSTALL_OPTIONS = LIVE_MEDIUM / "polyos" / "install.json"  # on the USB drive, next to live/ (not in the squashfs)
TARGET = Path("/mnt/polyos-target")
GRUB_THEME = "/usr/share/grub/themes/polyos"  # the boot menu's look (data/grub, packaged in polyos-shell)
OFFLINE_DEBS = "/usr/share/polyos/installer/debs"  # efi/ and bios/: GRUB packages fetched at ISO build
LOG_PATH = Path("/var/log/polyos-installer.log")
LIVE_PACKAGES = ["live-boot", "live-boot-initramfs-tools", "live-config", "live-config-systemd", "live-tools",
                 "calamares", "calamares-settings-debian", "user-setup"]
USER_GROUPS = ["sudo", "audio", "video", "plugdev", "netdev", "bluetooth", "lpadmin", "scanner", "users", "dip", "cdrom"]
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
HOSTNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
RESERVED_USERS = {
    "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail", "news", "uucp", "proxy", "www-data",
    "backup", "list", "irc", "nobody", "messagebus", "lightdm", "polkitd", "avahi", "colord", "pulse", "rtkit",
    "saned", "sshd", "systemd-network", "systemd-resolve", "systemd-timesync", "tss", "usbmux", "polyos", "admin",
}
LSBLK_COLUMNS = "NAME,TYPE,SIZE,MODEL,TRAN,RM,RO,FSTYPE,LABEL,PARTTYPE,PARTN,MOUNTPOINTS,PTTYPE,PKNAME"
# custom mode: where partitions may go. "/", /var and /opt always get a fresh ext4 filesystem.
SYSTEM_MOUNTS = ("/", "/boot/efi", "/home", "/var", "/opt", "/srv")
MUST_FORMAT = ("/", "/var", "/opt")
WIPE_TARGETS = ("/", "/home", "/srv")  # plus /mnt/NAME
STORAGE_RE = re.compile(r"^/mnt/[a-z0-9][a-z0-9_-]{0,31}$")
DEVICE_RE = re.compile(r"^/dev/[A-Za-z0-9/_-]+$")
MIN_ESP = 100 * MiB
# filesystems an existing partition can be mounted with, kept as it is (fstab type, options)
KEEP_FS = {
    "ext4": ("ext4", "defaults"), "ext3": ("ext3", "defaults"), "ext2": ("ext2", "defaults"),
    "btrfs": ("btrfs", "defaults"), "xfs": ("xfs", "defaults"), "f2fs": ("f2fs", "defaults"),
    "ntfs": ("ntfs3", "uid={uid},gid={uid},umask=022"), "vfat": ("vfat", "uid={uid},gid={uid},umask=022,utf8"),
    "exfat": ("exfat", "uid={uid},gid={uid},umask=022"),
}
EDITIONS = ("regular", "developer", "gaming")
AUTO_UPGRADES = security.AUTO_UPGRADES_TEXT.format(on=1)
ZRAMSWAP = ("# PolyOS: compressed swap in RAM, used before the swap file (much faster than a disk)\n"
            "ALGO=zstd\nPERCENT=50\nPRIORITY=100\n")
LINUX_FS = ("ext4", "ext3", "ext2", "btrfs", "xfs", "f2fs")  # can hold /home (Unix permissions)


class InstallError(Exception):
    """A failure whose message is meant for the person installing."""


# ==== reading the machine (pure parsers) ===================================================

def _align_up(value: int, step: int) -> int:
    return -(-value // step) * step


def _align_down(value: int, step: int) -> int:
    return value // step * step


def parse_lsblk(data: dict) -> list[dict]:
    """Disks from `lsblk -J -b -p -o LSBLK_COLUMNS`, with their partitions."""
    disks = []
    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        name = dev.get("name") or ""
        if re.match(r"^/dev/(zram|loop|sr|ram|fd)", name):
            continue
        parts = []
        for child in dev.get("children") or []:
            if child.get("type") != "part":
                continue
            parts.append({
                "path": child.get("name"),
                "number": int(child["partn"]) if str(child.get("partn") or "").isdigit() else None,
                "size": int(child.get("size") or 0),
                "fstype": child.get("fstype") or "",
                "label": child.get("label") or "",
                "parttype": (child.get("parttype") or "").lower(),
                "mounts": [m for m in (child.get("mountpoints") or []) if m],
            })
        disks.append({
            "path": name,
            "size": int(dev.get("size") or 0),
            "model": (dev.get("model") or "").strip() or "Disk",
            "transport": dev.get("tran") or "",
            "removable": bool(dev.get("rm")) and dev.get("rm") not in ("0", 0),
            "readonly": bool(dev.get("ro")) and dev.get("ro") not in ("0", 0),
            "table": dev.get("pttype") or None,
            "mounts": [m for m in (dev.get("mountpoints") or []) if m],
            "partitions": parts,
        })
    return disks


def parse_sfdisk(data: dict) -> dict:
    """Geometry from `sfdisk -J DISK`: label, sector size, usable range and partitions (in sectors)."""
    table = data["partitiontable"]
    sector = int(table.get("sectorsize") or 512)
    parts = []
    for p in table.get("partitions", []):
        node = p["node"]
        m = re.search(r"(\d+)$", node)
        parts.append({"node": node, "number": int(m.group(1)) if m else None, "start": int(p["start"]),
                      "size": int(p["size"]), "type": str(p.get("type", "")).lower()})
    return {
        "label": table.get("label"),
        "sector": sector,
        "first": int(table.get("firstlba") or (ALIGN // sector)),
        "last": int(table["lastlba"]) if table.get("lastlba") is not None else None,
        "partitions": sorted(parts, key=lambda p: p["start"]),
    }


def free_regions(table: dict, disk_bytes: int) -> list[dict]:
    """Unallocated, 1 MiB-aligned gaps (in sectors) that could hold a new partition."""
    sector = table["sector"]
    align = max(1, ALIGN // sector)
    first = max(table["first"], align)
    last = table["last"] if table["last"] is not None else disk_bytes // sector - 1
    used = [p for p in table["partitions"] if p["size"] > 0]
    regions, cursor = [], first
    for p in used:  # an extended partition counts as used: new primaries go outside it
        if p["start"] > cursor:
            regions.append((cursor, p["start"] - 1))
        cursor = max(cursor, p["start"] + p["size"])
    if last >= cursor:
        regions.append((cursor, last))
    out = []
    for lo, hi in regions:
        start = _align_up(lo, align)
        end = _align_down(hi + 1, align)  # exclusive
        if end - start >= align * 16:  # ignore slivers under 16 MiB
            out.append({"start": start, "size": end - start})
    return out


def parse_os_prober(text: str) -> dict[str, str]:
    """`os-prober` lines look like /dev/sda1@/efi/Microsoft/Boot/bootmgfw.efi:Windows Boot Manager:Windows:efi."""
    found = {}
    for line in text.splitlines():
        parts = line.strip().split(":")
        if len(parts) < 3:
            continue
        dev = parts[0].split("@", 1)[0]
        name = parts[1].strip() or parts[2].strip()
        if name == "Windows Boot Manager":
            name = "Windows"
        found[dev] = name
    return found


_NTFS_MIN = re.compile(r"You might resize at (\d+) bytes")


def parse_ntfsresize_info(text: str) -> tuple[int | None, str | None]:
    """(minimum size in bytes, reason it can't be resized) from `ntfsresize --info`."""
    lowered = text.lower()
    if "hibernated" in lowered or "unsafe state" in lowered or "fast restart" in lowered:
        return None, ("Windows didn't shut down completely (Fast Startup or hibernation is on). Start Windows, "
                      "turn off Fast Startup in Power Options, then shut down and try again.")
    if "bitlocker" in lowered:
        return None, "This partition is encrypted with BitLocker. Turn BitLocker off in Windows first."
    m = _NTFS_MIN.search(text)
    if m:
        return int(m.group(1)), None
    if "error" in lowered:
        last = [ln for ln in text.splitlines() if "error" in ln.lower()]
        return None, f"Windows' partition can't be resized: {last[-1].strip() if last else 'ntfsresize failed'}"
    return None, "Couldn't check how far this partition can shrink."


_RESIZE2FS_MIN = re.compile(r"Estimated minimum size of the filesystem:\s*(\d+)")
_BLOCK_SIZE = re.compile(r"^Block size:\s*(\d+)", re.M)


def parse_ext4_min(resize2fs_out: str, dumpe2fs_out: str) -> int | None:
    m = _RESIZE2FS_MIN.search(resize2fs_out)
    b = _BLOCK_SIZE.search(dumpe2fs_out)
    if not m or not b:
        return None
    return int(m.group(1)) * int(b.group(1))


def is_esp(part: dict, label: str | None) -> bool:
    ptype = part.get("parttype", "")
    return ptype == ESP_GUID.lower() or (label == "dos" and ptype in ESP_MBR_TYPES)


def guess_os(part: dict, prober: dict[str, str]) -> str | None:
    if part["path"] in prober:
        return prober[part["path"]]
    if part["fstype"] == "ntfs" and part["size"] >= 20 * GiB and part["parttype"] in (MS_BASIC_GUID.lower(), "0x7", "7"):
        return "Windows"
    if part["fstype"] == "BitLocker":
        return "Windows (BitLocker)"
    return None


def alongside_option(disk: dict, uefi: bool) -> dict:
    """How PolyOS could fit next to what's on this disk: free space, or shrinking one partition."""
    need_esp = uefi and not any(p.get("esp") for p in disk["partitions"])
    extra = (ESP_BYTES if need_esp else 0) + (BIOS_BOOT_BYTES if (not uefi and disk["table"] == "gpt") else 0)
    if disk["table"] is None:
        return {"possible": False, "reason": "This disk is empty. Choose “Install PolyOS 7” to use all of it."}
    if uefi and disk["table"] == "dos" and need_esp:
        return {"possible": False, "reason": "The system on this disk starts in legacy BIOS mode. Restart this USB "
                "drive in legacy (CSM) mode to install PolyOS next to it."}
    if not uefi and disk["table"] == "gpt" and any(p.get("esp") for p in disk["partitions"]) \
            and not any(p["parttype"] == BIOS_BOOT_GUID.lower() for p in disk["partitions"]):
        return {"possible": False, "reason": "The system on this disk starts in UEFI mode, but this USB drive "
                "started in legacy BIOS mode. Restart it in UEFI mode to install alongside."}
    if disk["table"] == "dos":
        primaries = [p for p in disk["partitions"] if (p["number"] or 0) <= 4]
        if len(primaries) >= 4:
            return {"possible": False, "reason": "This disk already has four primary partitions, the most it can hold."}
    best = None
    for region in disk.get("free", []):
        usable = region["bytes"] - extra
        if usable >= MIN_ROOT and (best is None or usable > best["maxBytes"]):
            best = {"possible": True, "kind": "free", "start": region["start"], "maxBytes": usable, "extra": extra}
    for part in disk["partitions"]:
        info = part.get("resize") or {}
        if info.get("min") is None:
            continue
        keep = _align_up(max(info["min"], (info.get("used") or info["min"]) + KEEP_FREE), ALIGN)
        usable = _align_down(part["size"] - keep, ALIGN) - extra
        if usable >= MIN_ROOT and (best is None or (best["kind"] != "free" and usable > best["maxBytes"])):
            best = {"possible": True, "kind": "shrink", "partition": part["path"], "keepMin": keep,
                    "maxBytes": usable, "extra": extra}
    if best:
        best["suggested"] = min(best["maxBytes"], max(MIN_ROOT, min(SUGGESTED_ROOT, best["maxBytes"] // 2)))
        best["minBytes"] = MIN_ROOT
        return best
    reasons = [p["resize"]["reason"] for p in disk["partitions"] if (p.get("resize") or {}).get("reason")]
    return {"possible": False, "reason": reasons[0] if reasons else
            f"There isn't enough free space. PolyOS needs at least {MIN_ROOT // GiB} GB."}


def new_partition_problem(disk: dict, uefi: bool) -> str | None:
    """Why PolyOS can't add its partitions to this disk as it is, or None."""
    if disk["table"] is None or not disk["partitions"]:
        return None  # an empty drive is set up from scratch
    has_esp = any(p.get("esp") for p in disk["partitions"])
    if uefi and disk["table"] == "dos" and not has_esp:
        return ("This drive uses the older MBR layout and this computer started in UEFI mode. Delete all its "
                "partitions so PolyOS can set it up, or restart the USB drive in legacy (CSM) mode.")
    if not uefi and disk["table"] == "gpt" and has_esp \
            and not any(p["parttype"] == BIOS_BOOT_GUID.lower() for p in disk["partitions"]):
        return ("The system on this drive starts in UEFI mode, but this USB drive started in legacy BIOS mode. "
                "Restart it in UEFI mode.")
    if disk["table"] == "dos" and len([p for p in disk["partitions"] if (p["number"] or 0) <= 4]) >= 4:
        return "This drive already has four partitions, the most an MBR drive can hold. Delete one first."
    return None


def space_option(disk: dict, region: dict, uefi: bool) -> dict:
    """Can PolyOS go in this unallocated space? {possible, usable bytes, newEsp} or {possible, reason}."""
    if disk.get("isLive"):
        return {"possible": False, "reason": "PolyOS is running from this drive."}
    if disk.get("readonly"):
        return {"possible": False, "reason": "This drive is read-only."}
    empty = disk["table"] is None or not disk["partitions"]
    problem = new_partition_problem(disk, uefi)
    if problem:
        return {"possible": False, "reason": problem}
    new_esp = uefi and (empty or not any(p.get("esp") for p in disk["partitions"]))
    extra = (ESP_BYTES if new_esp else 0) + (BIOS_BOOT_BYTES if not uefi and (disk["table"] == "gpt" or empty and disk["size"] > 2 * 1024 ** 4) else 0)
    usable = region["bytes"] - extra
    if usable < MIN_ROOT:
        return {"possible": False, "reason": f"This space is too small. PolyOS needs at least {MIN_ROOT // GiB} GB."}
    return {"possible": True, "usable": usable, "newEsp": new_esp}


def partition_option(part: dict, disk: dict) -> dict:
    """Can PolyOS be installed on this partition (erasing it)? The EFI check across drives comes later."""
    if disk.get("isLive"):
        return {"possible": False, "reason": "PolyOS is running from this drive."}
    if disk.get("readonly"):
        return {"possible": False, "reason": "This drive is read-only."}
    ptype = part.get("parttype", "")
    if part.get("esp") or ptype in (MSR_GUID.lower(), BIOS_BOOT_GUID.lower(), WINRE_GUID.lower()):
        return {"possible": False, "reason": "This partition is needed to start your computer or Windows. Choose another one."}
    if disk["table"] == "dos" and ptype in EXTENDED_MBR_TYPES:
        return {"possible": False, "reason": "This is an extended partition. Choose one of the partitions inside it."}
    if part["size"] < MIN_ROOT:
        return {"possible": False, "reason": f"This partition is too small. PolyOS needs at least {MIN_ROOT // GiB} GB."}
    return {"possible": True}


BITLOCKER_REASON = ("BitLocker is on, so PolyOS can't be installed next to Windows. Turn BitLocker off in "
                    "Windows, wait until it has finished decrypting, then start this USB drive again.")


def bitlocker_volumes(disks: list[dict]) -> list[dict]:
    """Partitions encrypted with BitLocker (still encrypting or decrypting counts too)."""
    return [{"path": p["path"], "disk": d["path"], "model": d.get("model") or d["path"], "size": p["size"],
             "label": p.get("label") or ""}
            for d in disks if not d.get("isLive") for p in d["partitions"] if (p.get("fstype") or "").lower() == "bitlocker"]


WINDOWS_ASLEEP_REASON = ("Windows is only asleep, not shut down (Fast Startup is on), so changing this drive now would "
                         "send Windows into Automatic Repair. Start Windows, turn off Fast Startup (Control Panel › "
                         "Power Options › Choose what the power buttons do), shut down, and start this USB drive again.")
NTFS_HIBERNATED, NTFS_UNCLEAN = 14, 15  # ntfs-3g.probe exit codes


def windows_asleep(disks: list[dict]) -> list[dict]:
    """NTFS partitions Windows left hibernated (Fast Startup) or not cleanly unmounted."""
    return [{"path": p["path"], "disk": d["path"], "model": d.get("model") or d["path"], "size": p["size"],
             "label": p.get("label") or "", "state": p["ntfsState"]}
            for d in disks if not d.get("isLive") for p in d["partitions"] if p.get("ntfsState")]


def finish_install_options(disks: list[dict], uefi: bool) -> list[dict]:
    """A partition only works in UEFI mode if some drive has an EFI system partition PolyOS can share.

    And while BitLocker is on, PolyOS doesn't go next to Windows: it can't shrink an encrypted
    partition, and a new boot manager would make Windows ask for its BitLocker recovery key.
    Deleting the encrypted partition (or erasing the whole drive) is still fine."""
    # Windows asleep (Fast Startup): it keeps every drive it had open in its saved session, so any
    # change to a drive with Windows' partitions on it breaks the resume. Only erasing a whole drive
    # stays possible (it asks first, as always).
    if windows_asleep(disks):
        for disk in disks:
            if disk.get("isLive") or not any(p.get("fstype") == "ntfs" for p in disk["partitions"]):
                continue
            disk["alongside"] = {"possible": False, "reason": WINDOWS_ASLEEP_REASON, "windowsAsleep": True}
            for region in disk["free"]:
                region["install"] = {"possible": False, "reason": WINDOWS_ASLEEP_REASON, "windowsAsleep": True}
            for part in disk["partitions"]:
                part["install"] = {"possible": False, "reason": WINDOWS_ASLEEP_REASON, "windowsAsleep": True}
    locked = {v["path"] for v in bitlocker_volumes(disks)}
    if locked:
        for disk in disks:
            if disk.get("isLive"):
                continue
            if disk["alongside"].get("possible") or any(p["path"] in locked for p in disk["partitions"]):
                disk["alongside"] = {"possible": False, "reason": BITLOCKER_REASON, "bitlocker": True}
            for region in disk["free"]:
                if region["install"]["possible"]:
                    region["install"] = {"possible": False, "reason": BITLOCKER_REASON, "bitlocker": True}
            for part in disk["partitions"]:
                if part["install"]["possible"] and locked - {part["path"]}:
                    part["install"] = {"possible": False, "reason": BITLOCKER_REASON, "bitlocker": True}
    has_esp = any(p.get("esp") and p["size"] >= MIN_ESP for d in disks if not d.get("isLive") for p in d["partitions"])
    if uefi and not has_esp:
        for disk in disks:
            for part in disk["partitions"]:
                if part["install"]["possible"]:
                    part["install"] = {"possible": False, "reason": (
                        "This computer needs an EFI system partition, and no drive has one. Delete this partition "
                        "and choose the unallocated space instead; PolyOS then makes one.")}
    return disks


def describe_disk(disk: dict, table: dict | None, prober: dict[str, str], uefi: bool, live_disk: str | None,
                  resize: dict[str, dict]) -> dict:
    """Everything the setup UI shows about one disk."""
    label = table["label"] if table else None
    # os-prober names UEFI Windows by its boot partition; give that name to the NTFS partition too
    probed = [prober[p["path"]] for p in disk["partitions"] if p["path"] in prober]
    windows_name = next((n for n in probed if "windows" in n.lower()), None)
    for part in disk["partitions"]:
        part["esp"] = is_esp(part, label)
        part["os"] = guess_os(part, prober)
        if part["os"] == "Windows" and part["path"] not in prober and windows_name:
            part["os"] = windows_name
        part["resize"] = resize.get(part["path"])
        placed = next((t for t in (table or {}).get("partitions", []) if t["node"] == part["path"]
                       or (t["number"] is not None and t["number"] == part["number"])), None)
        part["start"] = placed["start"] if placed else None  # in sectors, for listing in disk order
    # The size-based guess can call every big NTFS partition "Windows"; Windows lives on one of
    # them (C:), the others are data drives like D:. Keep the guess for the likeliest one only.
    guessed = [p for p in disk["partitions"] if p["os"] and p["path"] not in prober and p["fstype"] == "ntfs"]
    if len(guessed) > 1:
        named = [p for p in guessed if re.search(r"windows|^os$|system|^c$", p.get("label") or "", re.I)]
        keep = named[0] if named else min(guessed, key=lambda p: (p["start"] is None, p["start"] or 0, p["number"] or 0))
        for part in guessed:
            if part is not keep:
                part["os"] = None
    disk["table"] = label
    disk["sector"] = table["sector"] if table else 512
    disk["free"] = [{"start": r["start"], "bytes": r["size"] * table["sector"]}
                    for r in free_regions(table, disk["size"])] if table else []
    disk["oses"] = sorted({p["os"] for p in disk["partitions"] if p["os"]})
    disk["isLive"] = disk["path"] == live_disk
    disk["canErase"] = not disk["isLive"] and not disk["readonly"] and disk["size"] >= MIN_ROOT
    disk["alongside"] = alongside_option(disk, uefi) if disk["canErase"] else \
        {"possible": False, "reason": "PolyOS is running from this drive." if disk["isLive"] else "This disk is too small."}
    if label is None:  # an empty drive: all of it is one stretch of unallocated space
        disk["free"] = [{"start": 0, "bytes": disk["size"]}]
    for region in disk["free"]:
        region["install"] = space_option(disk, region, uefi)
    for part in disk["partitions"]:
        part["install"] = partition_option(part, disk)
    return disk


# ==== what this USB drive offers ==============================================================

FRESH_ONLY_REASON = ("This PolyOS USB drive installs PolyOS on a whole drive (a fresh install). "
                     "To keep Windows or another system next to PolyOS, use the dual-boot download.")


def install_options(path: Path | None = None) -> dict:
    """The public ISO installs on a whole drive only ({"dualBoot": false} in /polyos/install.json on
    the USB drive). The dual-boot ISO, and ISOs from before the file existed, offer everything."""
    try:
        data = json.loads((path or INSTALL_OPTIONS).read_text("utf-8"))
    except (OSError, ValueError):
        data = {}
    return {"dualBoot": not (isinstance(data, dict) and data.get("dualBoot") is False)}


# ==== the plan the setup UI sends ============================================================

def validate_plan(plan: dict, existing_users: set[str] | None = None) -> dict:
    """Check the setup UI's plan; returns a cleaned copy. Raises InstallError with a readable message."""
    if not isinstance(plan, dict):
        raise InstallError("The install plan is missing.")
    mode = plan.get("mode")
    if mode not in ("erase", "space", "alongside", "custom"):
        raise InstallError("Choose how to install PolyOS.")
    if mode != "erase" and not install_options()["dualBoot"]:
        raise InstallError(FRESH_ONLY_REASON)
    layout = None
    if mode == "custom":
        layout = validate_layout(plan.get("wipe") or {}, plan.get("mounts") or [])
        plan = {**plan, "disk": layout["disk"]}
    disk = plan.get("disk")
    if not isinstance(disk, str) or not DEVICE_RE.match(disk):
        raise InstallError("Choose a disk.")
    user = plan.get("user") or {}
    username = str(user.get("username") or "")
    if not USERNAME_RE.match(username):
        raise InstallError("Usernames use lowercase letters, numbers, - and _, and start with a letter.")
    if username in RESERVED_USERS or username in (existing_users or set()):
        raise InstallError(f"“{username}” is reserved. Pick another username.")
    full = str(user.get("fullName") or "").strip()[:80] or username
    if any(c in full for c in ":,\n="):
        raise InstallError("Your name can't contain : , or =.")
    password = user.get("password") or ""
    if not isinstance(password, str) or len(password) > 256 or "\n" in password:
        raise InstallError("That password can't be used.")
    key = user.get("recoveryKey") or ""
    if key and not recovery.looks_valid(str(key)):
        raise InstallError("The recovery key is damaged. Go back and try again.")
    hostname = str(plan.get("hostname") or f"{username}-polyos").lower()
    if not HOSTNAME_RE.match(hostname):
        raise InstallError("Computer names use letters, numbers and hyphens (up to 63).")
    tz = str(plan.get("timezone") or "UTC")
    if ".." in tz or not re.match(r"^[A-Za-z0-9_+/-]{1,64}$", tz):
        raise InstallError("Choose a time zone.")
    appearance = plan.get("appearance") or {}
    theme = appearance.get("theme") if appearance.get("theme") in ("dark", "light") else "dark"
    accent = appearance.get("accent") if re.match(r"^#[0-9a-fA-F]{6}$", str(appearance.get("accent") or "")) else "#678fd9"
    edition = plan.get("edition") or "regular"
    if edition not in EDITIONS:
        raise InstallError("Choose Regular, Developer or Gaming.")
    # How PolyOS runs here, from the hardware check on the USB drive (its settings follow from it)
    profile = plan.get("profile") if plan.get("profile") in hwcheck.PROFILE_SETTINGS else None
    background = plan.get("background") if plan.get("background") in ("normal", "reduced") else None
    look = {**(hwcheck.PROFILE_SETTINGS[profile] if profile else {}), **({"backgroundLimit": background} if background else {})}
    # "Set up like one of your computers": that computer's settings and apps (its look is on the
    # Personalization screen, so this one's theme and accent win)
    restore = polyaccount.clean_backup(plan.get("restore")) if plan.get("restore") else None
    copied = {k: v for k, v in (restore or {}).get("settings", {}).items() if k not in ("theme", "accent")}
    clean = {"mode": mode, "disk": disk, "hostname": hostname, "timezone": tz,
             "user": {"username": username, "fullName": full, "password": password, "recoveryKey": str(key)},
             "appearance": {"theme": theme, "accent": accent.lower()}, "edition": edition,
             # Setup asked everything before installing, so the new system starts straight to the
             # desktop; the edition's apps and the drivers install by themselves once online.
             "extraSettings": {**copied, **look, "edition": edition, "developerMode": edition == "developer",
                               "showAllApps": False, "gameMode": edition == "gaming",
                               "editionSetup": True},
             "firstStart": firststart.clean_plan(plan.get("drivers") or [], edition if edition != "regular" else None,
                                                 drivers.DRIVER_PACKAGE_RE, (restore or {}).get("apps"), polyaccount.APP_ID_RE),
             "polyAccount": poly_account_state(plan.get("polyAccount"))}
    if layout:
        clean.update(layout)
    if mode == "space":
        start = plan.get("start")
        if not isinstance(start, int) or isinstance(start, bool) or start < 0:
            raise InstallError("Choose the unallocated space for PolyOS.")
        clean["start"] = start
    if mode == "alongside":
        size = plan.get("size")
        if not isinstance(size, int) or size < MIN_ROOT:
            raise InstallError(f"Give PolyOS at least {MIN_ROOT // GiB} GB.")
        clean["size"] = size
    return clean


def poly_account_state(state) -> dict | None:
    """The Poly Account connection made during setup on the USB drive, for the new account's home."""
    if not isinstance(state, dict) or not isinstance(state.get("credential"), str):
        return None
    cred = state["credential"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]{8,300}", cred):
        return None
    keep = {k: state[k] for k in ("account", "device", "syncPrefs", "terms") if isinstance(state.get(k), dict)}
    keep.update({k: state[k] for k in ("sync", "remoteManagement") if isinstance(state.get(k), bool)})
    if state.get("telemetry") in ("minimal", "standard", "diagnostic"):
        keep["telemetry"] = state["telemetry"]
    return {"credential": cred, **keep}


def firewall_conf(text: str) -> str:
    """ufw.conf with the firewall enabled (ufw's defaults: nothing gets in, everything goes out)."""
    if re.search(r"^ENABLED=", text, re.M):
        return re.sub(r"^ENABLED=.*$", "ENABLED=yes", text, flags=re.M)
    return text + ("" if text.endswith("\n") or not text else "\n") + "ENABLED=yes\n"


def disk_of(device: str) -> str:
    """/dev/sda3 -> /dev/sda, /dev/nvme0n1p2 -> /dev/nvme0n1 (the real parent is checked against lsblk)."""
    m = re.match(r"^(/dev/(?:nvme\d+n\d+|mmcblk\d+|loop\d+|md\d+))p\d+$", device) or re.match(r"^(/dev/[a-z]+)\d+$", device)
    return m.group(1) if m else device


def _mount_ok(mount: str) -> bool:
    return mount in SYSTEM_MOUNTS or mount == "swap" or bool(STORAGE_RE.match(mount))


def validate_layout(wipe, mounts) -> dict:
    """custom mode: {disk: target} drives to erase, and [{device, mount, format}] partitions to use."""
    if not isinstance(wipe, dict) or not isinstance(mounts, list) or len(wipe) > 16 or len(mounts) > 32:
        raise InstallError("Choose what to do with your drives.")
    clean_wipe: dict[str, str] = {}
    for disk, target in wipe.items():
        if not isinstance(disk, str) or not DEVICE_RE.match(disk):
            raise InstallError("One of the drives can't be used.")
        if target not in WIPE_TARGETS and not (isinstance(target, str) and STORAGE_RE.match(target)):
            raise InstallError("A whole drive can hold PolyOS (/), your files (/home) or extra storage (/mnt/…).")
        clean_wipe[disk] = target
    clean_mounts, devices = [], set()
    for entry in mounts:
        if not isinstance(entry, dict):
            raise InstallError("Choose what to do with your partitions.")
        device, mount, fmt = entry.get("device"), entry.get("mount"), entry.get("format")
        if not isinstance(device, str) or not DEVICE_RE.match(device) or device in devices:
            raise InstallError("A partition was chosen twice or can't be used.")
        if not isinstance(mount, str) or not _mount_ok(mount):
            raise InstallError(f"{device} can't be used for {mount}.")
        if not isinstance(fmt, bool):
            raise InstallError("Say whether to erase each partition.")
        if mount in MUST_FORMAT and not fmt:
            raise InstallError(f"The partition for {mount} has to be erased first.")
        if disk_of(device) in clean_wipe:
            raise InstallError(f"{device} is on a drive that will be erased.")
        devices.add(device)
        clean_mounts.append({"device": device, "mount": mount, "format": fmt})
    targets = [*clean_wipe.values(), *(m["mount"] for m in clean_mounts if m["mount"] != "swap")]
    if targets.count("/") != 1:
        raise InstallError("Choose one drive or partition for PolyOS itself (/)." if "/" not in targets
                           else "Only one drive or partition can hold PolyOS (/).")
    dupes = sorted({t for t in targets if targets.count(t) > 1})
    if dupes:
        raise InstallError(f"Two places are set to {dupes[0]}. Each folder can come from one partition only.")
    if sum(1 for m in clean_mounts if m["mount"] == "swap") > 1:
        raise InstallError("Choose at most one swap partition.")
    root_disk = next((d for d, t in clean_wipe.items() if t == "/"), None) or \
        disk_of(next(m["device"] for m in clean_mounts if m["mount"] == "/"))
    return {"disk": root_disk, "wipe": clean_wipe, "mounts": clean_mounts}


def data_script(label: str) -> str:
    """sfdisk input for a whole drive used as one ext4 partition (your files or storage)."""
    return f"label: {label}\n" + (f"type={LINUX_GUID}, name=\"PolyOS data\"\n" if label == "gpt" else "type=83\n")


def mount_order(entries: list[dict]) -> list[dict]:
    """/ first, then parents before children (/home before /home/x); swap last."""
    return sorted(entries, key=lambda e: (e["mount"] == "swap", e["mount"] != "/", e["mount"].count("/"), e["mount"]))


def fstab_entries(entries: list[dict], uid: int = 1000, swapfile: bool = False) -> str:
    """fstab from [{uuid, mount, fs, kept}]: kept foreign filesystems belong to you and never block booting."""
    lines = ["# /etc/fstab: created by the PolyOS installer",
             "# <file system>  <mount point>  <type>  <options>  <dump>  <pass>"]
    for e in mount_order(entries):
        src = f"UUID={e['uuid']}"
        if e["mount"] == "swap":
            lines.append(f"{src}  none  swap  sw  0  0")
            continue
        if e["mount"] == "/":
            lines.append(f"{src}  /  ext4  errors=remount-ro  0  1")
            continue
        if e["mount"] == "/boot/efi":
            lines.append(f"{src}  /boot/efi  vfat  umask=0077  0  1")
            continue
        fstype, opts = KEEP_FS.get(e["fs"], (e["fs"], "defaults"))
        opts = opts.format(uid=uid)
        storage = bool(STORAGE_RE.match(e["mount"]))
        if storage:  # a missing or damaged extra drive must not stop the computer from starting
            opts += ",nofail,x-systemd.device-timeout=10s"
        passno = 2 if fstype in ("ext4", "ext3", "ext2") else 0
        lines.append(f"{src}  {e['mount']}  {fstype}  {opts}  0  {passno}")
    if swapfile:
        lines.append("/swapfile  none  swap  sw  0  0")
    return "\n".join(lines) + "\n"


def erase_script(label: str, uefi: bool) -> str:
    """sfdisk input for a whole-disk install."""
    if uefi:
        return (f"label: gpt\nsize={ESP_BYTES // MiB}MiB, type={ESP_GUID}, name=\"EFI system\"\n"
                f"type={LINUX_GUID}, name=\"PolyOS\"\n")
    if label == "gpt":  # BIOS on a disk over 2 TiB: GPT with a BIOS boot partition for GRUB
        return (f"label: gpt\nsize={BIOS_BOOT_BYTES // MiB}MiB, type={BIOS_BOOT_GUID}, name=\"BIOS boot\"\n"
                f"type={LINUX_GUID}, name=\"PolyOS\"\n")
    return "label: dos\ntype=83, bootable\n"


def alongside_layout(start: int, size_bytes: int, sector: int, label: str, need_esp: bool, need_bios: bool) -> list[dict]:
    """New partitions (in sectors) packed from `start`: [ESP] [BIOS boot] root."""
    align = max(1, ALIGN // sector)
    out, cursor = [], _align_up(start, align)
    if need_esp:
        n = ESP_BYTES // sector
        out.append({"role": "esp", "start": cursor, "size": n, "type": ESP_GUID if label == "gpt" else "ef"})
        cursor += n
    if need_bios:
        n = BIOS_BOOT_BYTES // sector
        out.append({"role": "bios", "start": cursor, "size": n, "type": BIOS_BOOT_GUID})
        cursor += n
    root = _align_down(size_bytes // sector, align)
    out.append({"role": "root", "start": cursor, "size": root, "type": LINUX_GUID if label == "gpt" else "83"})
    return out


def efi_entries(text: str) -> dict[str, str]:
    """`efibootmgr` output -> {boot number: label}."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^Boot([0-9A-Fa-f]{4})\*?\s+(.+?)(?:\t|\s{2,}|\s+(?:HD|PciRoot|VenHw)\()", line)
        if m:
            out[m.group(1).upper()] = m.group(2).strip()
    return out


def fstab(root_uuid: str, esp_uuid: str | None, swapfile: bool) -> str:
    entries = [{"uuid": root_uuid, "mount": "/", "fs": "ext4"}]
    if esp_uuid:
        entries.append({"uuid": esp_uuid, "mount": "/boot/efi", "fs": "vfat"})
    return fstab_entries(entries, swapfile=swapfile)


def swap_bytes(ram: int) -> int:
    return max(1 * GiB, min(4 * GiB, _align_up(ram, GiB)))


def partition_node(disk: str, number: int) -> str:
    """/dev/sda + 2 -> /dev/sda2; /dev/nvme0n1 + 2 -> /dev/nvme0n1p2."""
    return f"{disk}p{number}" if disk[-1].isdigit() else f"{disk}{number}"


# ==== running as root ======================================================================

Emit = Callable[[dict], None]


class Runner:
    """Runs commands, logging everything; raises InstallError on failure."""

    def __init__(self, emit: Emit, dry_run: bool = False):
        self.emit = emit
        self.dry_run = dry_run
        self.log = None if dry_run else open(LOG_PATH, "a", encoding="utf-8")  # noqa: SIM115

    def note(self, text: str) -> None:
        if self.log:
            self.log.write(f"[{time.strftime('%H:%M:%S')}] {text}\n")
            self.log.flush()

    def run(self, args: list[str], *, input: str | None = None, check: bool = True, timeout: float = 600,
            what: str | None = None, env: dict | None = None) -> str:
        self.note("$ " + " ".join(args))
        if self.dry_run:
            self.emit({"log": "$ " + " ".join(args)})
            return ""
        try:
            proc = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout,
                                  env={**os.environ, "LC_ALL": "C", **(env or {})})
        except FileNotFoundError:
            raise InstallError(f"{args[0]} is missing from this USB drive.") from None
        except subprocess.TimeoutExpired:
            raise InstallError(f"{what or args[0]} took too long and was stopped.") from None
        if proc.stdout.strip():
            self.note(proc.stdout.strip()[-4000:])
        if proc.stderr.strip():
            self.note(proc.stderr.strip()[-4000:])
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            raise InstallError(f"{what or 'A step'} failed: {detail[-1] if detail else f'exit code {proc.returncode}'}")
        return proc.stdout


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def live_disk() -> str | None:
    """The disk the live system was started from (never offered as a target)."""
    try:
        out = subprocess.run(["findmnt", "-n", "-o", "SOURCE", str(LIVE_MEDIUM)], capture_output=True, text=True, timeout=5).stdout.strip()
        if not out:
            return None
        parent = subprocess.run(["lsblk", "-n", "-p", "-o", "PKNAME", out], capture_output=True, text=True, timeout=5).stdout.strip()
        return parent.splitlines()[0] if parent else out
    except (OSError, subprocess.SubprocessError):
        return None


def ntfs_state(part: dict) -> str | None:
    """"hibernated" or "unclean" when Windows didn't shut down properly (ntfs-3g.probe), else None."""
    if part.get("fstype") != "ntfs" or part.get("mounts") or not _have("ntfs-3g.probe"):
        return None
    try:
        rc = subprocess.run(["ntfs-3g.probe", "--readwrite", part["path"]], capture_output=True, timeout=60).returncode
    except (OSError, subprocess.SubprocessError):
        return None
    return {NTFS_HIBERNATED: "hibernated", NTFS_UNCLEAN: "unclean"}.get(rc)


def _resize_info(runner: Runner, part: dict) -> dict | None:
    if part["mounts"] or part["size"] < MIN_ROOT + KEEP_FREE:
        return None
    if part["fstype"] == "ntfs" and _have("ntfsresize"):
        proc = subprocess.run(["ntfsresize", "--info", "--no-progress-bar", part["path"]],
                              capture_output=True, text=True, timeout=120, env={**os.environ, "LC_ALL": "C"})
        minimum, reason = parse_ntfsresize_info(proc.stdout + proc.stderr)
        return {"fs": "ntfs", "min": minimum, "used": minimum, "reason": reason}
    if part["fstype"] == "ext4" and _have("resize2fs"):
        est = subprocess.run(["resize2fs", "-P", part["path"]], capture_output=True, text=True, timeout=120)
        head = subprocess.run(["dumpe2fs", "-h", part["path"]], capture_output=True, text=True, timeout=60)
        minimum = parse_ext4_min(est.stdout + est.stderr, head.stdout)
        return {"fs": "ext4", "min": minimum, "used": minimum,
                "reason": None if minimum else "Couldn't check how far this Linux partition can shrink."}
    if part["fstype"] == "BitLocker":
        return {"fs": "bitlocker", "min": None, "reason": "This partition is encrypted with BitLocker. Turn BitLocker off in Windows first."}
    return None


def _ram_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * KiB
    except OSError:
        pass
    return 4 * GiB


def probe(emit: Emit | None = None) -> dict:
    """What the setup UI needs to offer install choices (runs as root)."""
    runner = Runner(emit or (lambda _e: None), dry_run=False)
    uefi = Path("/sys/firmware/efi").is_dir()
    raw = json.loads(runner.run(["lsblk", "-J", "-b", "-p", "-o", LSBLK_COLUMNS], what="Reading disks") or "{}")
    disks = parse_lsblk(raw)
    prober = {}
    if _have("os-prober"):
        prober = parse_os_prober(runner.run(["os-prober"], check=False, timeout=180))
    live = live_disk()
    out = []
    for disk in disks:
        table = None
        if disk["table"]:
            text = runner.run(["sfdisk", "-J", disk["path"]], check=False)
            try:
                table = parse_sfdisk(json.loads(text)) if text.strip() else None
            except (ValueError, KeyError):
                table = None
        resize = {}
        if disk["path"] != live:
            for part in disk["partitions"]:
                part["ntfsState"] = ntfs_state(part)
                info = _resize_info(runner, part)
                if info:
                    resize[part["path"]] = info
        out.append(describe_disk(disk, table, prober, uefi, live, resize))
    secure_boot = False
    if uefi and _have("mokutil"):
        secure_boot = "enabled" in runner.run(["mokutil", "--sb-state"], check=False).lower()
    return {"uefi": uefi, "secureBoot": secure_boot, "ram": _ram_bytes(), "disks": finish_install_options(out, uefi),
            "arch": debian_arch(), "minBytes": MIN_ROOT, "liveDisk": live, "bitlocker": bitlocker_volumes(out),
            "windowsAsleep": windows_asleep(out)}


# ==== the drive screen's Delete and New (run as root, right away, like Windows Setup) ======

def _drive_to_change(runner: Runner, disk: str) -> dict:
    if not isinstance(disk, str) or not DEVICE_RE.match(disk):
        raise InstallError("That drive can't be changed.")
    if disk == live_disk():
        raise InstallError("PolyOS is running from that drive, so it can't be changed.")
    disks = {d["path"]: d for d in parse_lsblk(json.loads(runner.run(["lsblk", "-J", "-b", "-p", "-o", LSBLK_COLUMNS]) or "{}"))}
    info = disks.get(disk)
    if info is None:
        raise InstallError("That drive is no longer connected. Press Refresh.")
    if info["readonly"]:
        raise InstallError("That drive is read-only.")
    return info


def _settle_drive(runner: Runner, disk: str) -> None:
    runner.run(["partx", "-u", disk], check=False)
    runner.run(["udevadm", "settle", "--timeout=15"], check=False)


def delete_partition(disk: str, number: int, emit: Emit | None = None) -> None:
    """Delete one partition (its files are lost); the space becomes unallocated."""
    runner = Runner(emit or (lambda _e: None))
    info = _drive_to_change(runner, disk)
    part = next((p for p in info["partitions"] if p["number"] == number), None)
    if part is None:
        raise InstallError("That partition is no longer there. Press Refresh.")
    for mount in part["mounts"]:  # the live system may have opened it (Files); let go of it first
        runner.run(["swapoff", part["path"]] if mount == "[SWAP]" else ["umount", mount], what="Closing the partition")
    runner.run(["wipefs", "-a", "-f", part["path"]], check=False)  # so nothing mistakes the space for the old files
    runner.run(["sfdisk", "--delete", disk, str(number)], what="Deleting the partition")
    _settle_drive(runner, disk)


def create_partition(disk: str, start: int, size_bytes: int, emit: Emit | None = None) -> None:
    """Make a partition in unallocated space; like Windows Setup, add an EFI system partition first
    when the computer starts in UEFI mode and no drive has one yet."""
    runner = Runner(emit or (lambda _e: None))
    info = _drive_to_change(runner, disk)
    uefi = Path("/sys/firmware/efi").is_dir()
    fresh = not info["table"]
    if fresh:  # an empty drive gets a partition table first
        label = "gpt" if (uefi or info["size"] > 2 * 1024 ** 4) else "dos"
        runner.run(["sfdisk", "--wipe", "always", "-q", disk], input=f"label: {label}\n", what="Setting up the drive")
        _settle_drive(runner, disk)
    table = parse_sfdisk(json.loads(runner.run(["sfdisk", "-J", disk], what="Reading the drive")))
    sector, align = table["sector"], max(1, ALIGN // table["sector"])
    region = next((r for r in free_regions(table, info["size"])
                   if (r["start"] <= start < r["start"] + r["size"]) or (fresh and start == 0)), None)
    if region is None:
        raise InstallError("That unallocated space has changed. Press Refresh.")
    label = table["label"]
    if label == "dos" and len([p for p in table["partitions"] if (p["number"] or 0) <= 4]) >= 4:
        raise InstallError("This drive already has four partitions, the most an MBR drive can hold. Delete one first.")
    cursor = max(region["start"], _align_up(start, align))
    end = region["start"] + region["size"]
    specs = []
    all_disks = parse_lsblk(json.loads(runner.run(["lsblk", "-J", "-b", "-p", "-o", LSBLK_COLUMNS]) or "{}"))
    live = live_disk()
    has_esp = any(is_esp(p, d["table"]) for d in all_disks if d["path"] != live for p in d["partitions"])
    if uefi and not has_esp and label == "gpt":
        n = ESP_BYTES // sector
        specs.append({"start": cursor, "size": n, "type": ESP_GUID, "esp": True})
        cursor += n
    count = min(_align_down(size_bytes // sector, align), end - cursor)
    if count * sector < 16 * MiB:
        raise InstallError("That's too small for a partition.")
    specs.append({"start": cursor, "size": count, "type": LINUX_GUID if label == "gpt" else "83", "esp": False})
    for spec in specs:
        runner.run(["sfdisk", "--append", "--no-reread", "--force", "-q", disk],
                   input=f"start={spec['start']}, size={spec['size']}, type={spec['type']}\n", what="Creating the partition")
    _settle_drive(runner, disk)
    made = {p["start"]: p["node"] for p in parse_sfdisk(json.loads(runner.run(["sfdisk", "-J", disk])))["partitions"]}
    for spec in specs:
        if spec["start"] not in made:
            raise InstallError("The new partition didn't appear. Press Refresh.")
        if spec["esp"]:
            runner.run(["mkfs.vfat", "-F", "32", "-n", "EFI", made[spec["start"]]], what="Preparing the EFI system partition")


class Installer:
    def __init__(self, plan: dict, emit: Emit, dry_run: bool = False):
        self.plan = plan
        self.emit = emit
        self.r = Runner(emit, dry_run)
        self.dry = dry_run
        self.uefi = Path("/sys/firmware/efi").is_dir()
        self.arch = debian_arch()
        self.disk = plan["disk"]
        self.root_dev: str | None = None
        self.esp_dev: str | None = None
        self.esp_is_new = False
        self.mounted: list[Path] = []
        self.windows_alongside = False
        self.dual = plan["mode"] == "alongside"  # another system stays: show GRUB's menu
        self.disk_info: dict | None = None
        self.all_disks: dict[str, dict] = {}
        self.mounts: list[dict] = []  # {device, mount, format, fs, uuid}: everything that goes in fstab

    # ---- helpers ---------------------------------------------------------------------
    def step(self, progress: float, message: str) -> None:
        self.r.note(f"== {message}")
        self.emit({"progress": round(progress, 3), "message": message})

    def chroot(self, args: list[str], **kw) -> str:
        env = {"DEBIAN_FRONTEND": "noninteractive", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", **kw.pop("env", {})}
        return self.r.run(["chroot", str(TARGET), *args], env=env, **kw)

    def sfdisk_table(self) -> dict:
        return parse_sfdisk(json.loads(self.r.run(["sfdisk", "-J", self.disk], what="Reading the partition table")))

    def settle(self) -> None:
        self.r.run(["partx", "-u", self.disk], check=False)
        self.r.run(["udevadm", "settle", "--timeout=15"], check=False)
        if not self.dry:
            time.sleep(1)

    # ---- steps ------------------------------------------------------------------------
    def preflight(self) -> None:
        if not self.dry and not SQUASHFS.is_file():
            raise InstallError("PolyOS can only be installed from the PolyOS USB drive.")
        if not self.uefi and self.arch != "amd64":
            raise InstallError("This computer didn't start in UEFI mode. ARM computers need UEFI firmware to run PolyOS.")
        if self.disk == live_disk():
            raise InstallError("That's the drive PolyOS is running from. Choose another disk.")
        disks = {d["path"]: d for d in parse_lsblk(json.loads(self.r.run(["lsblk", "-J", "-b", "-p", "-o", LSBLK_COLUMNS]) or '{"blockdevices": []}'))}
        self.all_disks = disks
        involved = {self.disk, *self.plan.get("wipe", {}), *(self._parent(m["device"]) for m in self.plan.get("mounts", []))}
        live = live_disk()
        for path in involved:
            if path == live:
                raise InstallError("That's the drive PolyOS is running from. Choose another disk.")
            if not self.dry and path not in disks:
                raise InstallError("One of the drives you chose is no longer connected.")
        info = disks.get(self.disk)
        if info and info["size"] < MIN_ROOT:
            raise InstallError("That disk is too small for PolyOS.")
        self.disk_info = info
        # Windows asleep (Fast Startup) on a drive we'd change but not erase: stop before touching anything
        if not self.dry and self.plan.get("mode") != "erase":
            keep = involved - set(self.plan.get("wipe", {}))
            for path in keep:
                for part in (disks.get(path) or {}).get("partitions", []):
                    if ntfs_state(part):
                        raise InstallError(WINDOWS_ASLEEP_REASON)
        # anything on these disks that the live system mounted (e.g. automount) must go first
        for path in involved:
            for part in (disks.get(path) or {}).get("partitions", []):
                for mount in part["mounts"]:
                    if mount == "[SWAP]":
                        self.r.run(["swapoff", part["path"]], check=False)
                    else:
                        self.r.run(["umount", "-l", mount], check=False)

    def _parent(self, device: str) -> str:
        for disk in self.all_disks.values():
            if any(p["path"] == device for p in disk["partitions"]):
                return disk["path"]
        return disk_of(device)

    def _partition(self, device: str) -> dict | None:
        for disk in self.all_disks.values():
            for part in disk["partitions"]:
                if part["path"] == device:
                    return part
        return None

    def partition_erase(self) -> None:
        self.step(0.02, "Preparing the disk…")
        size = (self.disk_info or {}).get("size", 0)
        label = "gpt" if (self.uefi or size > 2 * 1024 ** 4) else "dos"
        self.r.run(["wipefs", "-a", "-f", self.disk], what="Clearing the disk")
        self.r.run(["sfdisk", "--wipe", "always", "--wipe-partitions", "always", "-q", self.disk],
                   input=erase_script(label, self.uefi), what="Creating partitions")
        self.settle()
        self.esp_dev, self.root_dev, self.esp_is_new = self._erase_nodes(self.disk, label)

    def _erase_nodes(self, disk: str, label: str) -> tuple[str | None, str, bool]:
        """(ESP, root, ESP is new) on a disk partitioned with erase_script."""
        if self.uefi:
            return partition_node(disk, 1), partition_node(disk, 2), True
        if label == "gpt":
            return None, partition_node(disk, 2), False
        return None, partition_node(disk, 1), False

    def partition_custom(self) -> None:
        """Erase the drives chosen for it, then check every partition chosen, before anything is formatted."""
        wipe, chosen = self.plan["wipe"], self.plan["mounts"]
        # check first: nothing is erased if any choice turns out to be impossible
        for entry in chosen:
            part = self._partition(entry["device"])
            if part is None and not self.dry:
                raise InstallError(f"{entry['device']} is no longer there. Look at your disks again.")
            part = part or {"size": 64 * GiB, "fstype": "ext4", "parttype": ""}
            if entry["mount"] == "/" and part["size"] < MIN_ROOT:
                raise InstallError(f"{entry['device']} is too small for PolyOS. It needs at least {MIN_ROOT // GiB} GB.")
            if entry["mount"] == "/boot/efi":
                if part["size"] < MIN_ESP:
                    raise InstallError(f"{entry['device']} is too small to be the EFI boot partition.")
                if not entry["format"] and part["fstype"] != "vfat":
                    raise InstallError(f"{entry['device']} isn't an EFI boot partition. Choose to erase it, or pick another one.")
            if not entry["format"] and entry["mount"] not in ("swap", "/boot/efi"):
                if part["fstype"] not in KEEP_FS:
                    raise InstallError(f"{entry['device']} has no files PolyOS can read ({part['fstype'] or 'empty'}). "
                                       "Choose to erase it instead.")
                if entry["mount"] in ("/home", "/srv") and part["fstype"] not in LINUX_FS:
                    raise InstallError(f"{entry['device']} uses {part['fstype']}, which can't hold {entry['mount']}. "
                                       "Use it as extra storage, or erase it.")
            if entry["mount"] == "swap" and not entry["format"] and part["fstype"] != "swap":
                raise InstallError(f"{entry['device']} isn't a swap partition yet. Choose to erase it.")
        # the whole drives
        for disk, target in wipe.items():
            info = self.all_disks.get(disk) or {"size": 500 * GiB}
            label = "gpt" if (self.uefi or info["size"] > 2 * 1024 ** 4 or target != "/") else "dos"
            self.step(0.02, f"Preparing {info.get('model') or disk}…")
            self.r.run(["wipefs", "-a", "-f", disk], what="Clearing a drive")
            script = erase_script(label, self.uefi) if target == "/" else data_script(label)
            self.r.run(["sfdisk", "--wipe", "always", "--wipe-partitions", "always", "-q", disk],
                       input=script, what="Creating partitions")
            self.r.run(["partx", "-u", disk], check=False)
            if target == "/":
                self.esp_dev, self.root_dev, self.esp_is_new = self._erase_nodes(disk, label)
            else:
                self.mounts.append({"device": partition_node(disk, 1), "mount": target, "format": True, "fs": "ext4"})
        self.r.run(["udevadm", "settle", "--timeout=15"], check=False)
        for entry in chosen:
            part = self._partition(entry["device"]) or {"fstype": "ext4", "parttype": ""}
            if entry["mount"] == "/":
                self.root_dev = entry["device"]
            elif entry["mount"] == "/boot/efi":
                self.esp_dev, self.esp_is_new = entry["device"], entry["format"]
            else:
                fs = "swap" if entry["mount"] == "swap" else "ext4" if entry["format"] else part["fstype"]
                self.mounts.append({"device": entry["device"], "mount": entry["mount"], "format": entry["format"], "fs": fs})
        if self.uefi and not self.esp_dev:  # use the EFI partition already on the PolyOS drive (or any drive)
            candidates = [p for d in sorted(self.all_disks.values(), key=lambda d: d["path"] != self.disk)
                          if d["path"] not in wipe  # (an erased drive's old EFI partition is gone)
                          for p in d["partitions"] if is_esp(p, d["table"]) and p["size"] >= MIN_ESP]
            if not candidates and not self.dry:
                raise InstallError("This computer starts in UEFI mode and needs an EFI boot partition. "
                                   "Choose one for /boot/efi, or let PolyOS erase a whole drive.")
            self.esp_dev = candidates[0]["path"] if candidates else partition_node(self.disk, 1)
        if not self.uefi and self.disk not in wipe:
            info = self.all_disks.get(self.disk) or {}
            if info.get("table") == "gpt" and not any(p["parttype"] == BIOS_BOOT_GUID.lower() for p in info["partitions"]):
                raise InstallError("This computer starts in legacy BIOS mode, and the drive for PolyOS uses GPT without a "
                                   "BIOS boot partition. Restart the USB drive in UEFI mode, or erase that whole drive.")
        # keep other systems in the boot menu (and Windows' clock) when anything else stays
        others = [guess_os(p, {}) for d in self.all_disks.values() for p in d["partitions"]
                  if d["path"] not in wipe and p["path"] not in {m["device"] for m in chosen}]
        self.windows_alongside = any("windows" in (o or "").lower() for o in others)
        self.dual = any(others) or (self.esp_dev is not None and not self.esp_is_new)

    def partition_alongside(self) -> None:
        want = self.plan["size"]
        disk = self.disk_info or {}
        table = self.sfdisk_table() if not self.dry else {"label": "gpt", "sector": 512, "first": 2048, "last": None, "partitions": []}
        sector = table["sector"]
        # rebuild the same option the UI saw, from fresh data
        resize = {}
        for part in disk.get("partitions", []):
            info = _resize_info(self.r, part) if not self.dry else None
            if info:
                resize[part["path"]] = info
        described = describe_disk(json.loads(json.dumps(disk)), table, {}, self.uefi, None, resize) if disk else None
        option = described["alongside"] if described else {"possible": False, "reason": "Disk not found."}
        if not option.get("possible"):
            raise InstallError(option.get("reason") or "There's no room for PolyOS on that disk.")
        if want > option["maxBytes"]:
            raise InstallError("That's more space than this disk can give PolyOS. Pick a smaller size.")
        existing_esp = next((p for p in described["partitions"] if p["esp"]), None)
        self.windows_alongside = any("windows" in (o or "").lower() for o in described["oses"])

        if option["kind"] == "shrink":
            part = next(p for p in described["partitions"] if p["path"] == option["partition"])
            info = part["resize"]
            new_bytes = _align_down(part["size"] - want - option["extra"], ALIGN)
            if new_bytes < option["keepMin"]:
                raise InstallError("That would leave the other system too little room.")
            self.step(0.02, f"Making room: shrinking {part['os'] or 'the other system'}…")
            if info["fs"] == "ntfs":
                self.r.run(["ntfsresize", "--no-action", "--size", str(new_bytes), part["path"]],
                           input="y\n", what="Checking Windows' partition", timeout=1800)
                self.r.run(["ntfsresize", "--no-progress-bar", "--size", str(new_bytes), part["path"]],
                           input="y\n", what="Shrinking Windows' partition", timeout=7200)
            else:
                self.r.run(["e2fsck", "-f", "-y", part["path"]], what="Checking the Linux partition", timeout=3600)
                self.r.run(["resize2fs", part["path"], f"{new_bytes // KiB}K"], what="Shrinking the Linux partition", timeout=7200)
            number = part["number"]
            self.r.run(["sfdisk", "--no-reread", "--force", "-q", "-N", str(number), self.disk],
                       input=f", {new_bytes // sector}\n", what="Resizing the partition")
            self.settle()
            table = self.sfdisk_table() if not self.dry else table
            shrunk = next((p for p in table["partitions"] if p["number"] == number), None)
            region_start = (shrunk["start"] + shrunk["size"]) if shrunk else 0
            region = next((r for r in free_regions(table, disk.get("size", 0)) if r["start"] >= region_start), None)
        else:
            region = next((r for r in free_regions(table, disk.get("size", 0)) if r["start"] == option["start"]), None)
        if region is None and not self.dry:
            raise InstallError("The free space for PolyOS disappeared. Nothing else was changed.")
        self._create_in_region(table, region, want, existing_esp)

    def _create_in_region(self, table: dict, region: dict | None, want: int, existing_esp: dict | None) -> None:
        """PolyOS's partitions ([ESP] [BIOS boot] root) packed into one free region (sectors)."""
        sector = table["sector"]
        start = region["start"] if region else 2048
        need_esp = self.uefi and existing_esp is None
        need_bios = (not self.uefi) and table["label"] == "gpt"
        layout = alongside_layout(start, want, sector, table["label"], need_esp, need_bios)
        if region and sum(p["size"] for p in layout) + (layout[0]["start"] - start) > region["size"]:
            raise InstallError("PolyOS doesn't fit in the free space. Pick a smaller size.")
        self.step(0.05, "Creating PolyOS's partition…")
        before = {p["start"] for p in table["partitions"]}
        for spec in layout:
            self.r.run(["sfdisk", "--append", "--no-reread", "--force", "-q", self.disk],
                       input=f"start={spec['start']}, size={spec['size']}, type={spec['type']}\n", what="Creating partitions")
        self.settle()
        if self.dry:
            self.root_dev, self.esp_dev = partition_node(self.disk, 9), existing_esp["path"] if existing_esp else None
            return
        after = {p["start"]: p for p in self.sfdisk_table()["partitions"]}
        for spec in layout:
            created = after.get(spec["start"])
            if created is None or created["start"] in before:
                raise InstallError("The new partition didn't appear. Nothing was installed.")
            if spec["role"] == "root":
                self.root_dev = created["node"]
            elif spec["role"] == "esp":
                self.esp_dev, self.esp_is_new = created["node"], True
        if existing_esp and not self.esp_dev:
            self.esp_dev = existing_esp["path"]

    def partition_space(self) -> None:
        """Install in the stretch of unallocated space picked on the drive screen."""
        disk = self.disk_info or {"path": self.disk, "size": 500 * GiB, "readonly": False, "table": "gpt", "partitions": []}
        table = self.sfdisk_table() if not self.dry else {"label": "gpt", "sector": 512, "first": 2048, "last": None, "partitions": []}
        described = describe_disk(json.loads(json.dumps(disk)), table, {}, self.uefi, None, {})
        spot = next((r for r in described["free"] if r["start"] == self.plan["start"]), None)
        if spot is None and not self.dry:
            raise InstallError("That unallocated space has changed. Go back and look at your drives again.")
        option = spot["install"] if spot else {"possible": True, "usable": 100 * GiB}
        if not option["possible"]:
            raise InstallError(option["reason"])
        region = next((r for r in free_regions(table, disk["size"]) if r["start"] == self.plan["start"]), None)
        existing_esp = next((p for p in described["partitions"] if p["esp"]), None)
        self._create_in_region(table, region, option["usable"], existing_esp)
        # anything else on the computer stays, so the boot menu offers it too
        others = [guess_os(p, {}) for d in self.all_disks.values() for p in d["partitions"]]
        self.windows_alongside = any("windows" in (o or "").lower() for o in others)
        self.dual = any(others) or (self.esp_dev is not None and not self.esp_is_new)

    def _system_mounts(self) -> None:
        """Put PolyOS's own partitions (from erase/alongside/custom) in front of the others."""
        own = [{"device": self.root_dev, "mount": "/", "format": True, "fs": "ext4"}]
        if self.esp_dev:
            own.append({"device": self.esp_dev, "mount": "/boot/efi", "format": self.esp_is_new, "fs": "vfat"})
        self.mounts = own + self.mounts

    def format_and_mount(self) -> None:
        self.step(0.07, "Formatting…")
        self._system_mounts()
        labels = {"/": "PolyOS", "/home": "Home", "/boot/efi": "EFI"}
        for e in self.mounts:
            if not e["format"]:
                continue
            name = labels.get(e["mount"]) or e["mount"].rsplit("/", 1)[-1][:16] or "Data"
            if e["mount"] == "swap":
                self.r.run(["mkswap", "-L", "swap", e["device"]], what=f"Making {e['device']} a swap partition")
            elif e["fs"] == "vfat":
                self.r.run(["mkfs.vfat", "-F", "32", "-n", "EFI", e["device"]], what="Formatting the EFI partition")
            else:
                self.r.run(["mkfs.ext4", "-F", "-q", "-L", name, e["device"]], what=f"Formatting {e['device']} for {e['mount']}")
        # mounted now: PolyOS's own folders, and new storage (so it can be handed to you);
        # storage you're keeping is only listed in fstab and left untouched until PolyOS starts
        for e in mount_order(self.mounts):
            if e["mount"] == "swap" or (STORAGE_RE.match(e["mount"]) and not e["format"]):
                continue
            where = TARGET if e["mount"] == "/" else TARGET / e["mount"].lstrip("/")
            if not self.dry:
                where.mkdir(parents=True, exist_ok=True)
            self.r.run(["mount", e["device"], str(where)], what=f"Mounting {e['mount']}")
            self.mounted.append(where)

    def copy_system(self) -> None:
        self.step(0.09, "Copying PolyOS to the disk…")
        args = ["unsquashfs", "-f", "-d", str(TARGET), "-percentage", str(SQUASHFS)]
        self.r.note("$ " + " ".join(args))
        if self.dry:
            return
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        last = -1
        tail: list[str] = []
        for line in proc.stdout:
            line = line.strip()
            if line.isdigit():
                pct = int(line)
                if pct != last:
                    last = pct
                    self.emit({"progress": round(0.09 + 0.6 * pct / 100, 3), "message": f"Copying PolyOS to the disk… {pct}%"})
            elif line:
                tail = (tail + [line])[-20:]
        if proc.wait() != 0:
            self.r.note("\n".join(tail))
            raise InstallError("Copying PolyOS failed. The USB drive may be damaged; try writing it again.")

    def configure(self) -> None:
        self.step(0.70, "Setting up your computer…")
        plan = self.plan
        write = self._write
        for e in self.mounts:
            e["uuid"] = self._uuid(e["device"])
        self.swapfile = not any(e["mount"] == "swap" for e in self.mounts)
        if self.swapfile and not self.dry:
            swap = swap_bytes(_ram_bytes())
            self.r.run(["fallocate", "-l", str(swap), str(TARGET / "swapfile")], what="Creating the swap file")
            os.chmod(TARGET / "swapfile", 0o600)
            self.r.run(["mkswap", str(TARGET / "swapfile")], what="Creating the swap file")
        for e in self.mounts:  # mount points for storage kept as it is
            if STORAGE_RE.match(e["mount"]) and not self.dry:
                (TARGET / e["mount"].lstrip("/")).mkdir(parents=True, exist_ok=True)
        write("etc/fstab", fstab_entries(self.mounts, swapfile=self.swapfile))
        host = plan["hostname"]
        write("etc/hostname", host + "\n")
        write("etc/hosts", f"127.0.0.1\tlocalhost\n127.0.1.1\t{host}\n\n::1\tlocalhost ip6-localhost ip6-loopback\n"
                           "ff02::1\tip6-allnodes\nff02::2\tip6-allrouters\n")
        tz = plan["timezone"]
        if not self.dry and not (Path("/usr/share/zoneinfo") / tz).is_file():
            tz = "UTC"
        write("etc/timezone", tz + "\n")
        if not self.dry:
            localtime = TARGET / "etc/localtime"
            localtime.unlink(missing_ok=True)
            localtime.symlink_to(f"/usr/share/zoneinfo/{tz}")
        if self.windows_alongside:  # Windows keeps the hardware clock in local time
            write("etc/adjtime", "0.0 0 0.0\n0\nLOCAL\n")
        write("etc/default/locale", "LANG=en_US.UTF-8\n")
        write("etc/polyos/edition", plan.get("edition", "regular") + "\n")  # for edition updates (updates.local_edition)
        # the USB drive's boot menu looks for this to offer "Start PolyOS 7 on this computer"
        write("boot/polyos-installed", f"PolyOS {__version__}\n")
        if not self.dry:
            gen = TARGET / "etc/locale.gen"
            text = gen.read_text() if gen.exists() else ""
            if not re.search(r"^en_US\.UTF-8 UTF-8", text, re.M):
                gen.write_text(re.sub(r"^# ?en_US\.UTF-8 UTF-8", "en_US.UTF-8 UTF-8", text, flags=re.M)
                               if "en_US.UTF-8 UTF-8" in text else text + "en_US.UTF-8 UTF-8\n")
            (TARGET / "etc/machine-id").write_text("")  # a fresh id on first boot
            (TARGET / "var/lib/dbus/machine-id").unlink(missing_ok=True)
            # Wi-Fi networks joined during setup keep working after the restart
            src = Path("/etc/NetworkManager/system-connections")
            dst = TARGET / "etc/NetworkManager/system-connections"
            if src.is_dir() and dst.parent.is_dir():
                dst.mkdir(mode=0o700, exist_ok=True)
                for conn in src.glob("*.nmconnection"):
                    shutil.copy2(conn, dst / conn.name)
                    os.chmod(dst / conn.name, 0o600)

    def _write(self, rel: str, text: str) -> None:
        self.r.note(f"write /{rel}")
        if self.dry:
            return
        path = TARGET / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def _uuid(self, dev: str | None) -> str:
        if self.dry:
            return "0000-DRYRUN"
        out = self.r.run(["blkid", "-s", "UUID", "-o", "value", dev], what="Reading a partition id").strip()
        if not out:
            raise InstallError(f"{dev} has no filesystem id.")
        return out

    def bind_mounts(self) -> None:
        for src in ("dev", "dev/pts", "proc", "sys", "run"):
            dst = TARGET / src
            if not self.dry:
                dst.mkdir(parents=True, exist_ok=True)
            flag = "--rbind" if src == "sys" else "--bind"
            self.r.run(["mount", flag, f"/{src}", str(dst)], what=f"Preparing /{src}")
            self.mounted.append(dst)
        self._write("usr/sbin/policy-rc.d", "#!/bin/sh\nexit 101\n")  # no services start inside the chroot
        if not self.dry:
            os.chmod(TARGET / "usr/sbin/policy-rc.d", 0o755)

    def packages(self) -> None:
        self.step(0.74, "Removing the live-system tools…")
        installed = set(self.chroot(["dpkg-query", "-W", "-f", "${Package}\n"], check=False).split())
        remove = [p for p in LIVE_PACKAGES if p in installed] if not self.dry else LIVE_PACKAGES
        if remove:
            self.chroot(["apt-get", "-y", "purge", *remove], what="Removing live-system packages", timeout=900)
            self.chroot(["apt-get", "-y", "autoremove", "--purge"], check=False, timeout=900)
        self.chroot(["locale-gen"], check=False, timeout=300)

    def system_defaults(self) -> None:
        """Security and speed for the installed system: firewall on, automatic security updates,
        compressed RAM swap, SSD trimming, and no waiting for the network while starting up."""
        self.step(0.78, "Turning on security and speed settings…")
        if self.dry or (TARGET / "etc/ufw/ufw.conf").exists():
            ufw = "" if self.dry else (TARGET / "etc/ufw/ufw.conf").read_text()
            self._write("etc/ufw/ufw.conf", firewall_conf(ufw))
        if self.dry or (TARGET / "usr/bin/unattended-upgrade").exists():
            self._write("etc/apt/apt.conf.d/20auto-upgrades", AUTO_UPGRADES)
        if self.dry or (TARGET / "etc/default/zramswap").exists():
            self._write("etc/default/zramswap", ZRAMSWAP)
        for unit, state in (("fstrim.timer", "enable"), ("NetworkManager-wait-online.service", "disable"),
                            ("ufw.service", "enable"), ("zramswap.service", "enable")):
            self.chroot(["systemctl", state, unit], check=False)

    def create_user(self) -> None:
        self.step(0.80, "Creating your account…")
        user = self.plan["user"]
        name = user["username"]
        existing = set()
        if not self.dry:
            existing = {ln.split(":", 1)[0] for ln in (TARGET / "etc/passwd").read_text().splitlines() if ln}
            groups_file = {ln.split(":", 1)[0] for ln in (TARGET / "etc/group").read_text().splitlines() if ln}
        else:
            groups_file = set(USER_GROUPS)
        if name in existing:
            raise InstallError(f"The username “{name}” is already used by the system. Pick another.")
        groups = ",".join(g for g in USER_GROUPS if g in groups_file)
        self.chroot(["useradd", "-m", "-s", "/bin/bash", "-c", user["fullName"], "-G", groups, name], what="Creating your account")
        if user["password"]:
            self.chroot(["chpasswd"], input=f"{name}:{user['password']}\n", what="Setting your password")
        else:
            self.chroot(["passwd", "-d", name], what="Setting up sign-in without a password")
            self._write("etc/lightdm/lightdm.conf.d/60-polyos-autologin.conf",
                        f"# The account was created without a password: sign in automatically.\n[Seat:*]\n"
                        f"autologin-user={name}\nautologin-session=polyos\n")
        self._write(f"var/lib/AccountsService/users/{name}", "[User]\nSession=polyos\nXSession=polyos\nSystemAccount=false\n")
        if user.get("recoveryKey") and not self.dry:  # only its hash is stored
            recovery.save_record(name, recovery.make_record(user["recoveryKey"]), root=TARGET)
        # PolyOS settings from the setup screens, plus the wallpapers in Pictures
        appearance = self.plan["appearance"]
        settings = {"theme": appearance["theme"], "accent": appearance["accent"], "setupDone": True,
                    **self.plan.get("extraSettings", {})}
        home = f"home/{name}"
        existing = TARGET / home / ".config/polyos/settings.json"
        if not self.dry and existing.is_file():  # a kept /home: keep its PolyOS settings
            try:
                settings = {**json.loads(existing.read_text("utf-8")), **settings, "theme": settings["theme"]}
            except (OSError, ValueError):
                pass
        self._write(f"{home}/.config/polyos/settings.json", json.dumps(settings, indent=2) + "\n")
        if self.plan.get("polyAccount"):  # connected during setup: this computer stays connected
            self._write(f"{home}/.config/polyos/poly-account.json", json.dumps(self.plan["polyAccount"], indent=2) + "\n")
            if not self.dry:
                os.chmod(TARGET / home / ".config/polyos/poly-account.json", 0o600)
        first = self.plan.get("firstStart") or {}
        if firststart.pending(first):  # drivers and apps that need the internet: after the restart, by themselves
            if not self.dry:
                firststart.write_plan(first, TARGET)
            self.chroot(["systemctl", "enable", "polyos-first-start.timer"], check=False)
        if not self.dry:
            walls = Path("/usr/share/polyos/wallpapers")
            pics = TARGET / home / "Pictures" / "Wallpapers"
            pics.mkdir(parents=True, exist_ok=True)
            for wall in walls.glob("*.jpg") if walls.is_dir() else []:
                shutil.copy2(wall, pics / wall.name)
        self.chroot(["chown", "-R", f"{name}:{name}", f"/{home}"], what="Setting up your home folder")
        for e in self.mounts:  # new storage drives belong to you
            if STORAGE_RE.match(e["mount"]) and e["format"]:
                self.chroot(["chown", f"{name}:{name}", e["mount"]], check=False)
        uid = 1000
        if not self.dry:
            out = self.chroot(["id", "-u", name], check=False).strip()
            uid = int(out) if out.isdigit() else 1000
        if uid != 1000 and any(e["fs"] in ("ntfs", "vfat", "exfat") and e["mount"] != "/boot/efi" for e in self.mounts):
            self._write("etc/fstab", fstab_entries(self.mounts, uid=uid, swapfile=self.swapfile))

    def bootloader(self) -> None:
        self.step(0.86, "Installing the boot loader…")
        dual = self.dual
        self._write("etc/default/grub",
                    "# Written by the PolyOS installer. Run update-grub after editing.\n"
                    # straight to PolyOS; with Windows too, Shift or Esc shows the menu, and Start ›
                    # Power › Restart to Windows picks it for one start (grub-reboot needs "saved")
                    "GRUB_DEFAULT=saved\n"
                    "GRUB_TIMEOUT=2\n"
                    "GRUB_TIMEOUT_STYLE=hidden\n"
                    'GRUB_DISTRIBUTOR="PolyOS"\n'
                    'GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"\n'
                    'GRUB_CMDLINE_LINUX=""\n'
                    f"GRUB_DISABLE_OS_PROBER={'false' if dual else 'true'}\n"
                    + (f'GRUB_THEME="{GRUB_THEME}/theme.txt"\nGRUB_GFXMODE=auto\nGRUB_TERMINAL_OUTPUT=gfxterm\n'
                       if self.dry or (TARGET / GRUB_THEME.lstrip("/") / "theme.txt").exists() else ""))
        debs = []
        if not self.dry:
            kind = "efi" if self.uefi else "bios"
            folder = TARGET / OFFLINE_DEBS.lstrip("/") / kind
            debs = [f"{OFFLINE_DEBS}/{kind}/{p.name}" for p in sorted(folder.glob("*.deb"))]
        if not self.uefi:
            by_id = self._by_id(self.disk)
            self.chroot(["debconf-set-selections"], input=f"grub-pc grub-pc/install_devices multiselect {by_id}\n", check=False)
        if debs:
            self.chroot(["dpkg", "-i", *debs], check=False, what="Installing GRUB", timeout=600)
        if self.uefi:
            target, shim, _grub = EFI.get(self.arch, EFI["amd64"])
            signed = self.dry or (TARGET / f"usr/lib/shim/{shim}.signed").exists()
            args = ["grub-install", f"--target={target}", "--efi-directory=/boot/efi", "--bootloader-id=debian", "--recheck",
                    "--uefi-secure-boot" if signed else "--no-uefi-secure-boot"]
            if not dual:
                args.append("--force-extra-removable")  # for firmware that ignores boot entries
            self.chroot(args, what="Installing the boot loader", timeout=600)
            self._efi_label()
        elif self.arch != "amd64":
            raise InstallError("This computer didn't start in UEFI mode. ARM computers need UEFI firmware to run PolyOS.")
        else:
            self.chroot(["grub-install", "--target=i386-pc", "--recheck", self.disk], what="Installing the boot loader", timeout=600)
        self.grub_fonts()
        self.step(0.92, "Finishing the boot menu…")
        self.chroot(["update-initramfs", "-u", "-k", "all"], what="Building the startup image", timeout=900)
        self.chroot(["update-grub"], what="Creating the boot menu", timeout=900)
        if not self.dry:
            shutil.rmtree(TARGET / OFFLINE_DEBS.lstrip("/"), ignore_errors=True)

    def grub_fonts(self) -> None:
        """The boot menu's font: GRUB reads its own .pf2 format, made here from Poppins (DejaVu if it's missing)."""
        theme = TARGET / GRUB_THEME.lstrip("/")
        if not self.dry and not (theme / "theme.txt").exists():
            return
        for ttf in ("/usr/share/fonts/truetype/polyos/Poppins-Medium.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
            if self.dry or (TARGET / ttf.lstrip("/")).exists():
                # -n sets the name theme.txt asks for, whichever font it came from
                self.chroot(["grub-mkfont", "-s", "16", "-n", "Poppins Regular 16", "-o", f"{GRUB_THEME}/polyos-16.pf2", ttf],
                            check=False)
                break

    def _by_id(self, disk: str) -> str:
        folder = Path("/dev/disk/by-id")
        if folder.is_dir():
            for link in sorted(folder.iterdir()):
                if link.name.startswith(("ata-", "nvme-", "scsi-", "mmc-", "usb-")) and "-part" not in link.name:
                    try:
                        if os.path.realpath(link) == os.path.realpath(disk):
                            return str(link)
                    except OSError:
                        continue
        return disk

    def _efi_label(self) -> None:
        """Show "PolyOS" in the firmware boot menu (grub-install names its entry "debian")."""
        if not _have("efibootmgr") or not self.esp_dev:
            return
        entries = efi_entries(self.r.run(["efibootmgr"], check=False))
        part = self._partition(self.esp_dev)
        esp_disk = self._parent(self.esp_dev) if part else (self.disk if self.esp_is_new else disk_of(self.esp_dev))
        number = part["number"] if part and part.get("number") else None
        if number is None:
            m = re.search(r"(\d+)$", self.esp_dev)
            number = int(m.group(1)) if m else 1
        for num, label in entries.items():
            if label == "PolyOS":
                self.r.run(["efibootmgr", "-q", "-b", num, "-B"], check=False)
        _target, shim, grub = EFI.get(self.arch, EFI["amd64"])
        loader = f"\\EFI\\debian\\{shim}" if (self.dry or (TARGET / f"boot/efi/EFI/debian/{shim}").exists()) \
            else f"\\EFI\\debian\\{grub}"
        self.r.run(["efibootmgr", "-q", "-c", "-d", esp_disk, "-p", str(number), "-L", "PolyOS", "-l", loader], check=False)
        now = efi_entries(self.r.run(["efibootmgr"], check=False))
        ours = next((num for num, label in now.items() if label == "PolyOS"), None)
        if self.dry or ours:
            for num, label in entries.items():
                if label.lower() == "debian":
                    self.r.run(["efibootmgr", "-q", "-b", num, "-B"], check=False)
        if ours:  # start the installed PolyOS next time, even if the USB drive is still plugged in
            self.r.run(["efibootmgr", "-q", "-n", ours], check=False)

    def cleanup(self) -> None:
        if not self.dry:
            (TARGET / "usr/sbin/policy-rc.d").unlink(missing_ok=True)
            try:
                log_copy = TARGET / "var/log/polyos-installer.log"
                if LOG_PATH.exists() and log_copy.parent.is_dir():
                    shutil.copy2(LOG_PATH, log_copy)
                    os.chmod(log_copy, 0o600)
            except OSError:
                pass
        self.unmount()
        self.r.run(["sync"], check=False)

    def unmount(self) -> None:
        for path in reversed(self.mounted):
            self.r.run(["umount", "-R", "-l", str(path)], check=False)
        self.mounted.clear()

    def run(self) -> None:
        try:
            self.preflight()
            if self.plan["mode"] == "erase":
                self.partition_erase()
            elif self.plan["mode"] == "space":
                self.partition_space()
            elif self.plan["mode"] == "custom":
                self.partition_custom()
            else:
                self.partition_alongside()
            if not self.root_dev:
                raise InstallError("PolyOS's partition wasn't created.")
            self.format_and_mount()
            self.copy_system()
            self.configure()
            self.bind_mounts()
            self.packages()
            self.system_defaults()
            self.create_user()
            self.bootloader()
            self.step(0.97, "Cleaning up…")
            self.cleanup()
            self.emit({"progress": 1.0, "message": "PolyOS is installed.", "done": True})
        except InstallError:
            self.unmount()
            raise
        except Exception as exc:  # noqa: BLE001 - anything unexpected still ends with a clean unmount
            self.r.note(f"unexpected error: {exc!r}")
            self.unmount()
            raise InstallError(f"Something unexpected went wrong ({exc}). Details are in {LOG_PATH}.") from exc
