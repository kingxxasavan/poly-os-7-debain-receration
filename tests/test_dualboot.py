"""PolyOS next to Windows: the boot menu, and starting Windows the way BitLocker expects."""

import subprocess
import unittest
from pathlib import Path

from polyos import admin, installer

ROOT = Path(__file__).resolve().parent.parent
EFIBOOTMGR = ("BootCurrent: 0001\nBootNext: 0003\nBootOrder: 0001,0000\n"
              "Boot0000* Windows Boot Manager\tHD(1,GPT,a,0x800,0x32000)/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)\n"
              "Boot0001* PolyOS\tHD(1,GPT,a,0x800,0x32000)/File(\\EFI\\debian\\shimx64.efi)\n")


class BootMenuTests(unittest.TestCase):
    def test_windows_boot_entry(self):
        self.assertEqual(admin.windows_boot_entry(EFIBOOTMGR), "0000")
        self.assertIsNone(admin.windows_boot_entry("Boot0001* PolyOS\tHD(1,GPT,a)/File(x)\n"))

    def test_menu_lines(self):
        text = '# PolyOS\nGRUB_DEFAULT=saved\nGRUB_TIMEOUT=2\nGRUB_TIMEOUT_STYLE=hidden\nGRUB_DISTRIBUTOR="PolyOS"\n'
        shown = installer.grub_menu_lines(text, True)
        self.assertTrue(installer.boot_menu_shown(shown))
        self.assertIn("GRUB_TIMEOUT=10\n", shown)
        self.assertEqual(shown.count("GRUB_TIMEOUT="), 1)
        self.assertIn('GRUB_DISTRIBUTOR="PolyOS"', shown)
        hidden = installer.grub_menu_lines(shown, False)
        self.assertFalse(installer.boot_menu_shown(hidden))
        self.assertIn("GRUB_TIMEOUT_STYLE=hidden\n", hidden)

    def dropin(self, before: str) -> str:
        script = f'{before}\n. "{ROOT / "data/grub-defaults/50-polyos-menu.cfg"}"\necho "$GRUB_TIMEOUT_STYLE $GRUB_TIMEOUT $GRUB_DEFAULT"'
        return subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=True).stdout.strip()

    def test_dropin_keeps_a_shown_menu(self):
        self.assertEqual(self.dropin("GRUB_TIMEOUT_STYLE=menu; GRUB_TIMEOUT=10"), "menu 10 saved")

    def test_dropin_hides_it_otherwise(self):
        self.assertEqual(self.dropin("GRUB_TIMEOUT=5"), "hidden 2 saved")  # Debian's own /etc/default/grub


if __name__ == "__main__":
    unittest.main()
