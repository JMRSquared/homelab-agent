"""Persistent, file-backed memory for the agent.

One markdown file holds every topic as its own `## topic` section. The file is
re-read and re-written on every call rather than cached in memory, so the
brain survives process restarts and, since it lives on the `tank` ZFS mount
inside the container, survives container rebuilds too.

The whole scheme rests on parsing the file by splitting on lines starting
with `## `. That makes the heading line load-bearing: a topic or a body of
content that could itself look like a heading would corrupt the file on the
next read, silently merging or truncating notes. `write()` rejects both
before anything touches disk.

`consolidate()` is the one operation here that touches more than a single
topic at once - the improvement cycle's way of merging, pruning, and
flagging contradictions in what would otherwise only ever grow. It is not
an NLP engine: deciding what to merge, what's stale, and what contradicts
what is the calling model's judgement (it reads everything back via
`topics()` first); this class only provides the safe mechanics - back up
the whole file before touching it, validate every topic name, and write the
result atomically enough that a bad merge is recoverable from the backup.
"""

import datetime as dt
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

# A topic becomes a markdown `## ` heading and a dict key read back from that
# heading, so it must survive strip() unchanged (no leading/trailing
# whitespace) and must not contain "#" or a newline (both of which could be,
# or produce, a heading line of their own).
TOPIC_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 _-]{0,58}[A-Za-z0-9])?$")

# A line starting with "## " anywhere in the content would read back as a
# second heading, silently truncating the real content at that point.
_FAKE_HEADING = re.compile(r"^## ", re.MULTILINE)

# The topic contradictory notes get appended under, rather than merged into
# whichever topic they concern - flagging a contradiction means leaving both
# versions visible for a human or a later, more careful pass to resolve, not
# silently picking one.
CONTRADICTIONS_TOPIC = "Contradictions"

# How many pre-consolidation backups to keep, oldest deleted first past this
# - same "cap, don't grow forever" shape as `agent/tools/outbox.py`'s
# cleanup. A brain is written to at most every 10 minutes by the
# improvement cycle and consolidated far less often than that, so even a
# small cap covers a long history of recoverable snapshots.
BACKUP_RETAIN = 20


@dataclass(frozen=True)
class ConsolidationResult:
    backup_path: str
    updated_topics: list[str]
    dropped_topics: list[str]
    contradictions_recorded: int


class Brain:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text("# Homelab agent brain\n\n", encoding="utf-8")

    def _sections(self) -> dict[str, str]:
        text = self._path.read_text(encoding="utf-8")
        parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)
        return {parts[i].strip(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}

    def _write_sections(self, sections: dict[str, str]) -> None:
        body = "# Homelab agent brain\n\n" + "\n\n".join(
            f"## {name}\n{text}" for name, text in sorted(sections.items())
        )
        self._path.write_text(body + "\n", encoding="utf-8")

    def topics(self) -> dict[str, str]:
        """Every topic currently on file, in full - what a caller reads
        before deciding what to merge, drop, or flag via `consolidate()`."""
        return self._sections()

    def read(self, topic: str) -> str:
        return self._sections().get(topic, "")

    def write(self, topic: str, content: str) -> None:
        if not TOPIC_PATTERN.match(topic):
            raise ValueError(
                f"invalid topic {topic!r}: use letters, digits, spaces, '_' or '-' only, "
                "no leading/trailing whitespace"
            )
        if _FAKE_HEADING.search(content):
            raise ValueError("content cannot contain a line starting with '## '")
        sections = self._sections()
        sections[topic] = content.strip()
        self._write_sections(sections)

    def _backups_dir(self) -> Path:
        backups_dir = self._path.parent / "brain-backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        return backups_dir

    def _backup(self) -> Path:
        backups_dir = self._backups_dir()
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%f")
        dest = backups_dir / f"{self._path.stem}-{stamp}.md"
        if self._path.exists():
            shutil.copy2(self._path, dest)
        else:
            dest.write_text("", encoding="utf-8")
        return dest

    def _prune_backups(self) -> None:
        backups_dir = self._backups_dir()
        files = sorted(
            backups_dir.glob(f"{self._path.stem}-*.md"), key=lambda p: p.stat().st_mtime
        )
        for stale in files[:-BACKUP_RETAIN]:
            stale.unlink(missing_ok=True)

    def consolidate(
        self,
        *,
        updates: dict[str, str] | None = None,
        drop: list[str] | None = None,
        contradictions: list[str] | None = None,
    ) -> ConsolidationResult:
        """Apply a caller-decided merge in one safe step: back up the
        current file, replace each topic named in `updates` with its new
        (already-merged) content, permanently remove each topic named in
        `drop`, and append each string in `contradictions` to a dedicated
        `Contradictions` topic instead of resolving them - a contradiction
        stays visible rather than being silently picked one way.

        Every topic name (`updates` keys and `drop` entries) is validated
        the same way `write()` validates a single topic, before anything
        touches disk - one bad name fails the whole call rather than
        partially applying it.
        """
        updates = dict(updates) if updates else {}
        drop = list(drop) if drop else []
        contradictions = list(contradictions) if contradictions else []

        for topic in (*updates.keys(), *drop):
            if not TOPIC_PATTERN.match(topic):
                raise ValueError(
                    f"invalid topic {topic!r}: use letters, digits, spaces, '_' or '-' only, "
                    "no leading/trailing whitespace"
                )
        for topic, content in updates.items():
            if _FAKE_HEADING.search(content):
                raise ValueError(
                    f"content for topic {topic!r} cannot contain a line starting with '## '"
                )

        backup_path = self._backup()
        sections = self._sections()

        updated_topics: list[str] = []
        for topic, content in updates.items():
            sections[topic] = content.strip()
            updated_topics.append(topic)

        dropped_topics: list[str] = []
        for topic in drop:
            if sections.pop(topic, None) is not None:
                dropped_topics.append(topic)

        if contradictions:
            stamp = dt.datetime.now(dt.UTC).isoformat()
            existing = sections.get(CONTRADICTIONS_TOPIC, "")
            new_lines = "\n".join(f"- [{stamp}] {note.strip()}" for note in contradictions)
            sections[CONTRADICTIONS_TOPIC] = (
                f"{existing}\n{new_lines}".strip() if existing else new_lines
            )
            if CONTRADICTIONS_TOPIC not in updated_topics:
                updated_topics.append(CONTRADICTIONS_TOPIC)

        self._write_sections(sections)
        self._prune_backups()

        return ConsolidationResult(
            backup_path=str(backup_path),
            updated_topics=updated_topics,
            dropped_topics=dropped_topics,
            contradictions_recorded=len(contradictions),
        )
