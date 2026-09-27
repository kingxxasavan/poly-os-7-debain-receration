import json
import unittest
from unittest import mock

from polyos import installer
from polyos.installer import GiB, MiB, InstallError

LSBLK = {"blockdevices": [
    {"name": "/dev/nvme0n1", "type": "disk", "size": 512110190592, "model": "Samsung SSD 970 EVO Plus 500GB", "tran": "nvme",
     "rm": False, "ro": False, "fstype": None, "label": None, "parttype": None, "partn": None, "mountpoints": [None],
     "pttype": "gpt", "children": [
         {"name": "/dev/nvme0n1p1", "type": "part", "size": 104857600, "fstype": "vfat", "label": "SYSTEM",
          "parttype": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "partn": 1, "mountpoints": [None]},
         {"name": "/dev/nvme0n1p3", "type": "part", "size": 510000000000, "fstype": "ntfs", "label": "Windows",
          "parttype": "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7", "partn": 3, "mountpoints": [None]}]},
    {"name": "/dev/sdb", "type": "disk", "size": 32000000000, "model": "Ultra", "tran": "usb", "rm": True, "ro": False,
     "pttype": "dos", "mountpoints": [None], "children": [
         {"name": "/dev/sdb1", "type": "part", "size": 32000000000, "fstype": "iso9660", "partn": 1,
          "mountpoints": ["/run/live/medium"]}]},
    {"name": "/dev/zram0", "type": "disk", "size": 4000000000},
    {"name": "/dev/loop0", "type": "loop", "size": 1500000000},
]}

