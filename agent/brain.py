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
"""

import re
from pathlib import Path

# A topic becomes a markdown `## ` heading and a dict key read back from that
# heading, so it must survive strip() unchanged (no leading/trailing
# whitespace) and must not contain "#" or a newline (both of which could be,
# or produce, a heading line of their own).
TOPIC_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 _-]{0,58}[A-Za-z0-9])?$")

# A line starting with "## " anywhere in the content would read back as a
# second heading, silently truncating the real content at that point.
_FAKE_HEADING = re.compile(r"^## ", re.MULTILINE)


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
        body = "# Homelab agent brain\n\n" + "\n\n".join(
            f"## {name}\n{text}" for name, text in sorted(sections.items())
        )
        self._path.write_text(body + "\n", encoding="utf-8")
