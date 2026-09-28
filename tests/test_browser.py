"""The web browser you choose in setup: Google Chrome (Chromium on ARM) or Firefox."""

import tempfile
import unittest
from pathlib import Path

from polyos import installer, system
from polyos.backend import supersede_browsers
from polyos.core import DEFAULTS, browser_pins


class BrowserPinsTests(unittest.TestCase):
    def test_firefox_takes_chromes_place(self):
        patch = browser_pins(DEFAULTS, "firefox")
        self.assertEqual(patch["pinned"][:2], ["firefox-esr.desktop", "polyos-files.desktop"])
        self.assertNotIn("google-chrome.desktop", patch["startPinned"])
        self.assertEqual(patch["desktopIcons"][1], "firefox-esr.desktop")

    def test_and_back(self):
        back = browser_pins({**DEFAULTS, **browser_pins(DEFAULTS, "firefox")}, "chrome")
        self.assertEqual(back["pinned"], DEFAULTS["pinned"])
        self.assertEqual(back["startPinned"], DEFAULTS["startPinned"])

    def test_no_browser_pinned_goes_first(self):
        self.assertEqual(browser_pins({"pinned": ["a.desktop"]}, "firefox")["pinned"], ["firefox-esr.desktop", "a.desktop"])


class SupersedeTests(unittest.TestCase):
    base = {"google-chrome.desktop", "chromium.desktop"}

    def apps(self, *ids):
        return [{"id": i, "hidden": False} for i in ids]

    def shown(self, apps):
        return [a["id"] for a in apps if not a.get("superseded")]

    def test_chrome(self):
        apps = self.apps("google-chrome.desktop", "chromium.desktop", "firefox-esr.desktop")
        supersede_browsers(apps, self.base)
        self.assertEqual(self.shown(apps), ["google-chrome.desktop", "firefox-esr.desktop"])

    def test_firefox_once_installed(self):
        apps = self.apps("google-chrome.desktop", "chromium.desktop", "firefox-esr.desktop")
        supersede_browsers(apps, self.base, "firefox")
        self.assertEqual(self.shown(apps), ["firefox-esr.desktop"])

    def test_chrome_until_firefox_is_installed(self):
        apps = self.apps("google-chrome.desktop", "chromium.desktop")
        supersede_browsers(apps, self.base, "firefox")
        self.assertEqual(self.shown(apps), ["google-chrome.desktop"])


class DefaultBrowserTests(unittest.TestCase):
    def test_keeps_the_rest_of_the_file(self):
        home = Path(tempfile.mkdtemp())
        path = home / ".config/mimeapps.list"
        path.parent.mkdir(parents=True)
        path.write_text("[Added Associations]\nimage/png=a.desktop;\n\n[Default Applications]\ntext/plain=b.desktop;\n"
                        "text/html=old.desktop;\n")
        system.set_default_browser("firefox", home)
        text = path.read_text()
        self.assertIn("image/png=a.desktop;", text)
        self.assertIn("text/plain=b.desktop;", text)
        self.assertNotIn("old.desktop", text)
        self.assertIn("x-scheme-handler/https=firefox-esr.desktop;google-chrome.desktop;chromium.desktop;", text)

    def test_new_file(self):
        home = Path(tempfile.mkdtemp())
        system.set_default_browser("chrome", home)
        self.assertTrue((home / ".config/mimeapps.list").read_text().startswith("[Default Applications]\ntext/html=google-chrome"))


class InstallPlanTests(unittest.TestCase):
    def plan(self, **over):
        return {"mode": "erase", "disk": "/dev/sda", "hostname": "t-polyos", "timezone": "UTC", "edition": "regular",
                "user": {"fullName": "T", "username": "tester", "password": "pw"}, **over}

    def test_chrome_is_the_default(self):
        clean = installer.validate_plan(self.plan())
        self.assertEqual(clean["extraSettings"]["browser"], "chrome")
        self.assertNotIn("pinned", clean["extraSettings"])
        self.assertEqual(clean["firstStart"]["apps"], [])

    def test_firefox_installs_after_the_restart(self):
        clean = installer.validate_plan(self.plan(browser="firefox"))
        self.assertEqual(clean["extraSettings"]["browser"], "firefox")
        self.assertEqual(clean["extraSettings"]["pinned"][0], "firefox-esr.desktop")
        self.assertEqual(clean["firstStart"]["apps"], ["firefox"])

    def test_unknown_browser(self):
        self.assertEqual(installer.validate_plan(self.plan(browser="netscape"))["extraSettings"]["browser"], "chrome")


if __name__ == "__main__":
    unittest.main()
