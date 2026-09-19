import pytest

from agent.brain import Brain


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
