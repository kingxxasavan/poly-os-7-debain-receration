"""Your own pictures as the wallpaper: Settings › Personalization › Your pictures."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from polyos.backend import Backend


class MyPicturesTests(unittest.TestCase):
    def test_lists_pictures_newest_first(self):
        home = Path(tempfile.mkdtemp())
        for rel, when in (("Pictures/trip/beach.JPG", 300), ("Downloads/cat.webp", 200), ("Desktop/notes.txt", 400),
                          ("Pictures/.hidden/x.png", 500), ("Documents/logo.svg", 600), ("Pictures/a/b/c/deep.png", 700)):
            path = home / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * 10)
            os.utime(path, (when, when))
        be = SimpleNamespace(files=SimpleNamespace(home=str(home)), PICTURE_FOLDERS=Backend.PICTURE_FOLDERS)
        names = [p["name"] for p in Backend.my_pictures(be)]
        self.assertEqual(names, ["beach.JPG", "cat.webp"])  # no text, hidden folders, SVGs or 4 folders deep


if __name__ == "__main__":
    unittest.main()
