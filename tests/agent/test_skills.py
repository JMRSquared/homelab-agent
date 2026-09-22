import re
from pathlib import Path

import pytest

from agent import improve, skills, tick
from agent.prompts import SKILLS_INDEX
from agent.slack_app import SYSTEM_CHAT
from agent.tools import skills as skills_tool
from agent.tools.base import dispatch


def _write(directory: Path, name: str, description: str, body: str = "body") -> Path:
    path = directory / f"{name}.md"
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n")
    return path


@pytest.fixture
def local_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SKILLS_DIR", str(tmp_path))
    return tmp_path


def test_parse_reads_frontmatter_and_body():
    skill = skills.parse("---\nname: demo\ndescription: When to use.\n---\n\n# Hi\n", "x.md")
    assert skill is not None
    assert (skill.name, skill.description, skill.body) == ("demo", "When to use.", "# Hi")


@pytest.mark.parametrize(
    "text",
    [
        "no frontmatter at all",
        "---\nname: demo\n---\nbody",
        "---\ndescription: missing name\n---\nbody",
        "---\nname: Bad Name\ndescription: x\n---\nbody",
    ],
)
def test_parse_rejects_malformed_files(text):
    assert skills.parse(text, "x.md") is None


def test_local_skill_overrides_bundled_one(local_dir):
    _write(local_dir, "homelab-map", "Local replacement.", "local body")
    assert skills.get("homelab-map").body == "local body"


def test_local_skill_is_picked_up_without_restart(local_dir):
    assert skills.get("brand-new") is None
    _write(local_dir, "brand-new", "Added on the box.")
    assert skills.get("brand-new") is not None


def test_malformed_local_file_does_not_hide_other_skills(local_dir):
    (local_dir / "broken.md").write_text("not a skill")
    _write(local_dir, "fine", "Still loads.")
    loaded = skills.load_all()
    assert "fine" in loaded and "broken" not in loaded


def test_missing_local_dir_is_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SKILLS_DIR", str(tmp_path / "nope"))
    assert "homelab-map" in skills.load_all()


def test_index_is_empty_without_skills():
    assert skills.index_text({}) == ""


def test_index_lists_every_skill_and_names_the_tool():
    text = skills.index_text()
    assert "skill_read" in text
    for name in skills.load_all():
        assert f"- {name}: " in text


def test_every_system_prompt_carries_the_index():
    assert SKILLS_INDEX
    for prompt in (SYSTEM_CHAT, tick.SYSTEM_DAEMON, improve.SYSTEM_IMPROVE):
        assert SKILLS_INDEX in prompt


def test_every_bundled_file_is_a_valid_skill_named_after_its_file():
    files = sorted(skills.BUNDLED_DIR.glob("*.md"))
    assert files
    for file in files:
        skill = skills.parse(file.read_text(encoding="utf-8"), str(file))
        assert skill is not None, file.name
        assert skill.name == file.stem


def test_skill_cross_references_resolve():
    """A skill saying 'see skill `x`' must point at a skill that exists."""
    bundled = skills._read_dir(skills.BUNDLED_DIR)
    for skill in bundled.values():
        for ref in re.findall(r"skill `([a-z0-9-]+)`", skill.body):
            assert ref in bundled, f"{skill.name} references missing skill {ref}"


def test_skill_read_tool_returns_content(local_dir):
    _write(local_dir, "demo", "Demo skill.", "the runbook")
    out = dispatch("skill_read", {"name": "demo"})
    assert out == {
        "ok": True,
        "result": {"name": "demo", "description": "Demo skill.", "content": "the runbook"},
    }


def test_skill_read_unknown_name_lists_what_exists(local_dir):
    out = dispatch("skill_read", {"name": "nope"})
    assert out["ok"] is False
    assert "homelab-map" in out["error"]


def test_skill_list_tool_lists_names_and_descriptions(local_dir):
    _write(local_dir, "demo", "Demo skill.")
    names = {s["name"] for s in skills_tool.skill_list()["skills"]}
    assert {"demo", "homelab-map"} <= names
