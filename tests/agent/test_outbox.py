import os
import time

import pytest

from agent.tools import outbox


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(tmp_path / "outbox"))


def test_new_artifact_path_creates_dir_and_is_unique():
    p1 = outbox.new_artifact_path("audi", ".jpg")
    p1.write_bytes(b"x")
    p2 = outbox.new_artifact_path("audi", ".jpg")
    assert p1 != p2
    assert p1.parent.is_dir()


def test_new_artifact_path_sanitises_stem():
    p = outbox.new_artifact_path("../../etc/passwd", ".jpg")
    assert ".." not in p.name
    assert "/" not in p.name


def test_cleanup_removes_files_older_than_a_day():
    directory = outbox.outbox_dir()
    stale = directory / "old.jpg"
    stale.write_bytes(b"x")
    old_time = time.time() - outbox.MAX_AGE_SECONDS - 3600
    os.utime(stale, (old_time, old_time))

    outbox.new_artifact_path("fresh", ".jpg")

    assert not stale.exists()


def test_cleanup_caps_total_size_oldest_first(monkeypatch):
    monkeypatch.setattr(outbox, "MAX_TOTAL_BYTES", 100)
    directory = outbox.outbox_dir()

    older = directory / "older.bin"
    older.write_bytes(b"x" * 60)
    os.utime(older, (time.time() - 10, time.time() - 10))

    newer = directory / "newer.bin"
    newer.write_bytes(b"x" * 60)

    # Writing one more artifact should trigger cleanup and evict the older
    # file to get back under the cap, not the newer one.
    outbox.new_artifact_path("third", ".bin")

    assert not older.exists()
    assert newer.exists()
