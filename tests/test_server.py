import http.client
import json
import tempfile
import unittest
from pathlib import Path

from polyos import paths
from polyos.core import EventBus, Settings
from polyos.mock import MockBackend
from polyos.server import GREETER_API, Server


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        backend = MockBackend(Settings(Path(cls.tmp.name) / "s.json"), EventBus(), home=Path(cls.tmp.name) / "home")
        cls.server = Server(backend, paths.UI_DIR, "secret-token", dev=False, ctl_token="ctl-token")
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, token="secret-token", host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.server.port}"}
        if token:
            headers["X-PolyOS-Token"] = token
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        res = conn.getresponse()
        payload = res.read()
        conn.close()
        return res.status, payload, res

    def test_requires_token(self):
        self.assertEqual(self.request("GET", "/api/state", token=None)[0], 401)
        self.assertEqual(self.request("GET", "/api/state", token="wrong")[0], 401)
        self.assertEqual(self.request("GET", f"/api/state?t=secret-token", token=None)[0], 200)

    def test_rejects_foreign_host(self):
        self.assertEqual(self.request("GET", "/api/state", host="evil.example:80")[0], 403)

    def test_state_shape(self):
        status, body, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body)
        for key in ("version", "user", "settings", "apps", "windows", "system", "env"):
            self.assertIn(key, state)

    def test_static_files_and_traversal(self):
        status, body, res = self.request("GET", "/index.html", token=None)
        self.assertEqual(status, 200)
        self.assertIn("Content-Security-Policy", res.headers)
        self.assertNotIn(b"secret-token", body)  # the token is never served in production mode
        self.assertEqual(self.request("GET", "/../polyos/server.py", token=None)[0], 404)
        self.assertEqual(self.request("GET", "/%2e%2e/main.py", token=None)[0], 404)
        self.assertEqual(self.request("GET", "/dev.html", token=None)[0], 404)  # dev harness is dev-only

    def test_post_validation_and_actions(self):
        self.assertEqual(self.request("POST", "/api/launch", {})[0], 400)
        self.assertEqual(self.request("POST", "/api/power", {"action": "format-disk"})[0], 400)
        status, body, _ = self.request("POST", "/api/launch", {"id": "google-chrome.desktop"})
        self.assertEqual(status, 200)
        windows = json.loads(self.request("GET", "/api/state")[1])["windows"]
        self.assertTrue(any(w["appId"] == "google-chrome.desktop" for w in windows))
        status, body, _ = self.request("POST", "/api/settings", {"accent": "#22c55e"})
        self.assertEqual(json.loads(body)["accent"], "#22c55e")

    def test_run_command_and_new_popups(self):
        self.assertEqual(self.request("POST", "/api/run-command", {})[0], 400)
        status, body, _ = self.request("POST", "/api/run-command", {"command": "nosuchthing"})
        self.assertEqual(status, 500)
        self.assertIn("nosuchthing", json.loads(body)["error"])
        self.assertEqual(self.request("POST", "/api/run-command", {"command": "mousepad"})[0], 200)
        for view in ("start", "run", "power"):
            status, body, _ = self.request("POST", "/api/popup", {"view": view})
            self.assertEqual(status, 200, view)
            self.assertEqual(json.loads(body)["fullscreen"], view == "power")
        self.request("POST", "/api/popup", {"view": None})
        # the Windows key closes whatever menu is open, and opens the Home Menu otherwise
        self.request("POST", "/api/popup", {"view": "launcher"})
        self.assertIsNone(json.loads(self.request("POST", "/api/popup", {"view": "start", "toggle": True})[1] or b"null").get("view"))
        self.assertEqual(json.loads(self.request("POST", "/api/popup", {"view": "start", "toggle": True})[1])["view"], "start")
        self.request("POST", "/api/popup", {"view": None})

    def test_files_api(self):
        home = json.loads(self.request("GET", "/api/files/places")[1])["home"]
        listing = json.loads(self.request("GET", "/api/files/list?path=" + home.replace(" ", "%20"))[1])
        self.assertIn("Documents", [e["name"] for e in listing["entries"]])
        status, body, _ = self.request("POST", "/api/files/mkdir", {"parent": home, "name": "API test"})
        self.assertEqual(status, 200, body)
        created = json.loads(body)["path"]
        self.assertEqual(self.request("POST", "/api/files/trash", {"paths": [created]})[0], 200)
        trash = json.loads(self.request("GET", "/api/files/list?path=trash:///")[1])
        self.assertTrue(any(e["origin"] == created for e in trash["entries"]))
        self.assertEqual(self.request("POST", "/api/files/trash", {"paths": []})[0], 400)
        self.assertEqual(self.request("GET", "/files/raw?path=" + home, token=None)[0], 401)
        self.assertEqual(self.request("GET", "/files/raw?path=" + home + "/.bashrc")[0], 404)  # not an image

    def test_store_drivers_procs_and_admin(self):
        catalog = json.loads(self.request("GET", "/api/store")[1])
        self.assertTrue(any(a["id"] == "chrome" and a["installed"] for a in catalog["apps"]))
        self.assertEqual(self.request("POST", "/api/store/install", {"id": "nope"})[0], 404)
        self.assertEqual(self.request("POST", "/api/store/install", {"id": "vlc"})[0], 401)  # needs the password
        self.assertEqual(self.request("POST", "/api/admin/auth", {"password": "wrong"})[0], 403)
        self.assertEqual(self.request("POST", "/api/admin/auth", {"password": "polyos"})[0], 200)
        status, body, _ = self.request("POST", "/api/store/install", {"id": "vlc"})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["state"], "running")
        queued = json.loads(self.request("POST", "/api/store/install", {"id": "gimp"})[1])  # waits its turn
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(json.loads(self.request("POST", "/api/jobs/cancel", {"id": queued["id"]})[1])["state"], "cancelled")
        self.assertEqual(self.request("POST", "/api/jobs/cancel", {"id": queued["id"]})[0], 409)
        self.assertEqual(self.request("POST", "/api/store/remove", {"id": "chrome"})[0], 400)
        drivers = json.loads(self.request("GET", "/api/drivers")[1])
        self.assertTrue(any("nvidia-driver" in d["packages"] for d in drivers["devices"]))
        self.assertEqual(self.request("POST", "/api/drivers/install", {"packages": ["openssh-server"]})[0], 400)
        procs = json.loads(self.request("GET", "/api/procs")[1])
        self.assertIn("cpu", procs["perf"])
        self.assertEqual(self.request("POST", "/api/procs/end", {"pid": 1201})[0], 403)  # part of PolyOS
        self.assertEqual(self.request("GET", "/api/install/probe")[0], 409)  # not the live USB
        self.assertEqual(self.request("GET", "/icon/theme/vlc,video")[0], 200)

    def test_lock_screen(self):
        self.assertEqual(self.request("POST", "/api/power", {"action": "lock"})[0], 200)
        self.assertEqual(self.request("POST", "/api/lock/unlock", {"password": "nope"})[0], 403)
        self.assertEqual(self.request("POST", "/api/lock/unlock", {"password": "polyos"})[0], 200)
        self.assertEqual(self.request("POST", "/api/lock/recover", {"key": "bad", "password": "x"})[0], 403)
        self.assertEqual(self.request("POST", "/api/widgets/data", {"notes": "hello"})[0], 200)
        self.assertEqual(json.loads(self.request("GET", "/api/widgets/data")[1])["notes"], "hello")

    def test_icons_and_wallpaper(self):
        status, body, res = self.request("GET", "/icon/app/google-chrome.desktop")
        self.assertEqual(status, 200)
        self.assertEqual(res.headers["Content-Type"], "image/svg+xml")
        status, _, res = self.request("GET", "/wallpaper/current")
        self.assertEqual(status, 200)
        self.assertTrue(res.headers["Content-Type"].startswith("image/"))
        self.assertEqual(self.request("GET", "/wallpaper/builtin/..%2F..%2Fmain.py")[0], 404)
        status, _, res = self.request("GET", "/wallpaper/lock")
        self.assertEqual(status, 200)
        self.assertTrue(res.headers["Content-Type"].startswith("image/"))

    def raw_post(self, path, data, ctype):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request("POST", path, body=data, headers={"Host": f"127.0.0.1:{self.server.port}",
                                                       "X-PolyOS-Token": "secret-token", "Content-Type": ctype})
        res = conn.getresponse()
        payload = res.read()
        conn.close()
        return res.status, payload

    def test_camera_saves_to_pictures(self):
        info = json.loads(self.request("GET", "/api/camera")[1])
        self.assertTrue(info["camera"])
        jpeg = b"\xff\xd8\xff\xe0" + b"\0" * 200_000  # bigger than the JSON API's 64 KB limit
        status, body = self.raw_post("/api/camera/save?kind=photo", jpeg, "image/jpeg")
        self.assertEqual(status, 200, body)
        saved = Path(json.loads(body)["path"])
        self.assertEqual(saved.parent.name, "Camera")
        self.assertEqual(saved.read_bytes(), jpeg)
        self.assertEqual(self.raw_post("/api/camera/save?kind=photo", b"<html>", "image/jpeg")[0], 415)
        self.assertEqual(self.raw_post("/api/camera/save?kind=photo", jpeg, "text/html")[0], 415)
        self.assertEqual(self.raw_post("/api/camera/save?kind=exe", jpeg, "image/jpeg")[0], 400)
        status, body = self.raw_post("/api/camera/save?kind=video", b"\x1aE\xdf\xa3webm", "video/webm;codecs=vp8")
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["name"].endswith(".webm"))
        self.request("POST", "/api/settings", {"cameraAccess": False})
        try:
            self.assertEqual(self.raw_post("/api/camera/save?kind=photo", jpeg, "image/jpeg")[0], 403)
        finally:
            self.request("POST", "/api/settings", {"cameraAccess": True})

    def test_editions_cloud_security(self):
        packs = json.loads(self.request("GET", "/api/packs")[1])["packs"]
        self.assertIn("steam", [a["id"] for a in packs["gaming"]["apps"]])
        self.assertEqual(self.request("POST", "/api/packs/install", {"pack": "gaming", "apps": ["vscode"]})[0], 400)
        status, body, _ = self.request("POST", "/api/gaming/cloud", {"services": ["geforcenow"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["installed"], ["geforcenow"])
        state = json.loads(self.request("GET", "/api/state")[1])
        self.assertIn("polyos-cloud-geforcenow.desktop", [a["id"] for a in state["apps"]])
        self.assertEqual(self.request("POST", "/api/gaming/cloud", {"services": []})[0], 200)
        self.assertEqual(self.request("POST", "/api/gaming/cloud", {"services": ["evil"]})[0], 400)
        sec = json.loads(self.request("GET", "/api/security")[1])
        self.assertIn("firewall", sec)
        self.assertEqual(self.request("POST", "/api/security", {"what": "sshd", "on": True})[0], 400)

    def test_developer_mode_overrides_ui(self):
        config = Path(self.tmp.name)  # the settings file lives here, so ui/ overrides do too
        (config / "ui" / "css").mkdir(parents=True, exist_ok=True)
        (config / "ui" / "css" / "user.css").write_text(":root { --accent: #ff00aa; }")
        (config / "ui" / "made-up.js").write_text("alert(1)")
        try:
            self.assertNotIn(b"ff00aa", self.request("GET", "/css/user.css", token=None)[1])  # off by default
            self.request("POST", "/api/settings", {"developerMode": True})
            self.assertIn(b"ff00aa", self.request("GET", "/css/user.css", token=None)[1])
            self.assertEqual(self.request("GET", "/made-up.js", token=None)[0], 404)  # only replaces real files
        finally:
            self.request("POST", "/api/settings", {"developerMode": False})

    def test_new_settings_and_power(self):
        ok = {"taskbarStyle": "full", "taskbarAlign": "left", "taskbarAutoHide": True, "powerMode": "maximum",
              "screenOff": 5, "sleepAfter": 0, "desktopIcons": ["google-chrome.desktop"], "desktopOpen": "single"}
        status, body, _ = self.request("POST", "/api/settings", ok)
        self.assertEqual(status, 200, body)
        for bad in ({"taskbarStyle": "top"}, {"powerMode": "turbo"}, {"screenOff": 7}, {"screenOff": True},
                    {"desktopIcons": ["../x"]}):
            self.assertEqual(self.request("POST", "/api/settings", bad)[0], 400, bad)
        modes = json.loads(self.request("GET", "/api/power/modes")[1])
        self.assertEqual([m["id"] for m in modes["modes"]], ["saver", "balanced", "performance", "maximum"])
        perf = json.loads(self.request("GET", "/api/performance")[1])
        self.assertIn(perf["level"], ("optimal", "busy", "high"))
        # turning activity history off forgets it
        self.request("POST", "/api/launch", {"id": "google-chrome.desktop"})
        state = json.loads(self.request("POST", "/api/settings", {"keepRecent": False})[1])
        self.assertEqual(state["recent"], [])
        self.request("POST", "/api/launch", {"id": "google-chrome.desktop"})
        self.assertEqual(json.loads(self.request("GET", "/api/state")[1])["settings"]["recent"], [])
        self.request("POST", "/api/settings", {"keepRecent": True, "taskbarStyle": "floating", "taskbarAutoHide": False})


class GreeterServerTests(unittest.TestCase):
    """The login screen's server only answers what the login UI needs."""

    def test_restricted_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = MockBackend(Settings(Path(tmp) / "s.json"), EventBus(), home=Path(tmp) / "home")
            server = Server(backend, paths.UI_DIR, "tok", allow=GREETER_API)
            server.start()
            try:
                def get(path, method="GET", body=None):
                    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
                    data = json.dumps(body).encode() if body is not None else None
                    conn.request(method, path, body=data, headers={"Host": f"127.0.0.1:{server.port}", "X-PolyOS-Token": "tok",
                                                                   "Content-Type": "application/json"})
                    status = conn.getresponse().status
                    conn.close()
                    return status
                self.assertEqual(get("/api/state"), 200)
                self.assertEqual(get("/api/greeter/state"), 200)
                self.assertEqual(get("/api/performance"), 200)
                self.assertEqual(get("/wallpaper/lock"), 200)
                self.assertEqual(get("/api/camera"), 404)
                self.assertEqual(get("/api/packs"), 404)
                self.assertEqual(get("/api/dev", "POST", {"action": "folder"}), 404)
                self.assertEqual(get("/api/files/places"), 404)
                self.assertEqual(get("/api/launch", "POST", {"id": "google-chrome.desktop"}), 404)
                self.assertEqual(get("/api/run-command", "POST", {"command": "mousepad"}), 404)
                self.assertEqual(get("/api/greeter/login", "POST", {"user": "x", "password": "wrong"}), 403)
                self.assertEqual(get("/api/greeter/recover", "POST", {"user": "x", "key": "bad", "password": "p"}), 403)
                self.assertEqual(get("/api/lock/unlock", "POST", {"password": "polyos"}), 404)  # not on the login screen
            finally:
                server.stop()


class DebTests(unittest.TestCase):
    def test_packages_are_valid_ar_archives(self):
        import io
        import lzma  # noqa: F401 - tarfile needs it for .xz
        import tarfile

        import main

        with tempfile.TemporaryDirectory() as out:
            debs = [main.build_deb(name, Path(out)) for name in main.PACKAGES]
            for deb in debs:
                raw = deb.read_bytes()
                self.assertTrue(raw.startswith(b"!<arch>\n"))
                members, pos = {}, 8
                while pos < len(raw):
                    header = raw[pos:pos + 60]
                    name = header[:16].decode().strip()
                    size = int(header[48:58].decode())
                    members[name] = raw[pos + 60:pos + 60 + size]
                    pos += 60 + size + (size % 2)
                self.assertEqual(list(members), ["debian-binary", "control.tar.xz", "data.tar.xz"])
                self.assertEqual(members["debian-binary"], b"2.0\n")
                with tarfile.open(fileobj=io.BytesIO(members["control.tar.xz"])) as tar:
                    control = tar.extractfile("./control").read().decode()
                self.assertIn(f"Version: {main.VERSION}", control)
                with tarfile.open(fileobj=io.BytesIO(members["data.tar.xz"])) as tar:
                    names = tar.getnames()
                    if "polyos-shell" in deb.name:
                        self.assertIn("./usr/bin/polyos-session", names)
                        self.assertEqual(tar.getmember("./usr/bin/polyos-session").mode, 0o755)
                        self.assertIn("./usr/share/polyos/ui/index.html", names)
                        self.assertNotIn("./usr/share/polyos/ui/dev.html", names)
                        script = tar.extractfile("./usr/bin/polyos-shell").read()
                        self.assertNotIn(b"\r\n", script)
                        self.assertTrue(all(tar.getmember(n).uid == 0 for n in names))


class BootMediaTests(unittest.TestCase):
    """The USB stick: ARM64's EFI partition check and the boot menu branding hook."""

    @staticmethod
    def mbr(*partitions: tuple[int, int, int]) -> bytes:
        raw = bytearray(512)
        for n, (kind, start, size) in enumerate(partitions):
            entry = 446 + 16 * n
            raw[entry + 4] = kind
            raw[entry + 8:entry + 12] = start.to_bytes(4, "little")
            raw[entry + 12:entry + 16] = size.to_bytes(4, "little")
        raw[510:512] = b"\x55\xaa"
        return bytes(raw)

    def test_efi_partition(self):
        import main

        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "polyos.iso"
            cases = {
                self.mbr((0x83, 0, 3401216), (0xEF, 3401216, 9984)): 2,  # the hybrid ARM64 image
                self.mbr((0x00, 0, 0), (0x83, 64, 100)): None,
                self.mbr((0xEF, 0, 0)): None,  # empty slot
                bytes(2048): None,  # a CD-only ISO: no MBR signature
                b"short": None,
            }
            for raw, expected in cases.items():
                image.write_bytes(raw)
                self.assertEqual(main.efi_partition(image), expected)

    def test_boot_menu_hook(self):
        import shutil
        import subprocess

        hook = Path(__file__).resolve().parents[1] / "iso/config/hooks/live/0600-polyos-bootmenu.hook.binary"
        if not shutil.which("sh"):
            self.skipTest("needs a POSIX shell")
        with tempfile.TemporaryDirectory() as tmp:
            iso = Path(tmp)
            grub = iso / "boot/grub"
            theme = grub / "live-theme"
            art = grub / "polyos-theme"
            for d in (theme, art, iso / "isolinux"):
                d.mkdir(parents=True)
            (grub / "grub.cfg").write_text('menuentry "Live system (arm64)" --hotkey=l {\n}\n'
                                           'menuentry "Live system (arm64 fail-safe mode)" {\n}\n')
            (grub / "config.cfg").write_text("set default=0\n")
            # live-build's theme mixes spaces and tabs
            (theme / "theme.txt").write_text('desktop-image: "../splash.png"\n+ boot_menu {\n        left = 10%\n'
                                             '        width = 80%\n        item_color = "#a8a8a8"\n'
                                             '        selected_item_color= "#ffffff"\n        item_height = 16\n'
                                             '\titem_icon_space = 0\n}\n')
            (grub / "splash.png").write_bytes(b"debian")
            for name in ("splash.png", "boot.png", "terminal_box_c.png", "select_c.png"):
                (art / name).write_bytes(name.encode())
            (iso / "isolinux/live.cfg").write_text("label live-amd64\n\tmenu label ^Live system (amd64)\n")
            (iso / "isolinux/splash800x600.png").write_bytes(b"debian")
            subprocess.run(["sh", str(hook)], cwd=iso, check=True)
            menu = (grub / "grub.cfg").read_text()
            self.assertIn('menuentry "Start PolyOS 7" --hotkey=l', menu)
            self.assertIn('menuentry "Start PolyOS 7 (safe mode)"', menu)
            self.assertNotIn("Live system", menu)
            self.assertIn("menu label ^Start PolyOS 7", (iso / "isolinux/live.cfg").read_text())
            config = (grub / "config.cfg").read_text()
            self.assertIn("set timeout=30", config)
            self.assertIn("background_image /boot/grub/live-theme/boot.png", config)
            text = (theme / "theme.txt").read_text()
            self.assertTrue(text.startswith('terminal-box: "terminal_box_*.png"\n'))
            self.assertIn('terminal-width: "100%"', text)
            self.assertIn("left = 28%", text)
            self.assertIn('item_color = "#c9c2ea"', text)
            self.assertIn('selected_item_pixmap_style = "select_*.png"', text)
            self.assertIn("item_height = 24", text)
            self.assertIn("\titem_icon_space = 12", text)
            self.assertEqual((grub / "splash.png").read_bytes(), b"splash.png")
            self.assertEqual((iso / "isolinux/splash800x600.png").read_bytes(), b"splash.png")
            for name in ("boot.png", "terminal_box_c.png", "select_c.png"):
                self.assertEqual((theme / name).read_bytes(), name.encode())
            self.assertFalse(art.exists())


if __name__ == "__main__":
    unittest.main()


class InstalledBootMenuTests(unittest.TestCase):
    """The installed computer's GRUB theme: GRUB drops a theme it can't parse and shows its plain
    menu, so keep to what it reads (whole-number percentages, files that exist)."""

    def test_theme(self):
        import re

        root = paths.ROOT / "data"
        text = (root / "grub/theme.txt").read_text()
        self.assertFalse(re.search(r"\d\.\d+%", text), "GRUB can't read decimal percentages")
        self.assertIn('desktop-image: "background.png"', text)
        self.assertTrue((root / "boot/splash.png").is_file())
        self.assertTrue(list((root / "boot").glob("select_*.png")))
        self.assertEqual(text.count("{"), text.count("}"))

    def test_installer_uses_it(self):
        from polyos import installer

        self.assertEqual(installer.GRUB_THEME, "/usr/share/grub/themes/polyos")


class CtlTokenTests(ServerTests):
    """polyos-ctl's token (in a file other programs can read) opens only popups, apps, volume and power."""

    def test_ctl_token_is_limited(self):
        ok = [("GET", "/api/state", None), ("POST", "/api/popup", {"view": None}), ("POST", "/api/volume", {"level": 30}),
              ("POST", "/api/settings", {"developerMode": False})]
        for method, path, body in ok:
            self.assertEqual(self.request(method, path, body, token="ctl-token")[0], 200, path)
        refused = [("POST", "/api/store/install", {"id": "vlc"}), ("POST", "/api/admin/auth", {"password": "polyos"}),
                   ("POST", "/api/drivers/install", {"packages": ["nvidia-driver"]}), ("GET", "/api/vara/config", None),
                   ("POST", "/api/settings", {"wallpaper": "builtin:polyos-night.jpg"}), ("POST", "/api/account/pin", {"pin": "1234"}),
                   ("GET", "/api/files/list", None)]
        for method, path, body in refused:
            self.assertEqual(self.request(method, path, body, token="ctl-token")[0], 403, path)
