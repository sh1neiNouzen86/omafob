import shutil
import subprocess
import time
import unittest
from pathlib import Path

from twofa.camera import CameraError, FrameScanner


@unittest.skipUnless(shutil.which("ZXingReader") and shutil.which("qrencode"), "QR tools unavailable")
class FrameScannerTest(unittest.TestCase):
    def test_frame_is_decoded_and_temporary_images_are_removed(self):
        scanner = FrameScanner(timeout=8)
        scanner.start()
        self.addCleanup(scanner.stop)
        directory = Path(scanner.frame_directory)
        payload = "otpauth://totp/Test:demo?secret=JBSWY3DPEHPK3PXP"
        frame = directory / "frame-1.jpg"
        # PNG data is accepted independently of the file extension.
        frame.write_bytes(subprocess.check_output(["qrencode", "-t", "PNG", "-o", "-", payload]))
        scanner.submit(frame.name)
        deadline = time.monotonic() + 6
        while not scanner.detected and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(scanner.detected)
        self.assertEqual(len(scanner.points), 4)
        self.assertFalse(frame.exists())
        while not scanner.finished and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(scanner.outcome()[0], [payload])
        self.assertFalse(directory.exists())

    def test_rejects_paths_outside_its_private_directory(self):
        scanner = FrameScanner()
        scanner.start()
        self.addCleanup(scanner.stop)
        with self.assertRaises(CameraError):
            scanner.submit("../other.jpg")


class FrameScannerValidationTest(unittest.TestCase):
    def test_invalid_path_is_rejected_and_cancel_cleans_directory(self):
        from unittest.mock import patch

        scanner = FrameScanner(timeout=5)
        directory = Path(scanner.frame_directory)
        with patch("twofa.camera.shutil.which", return_value="/fake/ZXingReader"):
            scanner.start()
            with self.assertRaises(CameraError):
                scanner.submit("../outside.jpg")
            scanner.stop()
            deadline = time.monotonic() + 3
            while not scanner.finished and time.monotonic() < deadline:
                time.sleep(0.05)
        self.assertTrue(scanner.finished)
        self.assertTrue(scanner.outcome()[2])
        self.assertFalse(directory.exists())
