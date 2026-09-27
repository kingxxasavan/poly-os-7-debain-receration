"""Task Manager data: the user's processes grouped by app, and live performance numbers.

Everything comes from /proc. CPU percentages are measured between two samples (like
Windows Task Manager, 100% means every core is busy). Memory per process is the private
resident memory (resident minus shared pages), which adds up sensibly across processes.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
# Processes that make up the desktop itself: shown under "PolyOS" and never ended from here.
DESKTOP_PROCESSES = {"polyos-session", "polyos-shell", "openbox", "picom", "xcape", "lxpolkit", "Xorg",
                     "light-locker", "polkit-mate-authentication-agent-1", "dbus-daemon", "pipewire",
                     "wireplumber", "pipewire-pulse", "systemd", "at-spi-bus-launcher", "at-spi2-registryd",
                     "xfce4-notifyd", "gvfsd", "polyos-greeter"}


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def parse_stat(text: str) -> dict | None:
    """/proc/<pid>/stat: the name is in parentheses and may itself contain spaces or ')'."""
    try:
        head, _, rest = text.rpartition(")")
        name = head.split("(", 1)[1]
        f = rest.split()
        return {"name": name, "state": f[0], "ppid": int(f[1]), "ticks": int(f[11]) + int(f[12]),
                "threads": int(f[17]), "start": int(f[19])}
    except (IndexError, ValueError):
        return None


def parse_meminfo(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        parts = value.split()
        if parts and parts[0].isdigit():
            out[key] = int(parts[0]) * 1024
    return out


def parse_cpu_times(text: str) -> tuple[list[int], list[list[int]]]:
    """(total "cpu" line, per-core lines) from /proc/stat, each as [busy, total] jiffies."""
    total, cores = [0, 0], []
    for line in text.splitlines():
        if not line.startswith("cpu"):
            continue
        nums = [int(x) for x in line.split()[1:]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        pair = [sum(nums) - idle, sum(nums)]
        if line.startswith("cpu "):
            total = pair
        else:
            cores.append(pair)
    return total, cores


def parse_net_dev(text: str) -> tuple[int, int]:
    rx = tx = 0
    for line in text.splitlines()[2:]:
        name, _, data = line.partition(":")
        name = name.strip()
        if not data or name == "lo" or name.startswith(("veth", "docker", "br-", "virbr")):
            continue
        f = data.split()
        rx += int(f[0])
        tx += int(f[8])
    return rx, tx


def parse_diskstats(text: str) -> tuple[int, int]:
    """Sectors read/written by whole disks (not partitions, loops or ramdisks)."""
    rd = wr = 0
    for line in text.splitlines():
        f = line.split()
        if len(f) < 10:
            continue
        name = f[2]
        if name.startswith(("loop", "ram", "zram", "sr", "dm-", "md")):
            continue
        if (name.startswith("nvme") and "p" in name[4:]) or (name[:2] in ("sd", "vd", "hd") and name[-1].isdigit()) \
                or (name.startswith("mmcblk") and "p" in name[6:]):
            continue  # a partition
        rd += int(f[5])
        wr += int(f[9])
    return rd * 512, wr * 512


def _cpu_model() -> str:
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return ""


def user_processes(uid: int | None = None) -> list[tuple[int, set[str]]]:
    """(pid, names) for the person's processes, oldest first: the kernel's name and the program's file name."""
    uid = os.getuid() if uid is None and hasattr(os, "getuid") else uid
    out = []
    try:
        entries = list(os.scandir("/proc"))
    except OSError:
        return out
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
        except OSError:
            continue
        stat = parse_stat(_read(f"/proc/{entry.name}/stat"))
        argv0 = _read(f"/proc/{entry.name}/cmdline").split("\0", 1)[0]
        if stat is None or stat["state"] == "Z":
            continue
        out.append((int(entry.name), {stat["name"], Path(argv0).name} - {""}))
    return sorted(out)