SFDISK = {"partitiontable": {"label": "gpt", "id": "X", "device": "/dev/sda", "unit": "sectors", "firstlba": 34,
                             "lastlba": 1000215182, "sectorsize": 512, "partitions": [
                                 {"node": "/dev/sda1", "start": 2048, "size": 1048576, "type": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"},
                                 {"node": "/dev/sda2", "start": 1050624, "size": 400000000, "type": "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"}]}}


def disk(size=500 * GiB, table="gpt", parts=None):
    return {"path": "/dev/sda", "size": size, "model": "Disk", "transport": "sata", "removable": False,
            "readonly": False, "table": table, "mounts": [], "partitions": parts or []}


def part(path, number, size, fstype="ntfs", parttype=installer.MS_BASIC_GUID.lower()):
    return {"path": path, "number": number, "size": size, "fstype": fstype, "label": "", "parttype": parttype, "mounts": []}


class ParserTests(unittest.TestCase):
    def test_lsblk(self):
        disks = installer.parse_lsblk(LSBLK)
        self.assertEqual([d["path"] for d in disks], ["/dev/nvme0n1", "/dev/sdb"])  # no zram, no loop
        nvme = disks[0]
        self.assertEqual(nvme["table"], "gpt")
        self.assertFalse(nvme["removable"])
        self.assertEqual([p["number"] for p in nvme["partitions"]], [1, 3])
        self.assertEqual(disks[1]["partitions"][0]["mounts"], ["/run/live/medium"])
        self.assertTrue(disks[1]["removable"])

    def test_sfdisk_and_free_space(self):
        table = installer.parse_sfdisk(SFDISK)
        self.assertEqual(table["sector"], 512)
        self.assertEqual(table["partitions"][1]["number"], 2)
        regions = installer.free_regions(table, 1000215216 * 512)
        self.assertEqual(len(regions), 1)
        start = regions[0]["start"]
        self.assertEqual(start % 2048, 0)
        self.assertGreaterEqual(start, 1050624 + 400000000)
        self.assertLessEqual(start + regions[0]["size"], 1000215182 + 1)

    def test_os_prober(self):
        text = ("/dev/nvme0n1p1@/efi/Microsoft/Boot/bootmgfw.efi:Windows Boot Manager:Windows:efi\n"
                "/dev/sda2:Ubuntu 24.04 LTS (24.04):Ubuntu:linux\n")
        self.assertEqual(installer.parse_os_prober(text),
                         {"/dev/nvme0n1p1": "Windows", "/dev/sda2": "Ubuntu 24.04 LTS (24.04)"})

    def test_ntfsresize(self):
        ok = "ntfsresize v2022\nChecking filesystem consistency ...\nYou might resize at 131234525184 bytes or 131235 MB (freeing 378765 MB).\n"
        self.assertEqual(installer.parse_ntfsresize_info(ok), (131234525184, None))
        minimum, reason = installer.parse_ntfsresize_info("ERROR: The NTFS partition is hibernated. Windows must be resumed")
        self.assertIsNone(minimum)
        self.assertIn("Fast Startup", reason)

    def test_efibootmgr(self):
        text = ("BootCurrent: 0001\nBootOrder: 0003,0001,0000\n"
                "Boot0000* Windows Boot Manager\tHD(1,GPT,a,0x800,0x32000)/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)\n"
                "Boot0003* debian\tHD(1,GPT,a,0x800,0x32000)/File(\\EFI\\debian\\shimx64.efi)\n")
        self.assertEqual(installer.efi_entries(text), {"0000": "Windows Boot Manager", "0003": "debian"})

    def test_partition_nodes(self):
        self.assertEqual(installer.partition_node("/dev/sda", 2), "/dev/sda2")
        self.assertEqual(installer.partition_node("/dev/nvme0n1", 2), "/dev/nvme0n1p2")
        self.assertEqual(installer.partition_node("/dev/mmcblk0", 1), "/dev/mmcblk0p1")


class PlanningTests(unittest.TestCase):
    def test_free_space_next_to_windows(self):
        d = disk(parts=[part("/dev/sda1", 1, 100 * MiB, "vfat", installer.ESP_GUID.lower()), part("/dev/sda2", 2, 200 * GiB)])
        d["free"] = [{"start": 420000000, "bytes": 290 * GiB}]
        for p in d["partitions"]:
            p["esp"] = installer.is_esp(p, "gpt")
        opt = installer.alongside_option(d, uefi=True)
        self.assertEqual(opt["kind"], "free")
        self.assertEqual(opt["extra"], 0)  # Windows' EFI partition is reused
        self.assertEqual(opt["maxBytes"], 290 * GiB)

    def test_shrink_windows(self):
        win = part("/dev/sda2", 2, 400 * GiB)
        win["resize"] = {"fs": "ntfs", "min": 120 * GiB, "used": 120 * GiB, "reason": None}
        d = disk(parts=[part("/dev/sda1", 1, 100 * MiB, "vfat", installer.ESP_GUID.lower()), win])
        d["free"] = []
        for p in d["partitions"]:
            p["esp"] = installer.is_esp(p, "gpt")
        opt = installer.alongside_option(d, uefi=True)
        self.assertEqual(opt["kind"], "shrink")
        self.assertEqual(opt["partition"], "/dev/sda2")
        self.assertLessEqual(opt["maxBytes"], 400 * GiB - 120 * GiB - installer.KEEP_FREE)
        self.assertGreaterEqual(opt["suggested"], installer.MIN_ROOT)

    def test_refusals(self):
        locked = part("/dev/sda2", 2, 400 * GiB, "BitLocker")
        locked["resize"] = {"fs": "bitlocker", "min": None, "reason": "This partition is encrypted with BitLocker."}
        d = disk(parts=[locked])
        d["free"] = []
        opt = installer.alongside_option(d, uefi=False)
        self.assertFalse(opt["possible"])
        self.assertIn("BitLocker", opt["reason"])
        legacy = disk(table="dos", parts=[part("/dev/sda1", 1, 400 * GiB, parttype="7")])
        legacy["free"] = [{"start": 2048, "bytes": 90 * GiB}]
        self.assertIn("legacy BIOS", installer.alongside_option(legacy, uefi=True)["reason"])
        full = disk(table="dos", parts=[part(f"/dev/sda{i}", i, 10 * GiB, parttype="83") for i in range(1, 5)])
        full["free"] = [{"start": 2048, "bytes": 90 * GiB}]
        self.assertIn("four primary", installer.alongside_option(full, uefi=False)["reason"])
        self.assertIn("empty", installer.alongside_option(disk(table=None), uefi=True)["reason"])

    def test_describe_marks_live_disk(self):
        disks = installer.parse_lsblk(LSBLK)
        live = installer.describe_disk(disks[1], None, {}, True, "/dev/sdb", {})
        self.assertTrue(live["isLive"])
        self.assertFalse(live["canErase"])
        windows = installer.describe_disk(disks[0], None, {"/dev/nvme0n1p1": "Windows 11"}, True, "/dev/sdb", {})
        self.assertEqual(windows["oses"], ["Windows 11"])

    def test_layouts(self):
        self.assertIn("label: gpt", installer.erase_script("gpt", uefi=True))
        self.assertIn(installer.ESP_GUID, installer.erase_script("gpt", uefi=True))
        self.assertIn("bootable", installer.erase_script("dos", uefi=False))
        self.assertIn(installer.BIOS_BOOT_GUID, installer.erase_script("gpt", uefi=False))
        parts = installer.alongside_layout(1000001, 40 * GiB, 512, "gpt", need_esp=True, need_bios=False)
        self.assertEqual([p["role"] for p in parts], ["esp", "root"])
        self.assertEqual(parts[0]["start"] % 2048, 0)
        self.assertEqual(parts[1]["start"], parts[0]["start"] + parts[0]["size"])
        self.assertEqual(parts[1]["size"] * 512, 40 * GiB)

    def test_fstab_and_swap(self):
        text = installer.fstab("r-uuid", "e-uuid", swapfile=True)
        self.assertIn("UUID=r-uuid  /  ext4", text)
        self.assertIn("/boot/efi", text)
        self.assertIn("/swapfile", text)
        self.assertNotIn("/boot/efi", installer.fstab("r", None, False))
        self.assertEqual(installer.swap_bytes(2 * GiB), 2 * GiB)
        self.assertEqual(installer.swap_bytes(32 * GiB), 4 * GiB)
        self.assertEqual(installer.swap_bytes(512 * MiB), 1 * GiB)


class PlanValidationTests(unittest.TestCase):
    def plan(self, **over):
        base = {"mode": "erase", "disk": "/dev/sda", "hostname": "savan-polyos", "timezone": "America/New_York",
                "user": {"fullName": "Savan Patel", "username": "savan", "password": "hunter2hunter2"},
                "appearance": {"theme": "light", "accent": "#9B7FE0"}}
        base.update(over)
        return base

    def test_valid(self):
        clean = installer.validate_plan(self.plan())
        self.assertEqual(clean["appearance"], {"theme": "light", "accent": "#9b7fe0"})
        self.assertEqual(clean["user"]["username"], "savan")

    def test_rejects(self):
        bad = [
            {"mode": "format-everything"},
            {"disk": "/dev/sda; rm -rf /"},
            {"user": {"fullName": "x", "username": "Root", "password": ""}},
            {"user": {"fullName": "x", "username": "root", "password": ""}},
            {"user": {"fullName": "a:b", "username": "ab", "password": ""}},
            {"hostname": "-bad-"},
            {"timezone": "../../etc/shadow"},
            {"mode": "alongside", "size": 1024},
        ]
        for over in bad:
            with self.subTest(over=over), self.assertRaises(InstallError):
                installer.validate_plan(self.plan(**over))

    def test_editions(self):
        self.assertEqual(installer.validate_plan(self.plan())["edition"], "regular")
        gaming = installer.validate_plan(self.plan(edition="gaming"))
        self.assertTrue(gaming["extraSettings"]["gameMode"])
        # setup asked everything on the USB drive: the apps install by themselves after the restart
        self.assertTrue(gaming["extraSettings"]["editionSetup"])
        self.assertEqual(gaming["firstStart"]["pack"], "gaming")
        self.assertIsNone(installer.validate_plan(self.plan())["firstStart"]["pack"])
        dev = installer.validate_plan(self.plan(edition="developer"))
        self.assertTrue(dev["extraSettings"]["developerMode"])
        with self.assertRaises(InstallError):
            installer.validate_plan(self.plan(edition="hacker"))
        # settings can't be smuggled in through the plan: they follow from the edition alone
        smuggled = installer.validate_plan(self.plan(extraSettings={"developerMode": True, "setupDone": True}))
        self.assertEqual(smuggled["extraSettings"]["developerMode"], False)
        self.assertNotIn("setupDone", smuggled["extraSettings"])

    def test_everything_from_setup(self):
        """The hardware check's choice, its drivers and a Poly Account connection travel with the plan."""
        clean = installer.validate_plan(self.plan(profile="light", background="reduced",
                                                  drivers=["firmware-iwlwifi", "nvidia-driver", "rm -rf /", "firmware-iwlwifi"],
                                                  polyAccount={"credential": "pd_abcdefghijklmnop", "account": {"email": "a@b.c"},
                                                               "sync": True, "evil": "x"}))
        self.assertEqual(clean["extraSettings"]["performanceProfile"], "light")
        self.assertEqual(clean["extraSettings"]["backgroundLimit"], "reduced")
        self.assertEqual(clean["firstStart"]["drivers"], ["firmware-iwlwifi", "nvidia-driver"])
        self.assertEqual(clean["polyAccount"], {"credential": "pd_abcdefghijklmnop", "account": {"email": "a@b.c"}, "sync": True})
        odd = installer.validate_plan(self.plan(profile="turbo", polyAccount={"credential": "no spaces allowed"}))
        self.assertNotIn("performanceProfile", odd["extraSettings"])
        self.assertIsNone(odd["polyAccount"])

    def test_blank_password_allowed(self):
        clean = installer.validate_plan(self.plan(user={"fullName": "", "username": "andrew", "password": ""}))
        self.assertEqual(clean["user"]["fullName"], "andrew")


class DryRunTests(unittest.TestCase):
    def test_erase_dry_run_emits_commands_in_order(self):
        events = []
        plan = installer.validate_plan({"mode": "erase", "disk": "/dev/sda", "hostname": "t-polyos", "timezone": "UTC",
                                        "user": {"fullName": "T", "username": "tester", "password": "pw"},
                                        "appearance": {"theme": "dark", "accent": "#678fd9"}})
        with mock.patch.object(installer, "live_disk", return_value="/dev/sdb"), \
                mock.patch("pathlib.Path.is_dir", return_value=True):
            installer.Installer(plan, events.append, dry_run=True).run()
        commands = [e["log"].replace("\\", "/") for e in events if "log" in e]
        joined = "\n".join(commands)
        self.assertTrue(events[-1].get("done"))
        order = ["wipefs", "sfdisk --wipe", "mkfs.ext4", "mkfs.vfat", "chroot /mnt/polyos-target useradd",
                 "chpasswd", "grub-install --target=x86_64-efi", "update-grub", "umount"]
        positions = [next(i for i, c in enumerate(commands) if key in c) for key in order]
        self.assertEqual(positions, sorted(positions), joined)
        self.assertNotIn("pw", " ".join(c for c in commands if "chpasswd" in c))  # the password goes via stdin
        self.assertNotIn("polyos-first-start", joined)  # Regular with no drivers: nothing to finish later

    def test_first_start_and_boot_next(self):
        """Gaming: the apps install after the restart; the firmware starts PolyOS next, not the USB drive."""
        events = []
        plan = installer.validate_plan({"mode": "erase", "disk": "/dev/sda", "hostname": "t-polyos", "timezone": "UTC",
                                        "user": {"fullName": "T", "username": "tester", "password": "pw"}, "edition": "gaming",
                                        "appearance": {"theme": "dark", "accent": "#678fd9"}})
        efi = "BootCurrent: 0003\nBoot0001* Windows Boot Manager\tHD(1)\nBoot0004* PolyOS\tHD(1,GPT)\n"
        with mock.patch.object(installer, "live_disk", return_value="/dev/sdb"), \
                mock.patch("pathlib.Path.is_dir", return_value=True), \
                mock.patch.object(installer, "_have", return_value=True), \
                mock.patch.object(installer.Runner, "run", autospec=True,
                                  side_effect=lambda self, args, **kw: (self.emit({"log": "$ " + " ".join(args)}),
                                                                       efi if args == ["efibootmgr"] else "")[1]):
            installer.Installer(plan, events.append, dry_run=True).run()
        joined = "\n".join(e["log"] for e in events if "log" in e)
        self.assertIn("systemctl enable polyos-first-start.timer", joined)
        self.assertIn("efibootmgr -q -n 0004", joined)


if __name__ == "__main__":
    unittest.main()


class CustomLayoutTests(unittest.TestCase):
    """Custom mode: the person chooses what each drive and partition is for."""

    def plan(self, wipe=None, mounts=None):
        return {"mode": "custom", "hostname": "t-polyos", "timezone": "UTC", "wipe": wipe or {}, "mounts": mounts or [],
                "user": {"fullName": "T", "username": "tester", "password": "pw"},
                "appearance": {"theme": "dark", "accent": "#678fd9"}}

    def test_valid_layouts(self):
        clean = installer.validate_plan(self.plan(wipe={"/dev/nvme0n1": "/", "/dev/sda": "/home"}))
        self.assertEqual(clean["disk"], "/dev/nvme0n1")
        clean = installer.validate_plan(self.plan(mounts=[
            {"device": "/dev/nvme0n1p5", "mount": "/", "format": True},
            {"device": "/dev/nvme0n1p1", "mount": "/boot/efi", "format": False},
            {"device": "/dev/sdb1", "mount": "/home", "format": False},
            {"device": "/dev/sdb2", "mount": "/mnt/games", "format": True},
            {"device": "/dev/nvme0n1p6", "mount": "swap", "format": True}]))
        self.assertEqual(clean["disk"], "/dev/nvme0n1")
        self.assertEqual(len(clean["mounts"]), 5)

    def test_rejected_layouts(self):
        root = {"device": "/dev/sda2", "mount": "/", "format": True}
        bad = [
            {"mounts": []},  # nowhere for PolyOS
            {"wipe": {"/dev/sda": "/"}, "mounts": [{"device": "/dev/sdb1", "mount": "/", "format": True}]},  # two roots
            {"mounts": [{"device": "/dev/sda2", "mount": "/", "format": False}]},  # / must be erased
            {"mounts": [root, {"device": "/dev/sda3", "mount": "/etc", "format": True}]},  # not a place
            {"mounts": [root, {"device": "/dev/sda3", "mount": "/mnt/../../etc", "format": True}]},
            {"mounts": [root, {"device": "/dev/sda2", "mount": "/home", "format": True}]},  # same partition twice
            {"mounts": [root, {"device": "/dev/sda3", "mount": "/home", "format": True},
                        {"device": "/dev/sda4", "mount": "/home", "format": False}]},  # /home twice
            {"wipe": {"/dev/sdb": "/"}, "mounts": [{"device": "/dev/sdb2", "mount": "/home", "format": True}]},  # on an erased drive
            {"wipe": {"/dev/sdb; reboot": "/"}},
            {"wipe": {"/dev/sdb": "/boot"}},
            {"mounts": [root, {"device": "/dev/sda3", "mount": "/home", "format": "yes"}]},
        ]
        for over in bad:
            with self.subTest(over=over), self.assertRaises(InstallError):
                installer.validate_plan(self.plan(**over))

    def test_disk_of(self):
        self.assertEqual(installer.disk_of("/dev/sda12"), "/dev/sda")
        self.assertEqual(installer.disk_of("/dev/nvme1n1p3"), "/dev/nvme1n1")
        self.assertEqual(installer.disk_of("/dev/mmcblk0p2"), "/dev/mmcblk0")

    def test_fstab_for_kept_drives(self):
        text = installer.fstab_entries([
            {"uuid": "home", "mount": "/home", "fs": "ext4"},
            {"uuid": "root", "mount": "/", "fs": "ext4"},
            {"uuid": "win", "mount": "/mnt/windows", "fs": "ntfs"},
            {"uuid": "sw", "mount": "swap", "fs": "swap"},
        ], uid=1001)
        lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
        self.assertTrue(lines[0].startswith("UUID=root  /  ext4"))  # / before everything
        self.assertIn("UUID=home  /home  ext4  defaults  0  2", text)
        win = next(ln for ln in lines if "/mnt/windows" in ln)
        self.assertIn("ntfs3", win)
        self.assertIn("uid=1001", win)
        self.assertIn("nofail", win)  # an unplugged drive never blocks starting up
        self.assertIn("UUID=sw  none  swap", text)
        self.assertNotIn("/swapfile", text)

    def test_custom_dry_run(self):
        events = []
        plan = installer.validate_plan(self.plan(wipe={"/dev/sdc": "/mnt/storage"}, mounts=[
            {"device": "/dev/sda3", "mount": "/", "format": True},
            {"device": "/dev/sda1", "mount": "/boot/efi", "format": False},
            {"device": "/dev/sdb1", "mount": "/home", "format": False}]))
        esp = part("/dev/sda1", 1, 300 * MiB, "vfat", installer.ESP_GUID.lower())
        disks = [disk(parts=[esp, part("/dev/sda2", 2, 200 * GiB), part("/dev/sda3", 3, 100 * GiB, "ext4", installer.LINUX_GUID.lower())]),
                 {**disk(size=1000 * GiB, parts=[part("/dev/sdb1", 1, 900 * GiB, "ext4", installer.LINUX_GUID.lower())]), "path": "/dev/sdb"},
                 {**disk(size=2000 * GiB, table=None), "path": "/dev/sdc"}]
        with mock.patch.object(installer, "live_disk", return_value="/dev/sdz"), \
                mock.patch.object(installer, "parse_lsblk", return_value=disks), \
                mock.patch("pathlib.Path.is_dir", return_value=True):
            inst = installer.Installer(plan, events.append, dry_run=True)
            inst.run()
        commands = [e["log"].replace("\\", "/") for e in events if "log" in e]
        joined = "\n".join(commands)
        self.assertTrue(events[-1].get("done"), joined)
        self.assertTrue(inst.dual)  # Windows (sda2) stays: GRUB shows its menu
        self.assertTrue(inst.windows_alongside)
        self.assertIn("wipefs -a -f /dev/sdc", joined)
        self.assertIn("mkfs.ext4 -F -q -L PolyOS /dev/sda3", joined)
        self.assertIn("mkfs.ext4 -F -q -L storage /dev/sdc1", joined)
        for untouched in ("/dev/sda1", "/dev/sdb1"):  # kept: mounted, never formatted or wiped
            self.assertFalse(any(("mkfs" in c or "wipefs" in c) and untouched in c for c in commands), untouched)
        self.assertNotIn("wipefs -a -f /dev/sda\n", joined + "\n")
        mounts = [c for c in commands if c.startswith("$ mount /dev")]
        self.assertTrue(mounts[0].startswith("$ mount /dev/sda3 /mnt/polyos-target"), mounts)
        self.assertTrue(any("/dev/sdb1 /mnt/polyos-target/home" in c for c in mounts), mounts)

    def test_custom_refuses_before_erasing(self):
        plan = installer.validate_plan(self.plan(wipe={"/dev/sdc": "/home"}, mounts=[
            {"device": "/dev/sda2", "mount": "/", "format": True}]))  # far too small
        disks = [disk(parts=[part("/dev/sda2", 2, 8 * GiB, "ext4")]), {**disk(table=None), "path": "/dev/sdc"}]
        events = []
        with mock.patch.object(installer, "live_disk", return_value="/dev/sdz"), \
                mock.patch.object(installer, "parse_lsblk", return_value=disks), \
                self.assertRaises(InstallError):
            installer.Installer(plan, events.append, dry_run=True).run()
        self.assertFalse(any("wipefs" in e.get("log", "") for e in events))


class Arm64InstallTests(unittest.TestCase):
    def plan(self):
        return installer.validate_plan({"mode": "erase", "disk": "/dev/nvme0n1", "hostname": "t-polyos", "timezone": "UTC",
                                        "user": {"fullName": "T", "username": "tester", "password": "pw"},
                                        "appearance": {"theme": "dark", "accent": "#678fd9"}})

    def test_arm64_installs_arm_grub(self):
        events = []
        with mock.patch.object(installer, "live_disk", return_value="/dev/sdb"), \
                mock.patch.object(installer, "debian_arch", return_value="arm64"), \
                mock.patch.object(installer, "_have", return_value=True), \
                mock.patch("pathlib.Path.is_dir", return_value=True):
            installer.Installer(self.plan(), events.append, dry_run=True).run()
        joined = "\n".join(e["log"].replace("\\", "/") for e in events if "log" in e)
        self.assertIn("grub-install --target=arm64-efi", joined)
        self.assertIn("/EFI/debian/shimaa64.efi", joined)
        self.assertNotIn("x86_64", joined)

    def test_arm64_needs_uefi_before_touching_disks(self):
        events = []
        with mock.patch.object(installer, "live_disk", return_value="/dev/sdb"), \
                mock.patch.object(installer, "debian_arch", return_value="arm64"), \
                mock.patch("pathlib.Path.is_dir", return_value=False), self.assertRaises(InstallError):
            installer.Installer(self.plan(), events.append, dry_run=True).run()
        self.assertFalse(any("wipefs" in e.get("log", "") for e in events))


class DriveScreenTests(unittest.TestCase):
    """The Windows-style "Where do you want to install PolyOS?" screen."""

    def described(self, d, table, uefi=True, live=None):
        return installer.describe_disk(d, table, {}, uefi, live, {})

    def windows_disk(self):
        d = disk(parts=[part("/dev/sda1", 1, 100 * MiB, "vfat", installer.ESP_GUID.lower()),
                        part("/dev/sda2", 2, 16 * MiB, "", installer.MSR_GUID.lower()),
                        part("/dev/sda3", 3, 200 * GiB)])
        table = {"label": "gpt", "sector": 512, "first": 2048, "last": 500 * GiB // 512 - 34, "partitions": [
            {"node": "/dev/sda1", "number": 1, "start": 2048, "size": 204800, "type": "x"},
            {"node": "/dev/sda2", "number": 2, "start": 206848, "size": 32768, "type": "x"},
            {"node": "/dev/sda3", "number": 3, "start": 239616, "size": 200 * GiB // 512, "type": "x"}]}
        return d, table

    def test_partitions_and_spaces(self):
        d, table = self.windows_disk()
        out = self.described(d, table)
        esp, msr, win = out["partitions"]
        self.assertFalse(esp["install"]["possible"])  # needed to start the computer
        self.assertFalse(msr["install"]["possible"])
        self.assertTrue(win["install"]["possible"])  # erasing Windows' partition is allowed
        self.assertEqual([p["start"] for p in out["partitions"]], [2048, 206848, 239616])
        (space,) = out["free"]
        self.assertTrue(space["install"]["possible"])
        self.assertFalse(space["install"]["newEsp"])  # Windows' EFI partition is shared
        self.assertEqual(space["install"]["usable"], space["bytes"])

    def test_too_small_and_live(self):
        d = disk(parts=[part("/dev/sda1", 1, 10 * GiB, "ext4", installer.LINUX_GUID.lower())])
        table = {"label": "gpt", "sector": 512, "first": 2048, "last": 12 * GiB // 512, "partitions": [
            {"node": "/dev/sda1", "number": 1, "start": 2048, "size": 10 * GiB // 512, "type": "x"}]}
        out = self.described(d, table)
        self.assertIn("too small", out["partitions"][0]["install"]["reason"])
        self.assertIn("too small", out["free"][0]["install"]["reason"])
        live = self.described(disk(), None, live="/dev/sda")
        self.assertIn("running from", live["free"][0]["install"]["reason"])

    def test_empty_drive_is_one_space(self):
        out = self.described(disk(size=1000 * GiB, table=None), None)
        self.assertEqual(out["free"], [{"start": 0, "bytes": 1000 * GiB, "install": {"possible": True, "usable": 1000 * GiB - installer.ESP_BYTES, "newEsp": True}}])

    def test_uefi_needs_an_esp_somewhere(self):
        d = disk(parts=[part("/dev/sda1", 1, 100 * GiB, "ext4", installer.LINUX_GUID.lower())])
        table = {"label": "gpt", "sector": 512, "first": 2048, "last": 500 * GiB // 512 - 34, "partitions": [
            {"node": "/dev/sda1", "number": 1, "start": 2048, "size": 100 * GiB // 512, "type": "x"}]}
        out = installer.finish_install_options([self.described(d, table)], uefi=True)
        self.assertIn("EFI system partition", out[0]["partitions"][0]["install"]["reason"])
        self.assertTrue(out[0]["free"][0]["install"]["newEsp"])  # unallocated space still works: PolyOS adds one
        bios = installer.finish_install_options([self.described(d, table, uefi=False)], uefi=False)
        self.assertTrue(bios[0]["partitions"][0]["install"]["possible"])

    def test_mbr_limits(self):
        full = disk(table="dos", parts=[part(f"/dev/sda{i}", i, 50 * GiB, "ext4", "83") for i in range(1, 5)])
        self.assertIn("four partitions", installer.new_partition_problem(full, uefi=False))
        legacy = disk(table="dos", parts=[part("/dev/sda1", 1, 50 * GiB, "ntfs", "7")])
        self.assertIn("MBR", installer.new_partition_problem(legacy, uefi=True))
        self.assertIsNone(installer.new_partition_problem(disk(table="dos"), uefi=True))  # no partitions left: set up fresh

    def test_space_plan_and_dry_run(self):
        base = {"disk": "/dev/sda", "hostname": "t-polyos", "timezone": "UTC",
                "user": {"fullName": "T", "username": "tester", "password": "pw"}}
        with self.assertRaises(InstallError):
            installer.validate_plan({**base, "mode": "space"})
        with self.assertRaises(InstallError):
            installer.validate_plan({**base, "mode": "space", "start": True})
        plan = installer.validate_plan({**base, "mode": "space", "start": 2048})
        self.assertEqual((plan["mode"], plan["start"]), ("space", 2048))
        events = []
        with mock.patch.object(installer, "live_disk", return_value="/dev/sdb"), \
                mock.patch("pathlib.Path.is_dir", return_value=True):
            installer.Installer(plan, events.append, dry_run=True).run()
        commands = "\n".join(e["log"] for e in events if "log" in e)
        self.assertTrue(events[-1].get("done"))
        self.assertIn("sfdisk --append", commands)
        self.assertNotIn("wipefs -a -f /dev/sda\n", commands + "\n")  # the rest of the drive is left alone


class FakeDrive:
    """Stands in for lsblk/sfdisk on one GPT drive (/dev/sda), recording every command."""

    def __init__(self, parts, size=500 * GiB, label="gpt"):
        self.size, self.label, self.parts, self.commands = size, label, list(parts), []

    def run(self, args, *, input=None, **_kw):
        self.commands.append((args, input))
        if args[0] == "lsblk":
            children = [{"name": f"/dev/sda{p['number']}", "type": "part", "size": p["size"] * 512, "fstype": p.get("fs"),
                         "partn": p["number"], "parttype": p["type"].lower(), "mountpoints": p.get("mounts", [None])}
                        for p in self.parts]
            return json.dumps({"blockdevices": [{"name": "/dev/sda", "type": "disk", "size": self.size, "rm": False, "ro": False,
                                                 "pttype": self.label, "children": children}]})
        if args[:2] == ["sfdisk", "-J"]:
            return json.dumps({"partitiontable": {"label": self.label or "gpt", "sectorsize": 512, "firstlba": 34,
                                                  "lastlba": self.size // 512 - 34, "partitions": [
                                                      {"node": f"/dev/sda{p['number']}", "start": p["start"], "size": p["size"], "type": p["type"]}
                                                      for p in self.parts]}})
        if args[:2] == ["sfdisk", "--append"]:
            spec = dict(item.strip().split("=") for item in input.strip().split(","))
            n = max([p["number"] for p in self.parts] + [0]) + 1
            self.parts.append({"number": n, "start": int(spec["start"]), "size": int(spec["size"]), "type": spec["type"]})
        if args[:2] == ["sfdisk", "--delete"]:
            self.parts = [p for p in self.parts if p["number"] != int(args[3])]
        if args[0] == "sfdisk" and input and input.startswith("label:"):
            self.label = input.split(":")[1].strip()
        return ""


class DriveChangeTests(unittest.TestCase):
    def patched(self, drive, uefi=True):
        return [mock.patch.object(installer.Runner, "run", drive.run), mock.patch.object(installer, "live_disk", return_value="/dev/sdb"),
                mock.patch("pathlib.Path.is_dir", return_value=uefi), mock.patch.object(installer.Runner, "__init__", lambda s, e, d=False: None)]

    def apply(self, patches, fn, *args):
        for p in patches:
            p.start()
        try:
            return fn(*args)
        finally:
            for p in reversed(patches):
                p.stop()

    def test_delete(self):
        drive = FakeDrive([{"number": 1, "start": 2048, "size": 204800, "type": installer.ESP_GUID},
                           {"number": 3, "start": 239616, "size": 200 * GiB // 512, "type": installer.MS_BASIC_GUID, "mounts": ["/media/win"]}])
        self.apply(self.patched(drive), installer.delete_partition, "/dev/sda", 3)
        run = [c[0] for c in drive.commands]
        self.assertIn(["umount", "/media/win"], run)  # let go of it first
        self.assertIn(["wipefs", "-a", "-f", "/dev/sda3"], run)
        self.assertIn(["sfdisk", "--delete", "/dev/sda", "3"], run)
        self.assertEqual([p["number"] for p in drive.parts], [1])
        with self.assertRaises(InstallError):
            self.apply(self.patched(drive), installer.delete_partition, "/dev/sda", 3)  # gone already
        with self.assertRaises(InstallError):
            self.apply(self.patched(drive), installer.delete_partition, "/dev/sdb", 1)  # the USB drive PolyOS runs from

    def test_new_next_to_windows(self):
        drive = FakeDrive([{"number": 1, "start": 2048, "size": 204800, "type": installer.ESP_GUID}])
        self.apply(self.patched(drive), installer.create_partition, "/dev/sda", 206848, 100 * GiB)
        new = drive.parts[-1]
        self.assertEqual((new["start"], new["size"], new["type"]), (206848, 100 * GiB // 512, installer.LINUX_GUID))
        self.assertFalse(any(c[0][0] == "mkfs.vfat" for c in drive.commands))  # the existing EFI partition is enough

    def test_new_on_empty_drive_adds_an_esp(self):
        drive = FakeDrive([], label=None)
        self.apply(self.patched(drive), installer.create_partition, "/dev/sda", 0, 100 * GiB)
        self.assertEqual(drive.label, "gpt")
        esp, root = drive.parts
        self.assertEqual(esp["type"], installer.ESP_GUID)
        self.assertEqual(esp["size"] * 512, installer.ESP_BYTES)
        self.assertEqual(root["start"], esp["start"] + esp["size"])
        self.assertIn(["mkfs.vfat", "-F", "32", "-n", "EFI", "/dev/sda1"], [c[0] for c in drive.commands])


class DualBootTests(unittest.TestCase):
    """Windows with a C: and a D: drive, with and without BitLocker."""

    def laptop(self, c_fs="ntfs"):
        d = disk(parts=[part("/dev/sda1", 1, 100 * MiB, "vfat", installer.ESP_GUID.lower()),
                        part("/dev/sda2", 2, 16 * MiB, "", installer.MSR_GUID.lower()),
                        part("/dev/sda3", 3, 250 * GiB, c_fs),
                        part("/dev/sda4", 4, 200 * GiB)])
        d["partitions"][2]["label"] = "" if c_fs == "BitLocker" else "Windows"
        d["partitions"][3]["label"] = "New Volume"
        table = {"label": "gpt", "sector": 512, "first": 2048, "last": 500 * GiB // 512 - 34, "partitions": [
            {"node": "/dev/sda1", "number": 1, "start": 2048, "size": 204800, "type": "x"},
            {"node": "/dev/sda2", "number": 2, "start": 206848, "size": 32768, "type": "x"},
            {"node": "/dev/sda3", "number": 3, "start": 239616, "size": 250 * GiB // 512, "type": "x"},
            {"node": "/dev/sda4", "number": 4, "start": 239616 + 250 * GiB // 512, "size": 200 * GiB // 512, "type": "x"}]}
        resize = {"/dev/sda3": {"fs": "ntfs", "min": 80 * GiB, "used": 80 * GiB, "reason": None}} if c_fs == "ntfs" else {}
        return [installer.describe_disk(d, table, {}, True, None, resize)]

    def test_d_drive_is_offered_and_not_called_windows(self):
        (out,) = installer.finish_install_options(self.laptop(), True)
        c, d = out["partitions"][2], out["partitions"][3]
        self.assertEqual((c["os"], d["os"]), ("Windows", None))
        self.assertTrue(d["install"]["possible"])
        self.assertTrue(out["alongside"]["possible"])
        self.assertEqual(installer.bitlocker_volumes([out]), [])

    def test_bitlocker_blocks_dual_boot(self):
        (out,) = installer.finish_install_options(self.laptop("BitLocker"), True)
        self.assertEqual([v["path"] for v in installer.bitlocker_volumes([out])], ["/dev/sda3"])
        self.assertFalse(out["alongside"]["possible"])
        self.assertTrue(out["alongside"]["bitlocker"])
        c, d = out["partitions"][2], out["partitions"][3]
        self.assertFalse(d["install"]["possible"])  # Windows would stay, encrypted
        self.assertIn("BitLocker", d["install"]["reason"])
        self.assertTrue(c["install"]["possible"])  # erasing the encrypted partition itself is fine
        self.assertTrue(all(not r["install"]["possible"] for r in out["free"]))


class WindowsAsleepTests(unittest.TestCase):
    """Windows with Fast Startup is only asleep: PolyOS mustn't change its drive (Automatic Repair loop)."""

    def test_asleep_windows_blocks_changes_next_to_it(self):
        disk = {"path": "/dev/sda", "model": "SSD", "size": 500 * installer.GiB, "readonly": False, "isLive": False,
                "table": "gpt", "partitions": [
                    {"path": "/dev/sda3", "size": 200 * installer.GiB, "fstype": "ntfs", "ntfsState": "hibernated",
                     "install": {"possible": True}, "label": "Windows"},
                    {"path": "/dev/sda4", "size": 200 * installer.GiB, "fstype": "ntfs", "ntfsState": None,
                     "install": {"possible": True}, "label": "Data", "esp": False}],
                "free": [{"start": 1, "bytes": 90 * installer.GiB, "install": {"possible": True}}],
                "alongside": {"possible": True}}
        other = {"path": "/dev/sdb", "model": "USB", "size": 64 * installer.GiB, "readonly": False, "isLive": False,
                 "table": "gpt", "partitions": [{"path": "/dev/sdb1", "size": 60 * installer.GiB, "fstype": "ext4",
                                                 "install": {"possible": True}}], "free": [], "alongside": {"possible": False}}
        out = installer.finish_install_options([disk, other], uefi=False)
        self.assertEqual([v["path"] for v in installer.windows_asleep(out)], ["/dev/sda3"])
        self.assertFalse(out[0]["alongside"]["possible"])
        self.assertFalse(out[0]["free"][0]["install"]["possible"])
        self.assertFalse(out[0]["partitions"][1]["install"]["possible"])  # D: too: Windows had it open
        self.assertIn("Fast Startup", out[0]["partitions"][1]["install"]["reason"])
        self.assertTrue(out[1]["partitions"][0]["install"]["possible"])  # a drive without Windows is fine

    def test_boot_menu_waits_for_no_one(self):
        from pathlib import Path as P
        text = (P(__file__).resolve().parent.parent / "data/grub-defaults/50-polyos-menu.cfg").read_text()
        self.assertIn("GRUB_TIMEOUT_STYLE=hidden", text)
        self.assertIn("GRUB_DEFAULT=saved", text)
