from pathlib import Path

import pytest

from agent.brain import BACKUP_RETAIN, Brain


def test_write_then_read_a_topic(tmp_path):
    brain = Brain(str(tmp_path / "brain.md"))
    brain.write("jellyfin", "restarts weekly around 3am, cause unknown")
    assert "restarts weekly" in brain.read("jellyfin")


def test_write_replaces_rather_than_duplicates(tmp_path):
    path = tmp_path / "brain.md"
    brain = Brain(str(path))
    brain.write("zfs", "first note")
    brain.write("zfs", "second note")
    assert path.read_text().count("## zfs") == 1
    assert "first note" not in brain.read("zfs")


def test_unknown_topic_reads_empty(tmp_path):
    assert Brain(str(tmp_path / "brain.md")).read("nothing") == ""


def test_topic_round_trips_across_a_fresh_brain_instance(tmp_path):
    # A second Brain() re-parses the file from scratch, so this catches any
    # mismatch between what write() stores as a dict key and what a later
    # read (after a real reload) parses the heading back into.
    path = tmp_path / "brain.md"
    Brain(str(path)).write("jellyseerr", "note one")
    Brain(str(path)).write("zfs pool", "note two")
    reloaded = Brain(str(path))
    assert reloaded.read("jellyseerr") == "note one"
    assert reloaded.read("zfs pool") == "note two"


def test_topic_with_hash_is_rejected(tmp_path):
    brain = Brain(str(tmp_path / "brain.md"))
    with pytest.raises(ValueError):
        brain.write("## injected heading", "content")


def test_topic_with_newline_is_rejected(tmp_path):
    brain = Brain(str(tmp_path / "brain.md"))
    with pytest.raises(ValueError):
        brain.write("zfs\n## other", "content")


def test_topic_with_leading_or_trailing_whitespace_is_rejected(tmp_path):
    brain = Brain(str(tmp_path / "brain.md"))
    with pytest.raises(ValueError):
        brain.write(" zfs", "content")
    with pytest.raises(ValueError):
        brain.write("zfs ", "content")


def test_content_containing_a_fake_heading_is_rejected(tmp_path):
    brain = Brain(str(tmp_path / "brain.md"))
    with pytest.raises(ValueError):
        brain.write("zfs", "first line\n## fake heading\nsecond line")


def test_content_round_trips_with_blank_lines_and_hashes_mid_line(tmp_path):
    brain = Brain(str(tmp_path / "brain.md"))
    content = "line one\n\nline two has a # but not at line start"
    brain.write("notes", content)
    assert brain.read("notes") == content


def test_consolidate_backs_up_before_writing_and_the_backup_is_recoverable(tmp_path):
    path = tmp_path / "brain.md"
    brain = Brain(str(path))
    brain.write("jellyfin", "original note")

    result = brain.consolidate(updates={"jellyfin": "merged note"})

    backup_path = Path(result.backup_path)
    assert backup_path.exists()
    # The backup holds the pre-consolidation content - a bad merge is
    # recoverable by reading it back.
    assert "original note" in backup_path.read_text()
    assert brain.read("jellyfin") == "merged note"


def test_consolidate_merges_and_drops(tmp_path):
    brain = Brain(str(tmp_path / "brain.md"))
    brain.write("zfs", "old note")
    brain.write("stale topic", "no longer relevant")

    result = brain.consolidate(updates={"zfs": "merged zfs note"}, drop=["stale topic"])

    assert result.updated_topics == ["zfs"]
    assert result.dropped_topics == ["stale topic"]
    assert brain.read("zfs") == "merged zfs note"
    assert brain.read("stale topic") == ""


def test_consolidate_flags_a_contradiction_rather_than_resolving_it_silently(tmp_path):
    brain = Brain(str(tmp_path / "brain.md"))
    brain.write("jellyfin", "restarts weekly, cause unknown")

    result = brain.consolidate(
        contradictions=["jellyfin: one note says weekly restarts, another says never restarts"]
    )

    assert result.contradictions_recorded == 1
    # The original topic is untouched - nothing was picked for it.
    assert brain.read("jellyfin") == "restarts weekly, cause unknown"
    # The contradiction is visible under its own topic, not silently resolved.
    flagged = brain.read("Contradictions")
    assert "one note says weekly restarts" in flagged


def test_consolidate_appends_to_existing_contradictions_topic(tmp_path):
    brain = Brain(str(tmp_path / "brain.md"))
    brain.consolidate(contradictions=["first contradiction"])
    brain.consolidate(contradictions=["second contradiction"])
    flagged = brain.read("Contradictions")
    assert "first contradiction" in flagged
    assert "second contradiction" in flagged


def test_consolidate_rejects_an_invalid_topic_name_before_writing_anything(tmp_path):
    path = tmp_path / "brain.md"
    brain = Brain(str(path))
    brain.write("zfs", "kept as-is")
    with pytest.raises(ValueError):
        brain.consolidate(updates={"## injected": "x"})
    # Nothing was touched - the valid topic is untouched and no backup
    # rewrite happened for this rejected call.
    assert brain.read("zfs") == "kept as-is"


def test_consolidate_prunes_old_backups_past_the_retention_cap(tmp_path):
    brain = Brain(str(tmp_path / "brain.md"))
    for i in range(BACKUP_RETAIN + 5):
        brain.consolidate(updates={"topic": f"note {i}"})
    backups_dir = tmp_path / "brain-backups"
    assert len(list(backups_dir.glob("*.md"))) <= BACKUP_RETAIN
