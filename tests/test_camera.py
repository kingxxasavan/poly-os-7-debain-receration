"""The Camera app asks the system whether there's a camera, and whether it's an Intel IPU6 one."""

import tempfile
import unittest
from pathlib import Path

from polyos import power


def pci(root: Path, name: str, vendor: str, device: str) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "vendor").write_text(vendor + "\n")
    (root / name / "device").write_text(device + "\n")


class CameraTests(unittest.TestCase):
    def test_ipu6(self):
        root = Path(tempfile.mkdtemp())
        pci(root, "0000:00:02.0", "0x8086", "0x46a6")  # graphics
        self.assertFalse(power.has_ipu6(root))
        pci(root, "0000:00:05.0", "0x8086", "0x465d")  # IPU6
        self.assertTrue(power.has_ipu6(root))
        self.assertFalse(power.has_ipu6(root / "missing"))

    def test_webcam_node(self):
        root = Path(tempfile.mkdtemp())
        for node, name, index in (("video0", "Integrated Camera: Integrated C", 0), ("video1", "Integrated Camera: Integrated C", 1)):
            (root / node).mkdir()
            (root / node / "name").write_text(name)
            (root / node / "index").write_text(str(index))
        self.assertTrue(power.has_camera(root))
        self.assertFalse(power.has_camera(Path(tempfile.mkdtemp())))


if __name__ == "__main__":
    unittest.main()
