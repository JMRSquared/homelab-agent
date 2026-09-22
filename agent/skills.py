"""Skills: curated, read-on-demand runbooks for the homelab.

The brain (agent/brain.py) is what the agent has learned by itself, one
topic at a time, and it only surfaces when something asks for a topic by
name. Skills are the other half: knowledge written down ahead of time by
the owner - where things live, how a service is wired, what a fault looks
like and how it gets fixed. Without them every conversation starts from
zero and the model guesses at IPs, stack names and file paths.

Each skill is one markdown file with a small frontmatter block:

    ---
    name: media-jellyfin
    description: One line saying when to read this skill.
    ---
    body...

Loading is progressive, the same shape Claude's own skills use: every system
prompt carries only the index (name + description, see `index_text()`), and
the model pulls a full body with the `skill_read` tool when a request
matches. That keeps the prompt small while the knowledge stays one tool call
away.

Two directories are read, bundled first:
- `agent/skills/` inside this package, shipped with every deploy.
- `AGENT_SKILLS_DIR` (default /tank/dev/agent/skills), for skills the owner
  or the agent adds on the box without a redeploy. A local skill with the
  same name as a bundled one replaces it.

Files are re-read on every call, same "the file is the state" model as the
brain, so a new local skill is live on the next `skill_list`/`skill_read`.
Only the prompt index is computed once, at import.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

BUNDLED_DIR = Path(__file__).parent / "skills"
DEFAULT_LOCAL_DIR = "/tank/dev/agent/skills"

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    path: str


def _local_dir() -> Path:
    return Path(os.environ.get("AGENT_SKILLS_DIR", DEFAULT_LOCAL_DIR))


def parse(text: str, path: str) -> Skill | None:
    """Parse one skill file. Returns None for anything malformed rather than
    raising: one bad file dropped on the box must not take every other
    skill (or the prompt index built at import) down with it."""
    match = _FRONTMATTER.match(text)
    if match is None:
        return None
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    name = meta.get("name", "")
    description = meta.get("description", "")
    if not NAME_PATTERN.match(name) or not description:
        return None
    return Skill(name=name, description=description, body=match.group(2).strip(), path=path)


def _read_dir(directory: Path) -> dict[str, Skill]:
    found: dict[str, Skill] = {}
    try:
        files = sorted(directory.glob("*.md"))
    except OSError:
        return found
    for file in files:
        try:
            skill = parse(file.read_text(encoding="utf-8"), str(file))
        except OSError:
            continue
        if skill is not None:
            found[skill.name] = skill
    return found


def load_all() -> dict[str, Skill]:
    """Every skill, bundled then local, local winning on a name clash."""
    return {**_read_dir(BUNDLED_DIR), **_read_dir(_local_dir())}


def get(name: str) -> Skill | None:
    return load_all().get(name)


def index_text(skills: dict[str, Skill] | None = None) -> str:
    """The block every system prompt carries: what skills exist and when to
    read one. Empty string when there are none, so a prompt never tells the
    model to call a tool for a list that isn't there."""
    skills = load_all() if skills is None else skills
    if not skills:
        return ""
    lines = "\n".join(f"- {name}: {skills[name].description}" for name in sorted(skills))
    return (
        "\n\nYou have skills: runbooks the owner wrote about this exact homelab - where "
        "every service lives, how it is wired, what its known faults look like and how "
        "they get fixed. Before you act on or answer anything a skill below covers, call "
        "skill_read with its name and follow it. Read the skill first rather than "
        "guessing an IP, a stack name, a path or a command; if two apply, read both. "
        "Skills are facts written ahead of time; if what you observe disagrees with one, "
        "trust the live system, say so, and record the correction with brain_write.\n"
        f"{lines}"
    )
