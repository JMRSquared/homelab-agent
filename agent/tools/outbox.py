"""Shared artifact workspace for tools that write files the model later
attaches to something (an email today; conceivably a Slack upload later).

Not a tool module itself - no `@tool` here, nothing in `REGISTRY`. Imported
by `photos.py`, `mt5_screenshot.py`, `mail.py`, and `vision.py`.

The directory is the existing `/tank/dev/agent/outbox/` bind mount (see the
capability brief), overridable via `AGENT_OUTBOX_DIR` for tests and for a
future second mount without a code change. It is created on demand - nothing
assumes it exists at import time - and every write is preceded by a cleanup
pass so a chatty afternoon of screenshots and photo downloads can't slowly
fill a pool that also holds the household's only copy of its photos.
"""

import os
import time
import uuid
from pathlib import Path

# Anything older than this is swept on the next write. A day is generous for
# "the email that used this attachment already went out or failed" while
# still bounding how long a stray file can sit around.
MAX_AGE_SECONDS = 24 * 60 * 60

# Belt-and-suspenders alongside the age cap: even within a day, nothing
# should let the outbox grow past this. 500MB is comfortably more than a
# realistic day of 3MB MT5 screenshots and a handful of JPEG previews, and
# comfortably less than "a meaningful bite out of the tank pool."
MAX_TOTAL_BYTES = 500 * 1024 * 1024


def _outbox_dir() -> Path:
    return Path(os.environ.get("AGENT_OUTBOX_DIR", "/tank/dev/agent/outbox"))


def _cleanup(directory: Path) -> None:
    """Delete anything older than MAX_AGE_SECONDS, then - if still over
    MAX_TOTAL_BYTES - delete oldest-first until back under the cap. Best
    effort: a file another process is mid-writing or has already removed
    is skipped rather than raising, since cleanup must never be the reason
    a tool call fails.
    """
    now = time.time()
    entries: list[tuple[float, int, Path]] = []
    for child in directory.iterdir():
        if not child.is_file():
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        if now - stat.st_mtime > MAX_AGE_SECONDS:
            try:
                child.unlink()
            except OSError:
                pass
            continue
        entries.append((stat.st_mtime, stat.st_size, child))

    total = sum(size for _, size, _ in entries)
    if total <= MAX_TOTAL_BYTES:
        return
    for _, size, path in sorted(entries, key=lambda e: e[0]):
        if total <= MAX_TOTAL_BYTES:
            break
        try:
            path.unlink()
            total -= size
        except OSError:
            pass


def new_artifact_path(stem: str, suffix: str) -> Path:
    """Return a fresh path inside the outbox for a file the caller is about
    to write, creating the directory and running cleanup first.

    `stem` is a human-readable prefix (e.g. "audi", "mt5-screenshot");
    a short random suffix is appended before the extension so two runs -
    even two runs in the same second - can never collide. `suffix` is the
    file extension including the dot (e.g. ".jpg").
    """
    directory = _outbox_dir()
    directory.mkdir(parents=True, exist_ok=True)
    _cleanup(directory)
    safe_stem = "".join(c if c.isalnum() or c in "-_" else "-" for c in stem)[:60] or "artifact"
    unique = uuid.uuid4().hex[:8]
    return directory / f"{safe_stem}-{unique}{suffix}"


def outbox_dir() -> Path:
    """The outbox directory, created if it doesn't exist yet."""
    directory = _outbox_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def resolve_in_outbox(raw_path: str) -> Path:
    """Validate that `raw_path` names a real file inside the outbox, and
    return its resolved Path. Shared by every tool that takes a local file
    path as an argument (`send_email`'s attachments, `image_inspect`'s
    image) - the model can only ever hand a tool a path it got back from
    another tool's result, never an arbitrary filesystem path, and this is
    the one place that rule is enforced.
    """
    if not raw_path or not raw_path.strip():
        raise ValueError("path must not be blank")
    root = outbox_dir().resolve()
    path = Path(raw_path).resolve()
    if not path.is_file():
        raise ValueError(f"file not found: {raw_path!r}")
    if path != root and root not in path.parents:
        raise ValueError(
            f"path must be inside the outbox ({root}), got {raw_path!r} - arbitrary "
            "filesystem paths are rejected"
        )
    return path
