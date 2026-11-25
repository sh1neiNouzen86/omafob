import fcntl
import glob
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path

from .process import die_with_parent

DEFAULT_TIMEOUT = 45

_TOOL = "zbarcam"
_ARGS = ["--nodisplay", "--quiet", "--raw", "--oneshot", "-Sdisable", "-Sqrcode.enable"]


class CameraError(Exception):
    pass


def available():
    return shutil.which(_TOOL) is not None


def find_camera():
    # A machine may expose a video decoder before its actual camera.
    def order(path):
        suffix = path.removeprefix("/dev/video")
        return int(suffix) if suffix.isdigit() else float("inf")

    for path in sorted(glob.glob("/dev/video*"), key=order):
        try:
            with open(path, "rb", buffering=0) as device:
                capabilities = bytearray(104)
                fcntl.ioctl(device.fileno(), 0x80685600, capabilities)  # VIDIOC_QUERYCAP
                caps, device_caps = struct.unpack_from("II", capabilities, 84)
                if caps & 0x80000000:
                    caps = device_caps
                if caps & 0x00000001 and caps & 0x04000000:
                    return path
        except OSError:
            continue
    return None


# zbarcam in a thread, polled through finished and outcome().
class Scanner:
    def __init__(self, device=None, timeout=DEFAULT_TIMEOUT):
        self.device = device
        self.timeout = max(1, int(timeout or DEFAULT_TIMEOUT))
        self._process = None
        self._thread = None
        self._guard = threading.Lock()
        self._payloads = None
        self._message = None
        self._cancelled = False
        self._finished = False

    @property
    def finished(self):
        with self._guard:
            return self._finished

    def outcome(self):
        with self._guard:
            return self._payloads, self._message, self._cancelled

    def start(self):
        if not available():
            raise CameraError("zbarcam is not installed")
        device = self.device or find_camera()
        command = [_TOOL, *_ARGS] + ([device] if device else [])
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                preexec_fn=die_with_parent,
            )
        except OSError as error:
            raise CameraError(f"the camera could not be opened: {error}") from error
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        with self._guard:
            self._cancelled = True
        self._terminate()

    def _run(self):
        payloads = None
        message = None
        try:
            output, _ = self._process.communicate(timeout=self.timeout)
            lines = [line.strip() for line in output.decode("utf-8", "replace").splitlines() if line.strip()]
            if lines:
                payloads = lines
            else:
                message = "the camera did not see a QR code"
        except subprocess.TimeoutExpired:
            self._terminate()
            message = "the camera did not see a QR code"
        except Exception as error:
            self._terminate()
            message = str(error) or "the camera stopped unexpectedly"

        with self._guard:
            self._payloads = payloads
            self._message = message
            self._finished = True

    def _terminate(self):
        process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


class FrameScanner:
    """Decode frames captured by the panel without opening a second camera."""

    def __init__(self, device=None, timeout=DEFAULT_TIMEOUT):
        import queue
        import tempfile

        self.timeout = max(1, int(timeout or DEFAULT_TIMEOUT))
        self._directory = tempfile.TemporaryDirectory(prefix="omafob-camera-", dir="/dev/shm")
        self.frame_directory = self._directory.name
        self._queue = queue.Queue(maxsize=1)
        self._guard = threading.Lock()
        self._stop_event = threading.Event()
        self._finished = False
        self._detected = False
        self._cancelled = False
        self._payloads = None
        self._message = None
        self._points = []

    @property
    def finished(self):
        with self._guard:
            return self._finished

    @property
    def detected(self):
        with self._guard:
            return self._detected

    @property
    def points(self):
        with self._guard:
            return self._points[:]

    def outcome(self):
        with self._guard:
            return self._payloads, self._message, self._cancelled

    def start(self):
        if not shutil.which("ZXingReader"):
            self._directory.cleanup()
            raise CameraError("ZXingReader is not installed; install the zxing-cpp package")
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, name):
        import queue
        import re

        if not re.fullmatch(r"frame-[0-9]+\.jpg", name or ""):
            raise CameraError("invalid camera frame")
        path = Path(self.frame_directory) / name
        if self.finished or self.detected or self._stop_event.is_set():
            path.unlink(missing_ok=True)
            return
        try:
            self._queue.put_nowait(path)
        except queue.Full:
            path.unlink(missing_ok=True)

    def stop(self):
        with self._guard:
            self._cancelled = True
        self._stop_event.set()

    def _run(self):
        import json
        import queue
        import re

        deadline = time.monotonic() + self.timeout
        try:
            while not self._stop_event.is_set():
                if time.monotonic() >= deadline:
                    self._message = "the camera did not see a QR code"
                    break
                try:
                    path = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    # Frames live only in the private, memory-backed directory.
                    data = path.read_bytes()
                except FileNotFoundError:
                    continue
                finally:
                    path.unlink(missing_ok=True)
                if len(data) > 16 * 1024 * 1024:
                    continue
                result = subprocess.run(
                    ["ZXingReader", "-json", "-single", "-formats", "QRCode", "-"],
                    input=data,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    preexec_fn=die_with_parent,
                )
                if not result.stdout:
                    continue
                found = json.loads(result.stdout)
                text = found.get("Text", "")
                if not text or "\n" in text:
                    continue
                points = [[int(x), int(y)] for x, y in re.findall(r"(\d+)x(\d+)", found.get("Position", ""))]
                with self._guard:
                    self._detected = True
                    self._points = points
                    self._payloads = [text]
                self._stop_event.wait(2)
                break
        except Exception as error:
            self._message = str(error) or "the camera stopped unexpectedly"
        finally:
            self._directory.cleanup()
            with self._guard:
                self._finished = True
