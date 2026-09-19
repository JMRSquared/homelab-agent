"""Screen capture for a QEMU guest via `qm monitor`, converted to PNG.

Built for guest 200 (mt5, the live trading VM) but not hardcoded to it -
`vmid` is a parameter. `qm monitor <vmid>` opens an interactive QEMU monitor
session that reads commands from stdin until EOF; `screendump <path>` is one
of those commands and is read-only against the guest - it captures the
framebuffer without touching guest input, so it cannot disturb whatever is
open on screen (a requirement here specifically because that screen can be
an open MT5 terminal). This module never sends any other monitor command.

Deliberately no retry loop anywhere in this module: a screendump that
doesn't show up raises a typed error once and stops. This runs against a
live trading VM; retrying blind against it is worse than failing loudly.
"""

import subprocess
import time
import uuid
from pathlib import Path

# Time budget for the `qm monitor` invocation itself to return.
MONITOR_TIMEOUT = 15
# Time budget, after the monitor command returns, to wait for the PPM file
# to actually land on disk - `qm monitor` returning doesn't guarantee the
# write already landed on a busy host.
APPEAR_TIMEOUT = 20
POLL_INTERVAL = 0.5
# 1280x800 uncompressed is ~3MB; ffmpeg converting that to PNG is fast, but
# a busy host can stall any subprocess - give it real room without hanging
# forever.
CONVERT_TIMEOUT = 30

TMP_DIR = Path("/tmp")


class ScreendumpTimeoutError(RuntimeError):
    """`qm monitor` didn't respond, or the PPM capture never appeared on
    disk, within the allotted time."""


class ScreendumpConversionError(RuntimeError):
    """ffmpeg failed to convert the PPM capture to PNG, or the expected
    PNG output never appeared."""


def _run(argv: list[str], *, input_text: str | None = None, timeout: float) -> None:
    subprocess.run(
        argv, input=input_text, capture_output=True, text=True, timeout=timeout, check=True
    )


def capture_png(vmid: int = 200) -> bytes:
    """Screendump `vmid`'s framebuffer and return it as PNG bytes.

    Intermediate PPM and PNG files on the host are always removed, on
    every exit path, success or failure.
    """
    token = uuid.uuid4().hex
    ppm_path = TMP_DIR / f"hostctl-screendump-{vmid}-{token}.ppm"
    png_path = TMP_DIR / f"hostctl-screendump-{vmid}-{token}.png"
    try:
        try:
            _run(
                ["qm", "monitor", str(vmid)],
                input_text=f"screendump {ppm_path}\n",
                timeout=MONITOR_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise ScreendumpTimeoutError(
                f"`qm monitor {vmid}` did not respond within {MONITOR_TIMEOUT}s"
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise ScreendumpTimeoutError(f"`qm monitor {vmid}` failed: {detail}") from exc

        deadline = time.monotonic() + APPEAR_TIMEOUT
        while not ppm_path.exists():
            if time.monotonic() >= deadline:
                raise ScreendumpTimeoutError(
                    f"screendump for guest {vmid} did not appear at {ppm_path} within "
                    f"{APPEAR_TIMEOUT}s"
                )
            time.sleep(POLL_INTERVAL)

        try:
            _run(["ffmpeg", "-y", "-i", str(ppm_path), str(png_path)], timeout=CONVERT_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise ScreendumpConversionError(
                f"ffmpeg did not finish converting the guest {vmid} screendump within "
                f"{CONVERT_TIMEOUT}s"
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise ScreendumpConversionError(
                f"ffmpeg failed converting the guest {vmid} screendump: {detail}"
            ) from exc

        if not png_path.exists():
            raise ScreendumpConversionError(
                f"ffmpeg reported success but {png_path} does not exist"
            )
        return png_path.read_bytes()
    finally:
        ppm_path.unlink(missing_ok=True)
        png_path.unlink(missing_ok=True)