class ProcessMonitor:
    def __init__(self, protected: set[int] | None = None):
        self.uid = os.getuid() if hasattr(os, "getuid") else 0
        self.protected = protected or set()
        self._prev_ticks: dict[int, int] = {}
        self._prev_cpu: list[int] | None = None
        self._prev_cores: list[list[int]] = []
        self._prev_io: tuple[float, int, int, int, int] | None = None
        self._cpu_model = _cpu_model()

    def _processes(self) -> dict[int, dict]:
        procs = {}
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                if entry.stat().st_uid != self.uid:
                    continue
            except OSError:
                continue
            stat = parse_stat(_read(f"/proc/{pid}/stat"))
            if stat is None:
                continue
            statm = _read(f"/proc/{pid}/statm").split()
            private = (int(statm[1]) - int(statm[2])) * PAGE if len(statm) > 2 else 0
            cmdline = _read(f"/proc/{pid}/cmdline").replace("\0", " ").strip()
            procs[pid] = {**stat, "pid": pid, "memory": max(0, private), "cmdline": cmdline[:300]}
        return procs

    def sample(self, windows: list[dict]) -> dict:
        """windows: [{"pid", "title", "appId", "icon", "name"}] from the shell's window list."""
        cpu_now, cores_now = parse_cpu_times(_read("/proc/stat"))
        procs = self._processes()
        dtotal = max(1, cpu_now[1] - self._prev_cpu[1]) if self._prev_cpu else 0
        for pid, p in procs.items():
            before = self._prev_ticks.get(pid)
            p["cpu"] = round(100 * (p["ticks"] - before) / dtotal, 1) if dtotal and before is not None else 0.0
        self._prev_ticks = {pid: p["ticks"] for pid, p in procs.items()}

        # ---- group: each app window owns its process and that process's descendants ----
        children: dict[int, list[int]] = {}
        for pid, p in procs.items():
            children.setdefault(p["ppid"], []).append(pid)
        apps, claimed = [], set()
        for win in windows:
            pid = win.get("pid") or 0
            if pid not in procs or pid in claimed:
                continue
            tree, stack = [], [pid]
            while stack:
                cur = stack.pop()
                if cur in claimed or cur not in procs:
                    continue
                claimed.add(cur)
                tree.append(cur)
                stack.extend(children.get(cur, []))
            members = [procs[t] for t in tree]
            apps.append({
                "pid": pid, "name": win.get("name") or win.get("title") or procs[pid]["name"], "title": win.get("title", ""),
                "icon": win.get("icon"), "cpu": round(sum(m["cpu"] for m in members), 1),
                "memory": sum(m["memory"] for m in members), "count": len(members),
                "status": "Not responding" if any(m["state"] == "D" for m in members) else "Running",
                "protected": pid in self.protected,
            })
        background, desktop = [], []
        for pid, p in procs.items():
            if pid in claimed:
                continue
            row = {"pid": pid, "name": p["name"], "cmdline": p["cmdline"], "cpu": p["cpu"], "memory": p["memory"],
                   "count": 1, "status": {"R": "Running", "S": "Running", "D": "Waiting", "T": "Suspended", "Z": "Ended"}.get(p["state"], "Running"),
                   "protected": pid in self.protected or p["name"] in DESKTOP_PROCESSES}
            (desktop if row["protected"] else background).append(row)
        for group in (apps, background, desktop):
            group.sort(key=lambda r: (-r["cpu"], -r["memory"]))

        # ---- performance ----
        mem = parse_meminfo(_read("/proc/meminfo"))
        cores = []
        for i, pair in enumerate(cores_now):
            prev = self._prev_cores[i] if i < len(self._prev_cores) else None
            d = pair[1] - prev[1] if prev else 0
            cores.append(round(100 * (pair[0] - prev[0]) / d, 1) if prev and d > 0 else 0.0)
        cpu_pct = round(100 * (cpu_now[0] - self._prev_cpu[0]) / dtotal, 1) if dtotal else 0.0
        self._prev_cpu, self._prev_cores = cpu_now, cores_now
        now = time.monotonic()
        rx, tx = parse_net_dev(_read("/proc/net/dev"))
        rd, wr = parse_diskstats(_read("/proc/diskstats"))
        rates = {"netDown": 0, "netUp": 0, "diskRead": 0, "diskWrite": 0}
        if self._prev_io:
            t, prx, ptx, prd, pwr = self._prev_io
            dt = max(0.001, now - t)
            rates = {"netDown": max(0, int((rx - prx) / dt)), "netUp": max(0, int((tx - ptx) / dt)),
                     "diskRead": max(0, int((rd - prd) / dt)), "diskWrite": max(0, int((wr - pwr) / dt))}
        self._prev_io = (now, rx, tx, rd, wr)
        total_mem = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        uptime = float((_read("/proc/uptime").split() or ["0"])[0])
        return {
            "apps": apps, "background": background, "desktop": desktop,
            "perf": {
                "cpu": max(0.0, min(100.0, cpu_pct)), "cores": cores, "cpuModel": self._cpu_model,
                "memTotal": total_mem, "memUsed": max(0, total_mem - avail), "memCached": mem.get("Cached", 0),
                "swapTotal": mem.get("SwapTotal", 0), "swapUsed": max(0, mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)),
                "processes": len(procs), "threads": sum(p["threads"] for p in procs.values()),
                "uptime": int(uptime), **rates,
            },
        }

    def end(self, pid: int, force: bool = False) -> None:
        """End a process and its children (SIGTERM, or SIGKILL when forced)."""
        procs = self._processes()
        if pid not in procs:
            raise ProcessLookupError("That process has already ended.")
        if pid in self.protected or procs[pid]["name"] in DESKTOP_PROCESSES:
            raise PermissionError("That's part of PolyOS itself and can't be ended here.")
        children: dict[int, list[int]] = {}
        for p in procs.values():
            children.setdefault(p["ppid"], []).append(p["pid"])
        order, stack = [], [pid]
        while stack:
            cur = stack.pop()
            order.append(cur)
            stack.extend(children.get(cur, []))
        sig = signal.SIGKILL if force else signal.SIGTERM
        for target in reversed(order):  # children first, so the parent can't respawn them
            try:
                os.kill(target, sig)
            except ProcessLookupError:
                pass


def protected_pids() -> set[int]:
    """This process and its ancestors (the shell, the session, the display manager)."""
    out, pid = set(), os.getpid()
    for _ in range(12):
        out.add(pid)
        stat = parse_stat(_read(f"/proc/{pid}/stat"))
        if not stat or stat["ppid"] <= 1:
            break
        pid = stat["ppid"]
    return out


def cpu_count() -> int:
    return os.cpu_count() or 1


def boot_time() -> float:
    for line in _read("/proc/stat").splitlines():
        if line.startswith("btime"):
            return float(line.split()[1])
    return time.time()


def exists(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()
