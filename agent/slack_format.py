"""Convert the model's standard Markdown into Slack's mrkdwn.

The model (and the system prompts driving it) write ordinary Markdown -
`**bold**`, `# headings`, `[text](url)`, `- bullets`. Slack does not render
any of that; it renders mrkdwn, a different and much smaller dialect. Left
unconverted, the family sees raw asterisks and hashes in every reply.

This is a presentation-layer concern, applied at the point a reply is
actually posted to Slack (`agent/slack_app.py`'s `handle_message`,
`agent/tools/comms.py`'s `slack_say`) - not inside `agent/model.py`, which
must keep returning the model's text unchanged so the tick path's event log
and the `#agent-log` audit trail both see exactly what the model said,
before any presentation transform touches it.
"""

import re
from re import Match

# Fenced blocks first (```...```, DOTALL so they can span lines), then
# inline code (`...`, not crossing a line - Markdown inline code never
# does). Alternation order matters here too: a fenced block's own backtick
# run must be claimed by the first branch before the second branch's
# single-backtick pattern gets a chance to carve it up.
_CODE_SPAN_RE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)

# One NUL-delimited placeholder per protected span. NUL never appears in
# ordinary chat text and survives every transform below untouched, since
# none of them match on \x00.
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")

_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^([ \t]*)[-*][ \t]+", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_STRIKE_RE = re.compile(r"~~(.+?)~~")

# **bold**/__bold__ must be tried before *italic*/_italic_ at every scan
# position, or "**bold**" mangles into an empty italic span plus leftover
# asterisks (the classic failure: **bold** -> *bold* -> _bold_ or worse).
# A single combined regex with alternation in this order, resolved in one
# left-to-right pass, gets this right: at the position of "**bold**", the
# bold alternative matches (it requires and finds the second "*"), so the
# italic alternative - which could also start matching at the very same
# "*" - is never tried there at all. Two separately-run regexes (bold pass
# then italic pass) cannot give this guarantee: after the first pass
# rewrites "**bold**" to "*bold*", a second pass looking for *italic* would
# match that same text again.
_BOLD_ITALIC_RE = re.compile(
    r"\*\*(?P<bold_star>.+?)\*\*"
    r"|__(?P<bold_under>.+?)__"
    r"|\*(?P<italic_star>.+?)\*"
    r"|_(?P<italic_under>.+?)_"
)


def _bold_italic_sub(match: Match[str]) -> str:
    bold = match.group("bold_star") or match.group("bold_under")
    if bold is not None:
        return f"*{bold}*"
    italic = match.group("italic_star") or match.group("italic_under")
    if italic is not None:
        return f"_{italic}_"
    return match.group(0)  # pragma: no cover - alternation always matches a group


def _convert(text: str) -> str:
    # Bullets before bold/italic: a bare line-leading "* " has no matching
    # close on that line, but a badly-timed regex pass could still walk
    # across it looking for one. Converting "* "/"- " to "• " first removes
    # the ambiguous marker before the bold/italic pass ever runs.
    text = _BULLET_RE.sub(r"\1• ", text)
    text = _LINK_RE.sub(r"<\2|\1>", text)
    text = _BOLD_ITALIC_RE.sub(_bold_italic_sub, text)
    text = _STRIKE_RE.sub(r"~\1~", text)
    # Headings last: the heading target ("*Heading*") is single-asterisk
    # Slack bold syntax, indistinguishable from italic input syntax. Doing
    # this before the bold/italic pass means that very output gets walked
    # straight back over and mangled into "_Heading_" - the same ordering
    # hazard the bold/italic pass itself exists to avoid, just one step
    # later in the pipeline.
    text = _HEADING_RE.sub(r"*\1*", text)
    return text


def to_mrkdwn(text: str) -> str:
    """Convert Markdown to Slack mrkdwn. Pure function: same input, same
    output, no I/O.

    Mapping applied to everything outside a code span:
      **bold**, __bold__          -> *bold*
      *italic*, _italic_          -> _italic_
      # Heading (any level)       -> *Heading* on its own line
      [text](url)                 -> <url|text>
      ~~strike~~                  -> ~strike~
      "- " / "* " bullets         -> "• "
      numbered lists              -> left as-is (Slack renders them fine)

    Fenced ``` code blocks ``` and `inline code` spans are located first
    and never touched by any of the above - their content, including any
    literal `*`/`_`/`#` a shell snippet happens to contain, is restored
    verbatim.
    """
    codes: list[str] = []

    def _stash(match: Match[str]) -> str:
        codes.append(match.group(0))
        return f"\x00{len(codes) - 1}\x00"

    protected = _CODE_SPAN_RE.sub(_stash, text)
    converted = _convert(protected)
    return _PLACEHOLDER_RE.sub(lambda m: codes[int(m.group(1))], converted)
