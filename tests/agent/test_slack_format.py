from agent.slack_format import to_mrkdwn


def test_bold_double_star():
    assert to_mrkdwn("**bold**") == "*bold*"


def test_bold_double_underscore():
    assert to_mrkdwn("__bold__") == "*bold*"


def test_italic_single_star():
    assert to_mrkdwn("*italic*") == "_italic_"


def test_italic_single_underscore():
    assert to_mrkdwn("_italic_") == "_italic_"


def test_heading_level_one():
    assert to_mrkdwn("# Heading") == "*Heading*"


def test_heading_deeper_levels_all_become_plain_bold():
    for marker in ("#", "##", "###", "####", "#####", "######"):
        assert to_mrkdwn(f"{marker} Section") == "*Section*"


def test_heading_mid_document_stays_on_its_own_line():
    text = "intro\n## Section\nbody"
    assert to_mrkdwn(text) == "intro\n*Section*\nbody"


def test_link():
    assert to_mrkdwn("[Jellyfin](http://10.0.0.165:8096)") == "<http://10.0.0.165:8096|Jellyfin>"


def test_strikethrough():
    assert to_mrkdwn("~~strike~~") == "~strike~"


def test_dash_bullet():
    assert to_mrkdwn("- item") == "• item"


def test_star_bullet():
    assert to_mrkdwn("* item") == "• item"


def test_bullet_list_multiple_lines():
    text = "- first\n- second\n* third"
    assert to_mrkdwn(text) == "• first\n• second\n• third"


def test_indented_bullet_keeps_its_indentation():
    assert to_mrkdwn("  - nested") == "  • nested"


def test_numbered_list_is_left_alone():
    text = "1. first\n2. second"
    assert to_mrkdwn(text) == text


def test_ordering_hazard_bold_and_italic_in_the_same_string():
    """The ordering hazard: **bold** must convert before single-asterisk
    italics, or it mangles into *bold* -> _bold_ (or worse - a stray
    asterisk consumed as an italic delimiter for unrelated text)."""
    text = "This is **bold** and this is *italic*."
    assert to_mrkdwn(text) == "This is *bold* and this is _italic_."


def test_ordering_hazard_bold_underscore_and_italic_underscore():
    text = "This is __bold__ and this is _italic_."
    assert to_mrkdwn(text) == "This is *bold* and this is _italic_."


def test_ordering_hazard_bold_then_italic_adjacent():
    text = "**bold** *italic*"
    assert to_mrkdwn(text) == "*bold* _italic_"


def test_fenced_code_block_with_markdown_characters_is_untouched():
    text = "Run this:\n```\ngrep -E '*foo|_bar_' file.txt\n```\ndone"
    expected = "Run this:\n```\ngrep -E '*foo|_bar_' file.txt\n```\ndone"
    assert to_mrkdwn(text) == expected


def test_fenced_code_block_with_bullet_looking_lines_is_untouched():
    text = "```\n- not a bullet\n* also not a bullet\n```"
    assert to_mrkdwn(text) == text


def test_inline_code_with_markdown_characters_is_untouched():
    text = "Set `MINIMAX_MODEL=*_special_*` in the env file."
    assert to_mrkdwn(text) == text


def test_bold_outside_code_converts_while_inline_code_stays_verbatim():
    text = "**Important**: run `echo *hi*` first."
    assert to_mrkdwn(text) == "*Important*: run `echo *hi*` first."


def test_realistic_daemon_reply():
    text = (
        "# Jellyfin restart\n"
        "- Container was unhealthy\n"
        "- Restarted via `docker_action`\n"
        "**Result**: healthy after 12s. See [logs](http://10.0.0.165:9999)."
    )
    expected = (
        "*Jellyfin restart*\n"
        "• Container was unhealthy\n"
        "• Restarted via `docker_action`\n"
        "*Result*: healthy after 12s. See <http://10.0.0.165:9999|logs>."
    )
    assert to_mrkdwn(text) == expected


def test_plain_text_is_unchanged():
    assert to_mrkdwn("nothing special here") == "nothing special here"


def test_empty_string():
    assert to_mrkdwn("") == ""
