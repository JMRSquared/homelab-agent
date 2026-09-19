import subprocess

import pytest

from hostctl import screendump


def test_capture_png_runs_monitor_then_ffmpeg_and_cleans_up(monkeypatch, tmp_path):
    monkeypatch.setattr(screendump, "TMP_DIR", tmp_path)
    calls: list[list[str]] = []

    def fake_run(argv, *, input_text=None, timeout):
        calls.append(argv)
        if argv[:2] == ["qm", "monitor"]:
            # Simulate the PPM landing on disk, the way the real qm monitor
            # command would as a side effect of the screendump command.
            ppm_path = input_text.split()[1].strip()
            with open(ppm_path, "wb") as f:
                f.write(b"P6 fake ppm")
        elif argv[0] == "ffmpeg":
            png_path = argv[-1]
            with open(png_path, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\nfakepngbytes")

    monkeypatch.setattr(screendump, "_run", fake_run)
    monkeypatch.setattr(screendump, "POLL_INTERVAL", 0.01)

    png = screendump.capture_png(200)

    assert png.startswith(b"\x89PNG")
    assert calls[0][:2] == ["qm", "monitor"]
    assert calls[1][0] == "ffmpeg"
    # Intermediate files must not survive the call.
    assert list(tmp_path.iterdir()) == []


def test_capture_png_raises_typed_error_when_monitor_times_out(monkeypatch, tmp_path):
    monkeypatch.setattr(screendump, "TMP_DIR", tmp_path)

    def fake_run(argv, *, input_text=None, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    monkeypatch.setattr(screendump, "_run", fake_run)

    with pytest.raises(screendump.ScreendumpTimeoutError):
        screendump.capture_png(200)
    assert list(tmp_path.iterdir()) == []


def test_capture_png_raises_typed_error_when_ppm_never_appears(monkeypatch, tmp_path):
    """The monitor command can return without the file ever landing (a busy
    host, a bad path) - this must time out with a typed error, not hang."""
    monkeypatch.setattr(screendump, "TMP_DIR", tmp_path)
    monkeypatch.setattr(screendump, "APPEAR_TIMEOUT", 0.05)
    monkeypatch.setattr(screendump, "POLL_INTERVAL", 0.01)

    def fake_run(argv, *, input_text=None, timeout):
        pass  # qm monitor "succeeds" but never actually writes the file

    monkeypatch.setattr(screendump, "_run", fake_run)

    with pytest.raises(screendump.ScreendumpTimeoutError):
        screendump.capture_png(200)


def test_capture_png_raises_typed_error_when_ffmpeg_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(screendump, "TMP_DIR", tmp_path)
    monkeypatch.setattr(screendump, "POLL_INTERVAL", 0.01)

    def fake_run(argv, *, input_text=None, timeout):
        if argv[:2] == ["qm", "monitor"]:
            ppm_path = input_text.split()[1].strip()
            with open(ppm_path, "wb") as f:
                f.write(b"P6 fake ppm")
            return
        raise subprocess.CalledProcessError(1, argv, output="", stderr="unsupported codec")

    monkeypatch.setattr(screendump, "_run", fake_run)

    with pytest.raises(screendump.ScreendumpConversionError):
        screendump.capture_png(200)
    assert list(tmp_path.iterdir()) == []


def test_capture_png_never_retries():
    """No retry loop anywhere in the module - a single failed subprocess
    call must propagate immediately rather than being tried again."""
    import inspect

    source = inspect.getsource(screendump)
    assert "for attempt" not in source
    assert "while True" not in source
