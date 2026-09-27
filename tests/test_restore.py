"""Set up like one of your computers: backups in the Poly Account, applied while installing."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from polyos import firststart, installer, polyaccount
from polyos.core import DEFAULTS


class BackupTests(unittest.TestCase):
    def test_make_backup(self):
        settings = {**DEFAULTS, "edition": "gaming", "taskbarAlign": "left", "wallpaper": "/home/sam/Pictures/me.jpg",
                    "effects": "off", "performanceProfile": "light"}
        b = polyaccount.make_backup(settings, ["steam", "discord", "steam", "Not An Id"])
        self.assertEqual(b["edition"], "gaming")
        self.assertEqual(b["apps"], ["discord", "steam"])
        self.assertEqual(b["settings"]["taskbarAlign"], "left")
        self.assertNotIn("wallpaper", b["settings"])  # a picture from this disk wouldn't exist on the new one
        self.assertNotIn("effects", b["settings"])  # belongs to this computer's hardware
        self.assertNotIn("performanceProfile", b["settings"])
        self.assertNotIn("setupDone", b["settings"])

    def test_clean_backup(self):
        dirty = {"edition": "root", "settings": {"theme": "purple", "accent": "#E07F9B", "pinned": ["steam.desktop"],
                                                 "setupDone": False, "developerMode": True, "screenOff": "soon"},
                 "apps": ["steam", "../../etc", "x" * 80, 5]}
        clean = polyaccount.clean_backup(dirty)
        self.assertEqual(clean["edition"], "regular")
        self.assertEqual(clean["apps"], ["steam"])
        self.assertEqual(clean["settings"], {"accent": "#e07f9b", "pinned": ["steam.desktop"]})
        self.assertIsNone(polyaccount.clean_backup("nope"))

    def test_backup_due(self):
        b = polyaccount.make_backup(DEFAULTS, [])
        state = {"credential": "pd_x", "sync": True}
        self.assertTrue(polyaccount.backup_due(state, b, 1000))  # never sent
        with mock.patch.object(polyaccount, "http"), mock.patch.object(polyaccount, "merge"):
            polyaccount.push_backup(state, b, 1000)
        self.assertFalse(polyaccount.backup_due(state, b, 1000 + 10 * 3600))  # unchanged
        changed = polyaccount.make_backup({**DEFAULTS, "theme": "light"}, [])
        self.assertFalse(polyaccount.backup_due(state, changed, 1000 + 60))  # changed, but just sent
        self.assertTrue(polyaccount.backup_due(state, changed, 1000 + polyaccount.BACKUP_EVERY))
        self.assertFalse(polyaccount.backup_due({"credential": "pd_x", "sync": False}, b, 0))  # sync off: no backups


class RestorePlanTests(unittest.TestCase):
    def plan(self, **over):
        base = {"mode": "erase", "disk": "/dev/sda", "hostname": "t-polyos", "timezone": "UTC", "edition": "gaming",
                "user": {"fullName": "T", "username": "tester", "password": "pw"},
                "appearance": {"theme": "light", "accent": "#112233"}}
        return {**base, **over}

    def test_restore_copies_settings_and_apps(self):
        clean = installer.validate_plan(self.plan(restore={
            "name": "Gaming PC", "edition": "gaming", "apps": ["steam", "discord", "bad id"],
            "settings": {"theme": "dark", "accent": "#e07f9b", "taskbarAlign": "left", "pinned": ["steam.desktop"],
                         "setupDone": False, "effects": "off"}}))
        extra = clean["extraSettings"]
        self.assertEqual(extra["taskbarAlign"], "left")
        self.assertEqual(extra["pinned"], ["steam.desktop"])
        self.assertNotIn("effects", extra)
        self.assertNotIn("theme", extra)  # the Personalization screen's choice wins
        self.assertEqual(clean["appearance"], {"theme": "light", "accent": "#112233"})
        self.assertEqual(clean["firstStart"]["apps"], ["steam", "discord"])
        self.assertEqual(clean["firstStart"]["pack"], "gaming")

    def test_no_restore(self):
        clean = installer.validate_plan(self.plan())
        self.assertEqual(clean["firstStart"]["apps"], [])
        self.assertNotIn("taskbarAlign", clean["extraSettings"])


class FirstStartAppsTests(unittest.TestCase):
    def test_apps_install_after_restart(self):
        tmp = Path(tempfile.mkdtemp())
        plan_path, status_path = tmp / "plan.json", tmp / "status.json"
        plan_path.write_text(json.dumps({"drivers": [], "pack": None, "apps": ["steam", "discord"]}))
        installed = []
        status = firststart.run(lambda e: None, lambda names: None, lambda name: None, lambda: True,
                                plan_path=plan_path, status_path=status_path, clock=lambda: 5,
                                install_apps=installed.extend)
        self.assertEqual(installed, ["steam", "discord"])
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["appsDone"], 2)
        self.assertFalse(plan_path.exists())

    def test_apps_wait_for_the_internet(self):
        tmp = Path(tempfile.mkdtemp())
        plan_path = tmp / "plan.json"
        plan_path.write_text(json.dumps({"apps": ["steam"]}))
        status = firststart.run(lambda e: None, lambda n: None, lambda n: None, lambda: False,
                                plan_path=plan_path, status_path=tmp / "s.json", install_apps=lambda ids: None)
        self.assertEqual(status["state"], "waiting")
        self.assertIn("apps", status["what"])
        self.assertTrue(plan_path.exists())


class AdminAppsInstallTests(unittest.TestCase):
    def test_only_catalog_apps_for_this_processor(self):
        from polyos import admin, store
        catalog = {"apps": [{"id": "steam", "name": "Steam", "source": "debian", "packages": ["steam-installer"], "arches": ["amd64"]},
                            {"id": "obs", "name": "OBS", "source": "flathub", "ref": "com.obsproject.Studio"},
                            {"id": "chrome", "name": "Chrome", "source": "debian", "packages": ["google-chrome-stable"], "system": True}]}
        with mock.patch.object(store, "load", return_value=catalog), \
                mock.patch.object(store, "validate", return_value={a["id"]: a for a in catalog["apps"]}), \
                mock.patch.object(store, "debian_arch", return_value="arm64"), \
                mock.patch.object(admin, "install_catalog_apps") as install:
            admin.apps_install(["steam", "obs", "chrome", "gone-from-catalog"])
        chosen = install.call_args[0][0]
        self.assertEqual([a["id"] for a in chosen], ["obs"])  # Steam isn't for ARM; Chrome is part of PolyOS


if __name__ == "__main__":
    unittest.main()
