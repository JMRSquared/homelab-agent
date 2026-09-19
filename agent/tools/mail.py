"""Send email with attachments through the homelab's own mail server
(Stalwart, LXC 103) rather than a third-party provider.

Uses `smtplib` and `email` from the standard library only - no new
dependency, per the capability brief. Every env var is read at call time
(`os.environ.get`/`os.environ[...]` inside `send_email`, not at import), the
same pattern every other tool module in this package follows, so a value
changed in `/etc/homelab-agent/env` and reloaded takes effect on the next
call without a restart.
"""

import os
import re
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any

from agent.tools import imaging, outbox
from agent.tools.base import tool

# Stalwart on LXC 103, 10.0.0.167 - see docs/deploy.md for how that address
# was confirmed (it's easy to mistake for 104, the agent's own container).
# Overridable so tests never touch a real host and so the address can move
# without a code change.
DEFAULT_SMTP_HOST = "10.0.0.167"
DEFAULT_SMTP_PORT = 465
DEFAULT_FROM = "admin@mail.jmrsquared.com"

SMTP_TIMEOUT = 30.0

# Gmail - the target of the capability brief's own worked example - caps
# incoming messages at 25MB total, and base64-encoding attachments adds
# roughly a third to their raw size. Capping the raw attachment total well
# under that leaves headroom for encoding overhead and headers, and fails
# fast and clearly here instead of as a confusing bounce from the far end.
MAX_ATTACHMENTS_BYTES = 15 * 1024 * 1024

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(
            f"missing required env var {name} - send_email cannot authenticate to the "
            "mail container without it. The owner sets this via deploy/set-secrets.sh "
            "on LXC 104; until then this tool stays unavailable, and every other tool "
            "keeps working."
        )
    return value


@tool(
    "send_email",
    "Send an email, with optional attachments, from admin@mail.jmrsquared.com through the "
    "homelab's own mail server. `attachments` is a list of local file paths - each must "
    "already exist inside the agent's outbox (e.g. paths returned by photos_download or "
    "mt5_screenshot), not an arbitrary filesystem path; pass an empty list for no "
    "attachments. There is no recipient allowlist - any address is accepted, so confirm "
    "`to` is right before calling. Returns the mail server's own assigned message id, not "
    "just confirmation that something was sent.",
    {
        "type": "object",
        "properties": {
            "to": {"type": "string", "minLength": 3},
            "subject": {"type": "string", "minLength": 1},
            "body": {"type": "string"},
            "attachments": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["to", "subject", "body", "attachments"],
        "additionalProperties": False,
    },
)
def send_email(to: str, subject: str, body: str, attachments: list[str]) -> dict[str, Any]:
    to = to.strip()
    if not _EMAIL_RE.match(to):
        raise ValueError(f"not a valid-looking email address: {to!r}")
    subject = subject.strip()
    if not subject:
        raise ValueError("subject must not be blank")

    paths = [outbox.resolve_in_outbox(p) for p in attachments]
    total_bytes = sum(p.stat().st_size for p in paths)
    if total_bytes > MAX_ATTACHMENTS_BYTES:
        raise ValueError(
            f"attachments total {total_bytes} bytes, over the "
            f"{MAX_ATTACHMENTS_BYTES}-byte cap"
        )

    # Read the password last, after every other validation - a bad
    # recipient or an out-of-outbox attachment should fail before the tool
    # even asks whether mail is configured at all.
    password = _require_env("MAIL_PASSWORD")
    host = os.environ.get("MAIL_SMTP_HOST", DEFAULT_SMTP_HOST)
    port = int(os.environ.get("MAIL_SMTP_PORT", str(DEFAULT_SMTP_PORT)))
    sender = os.environ.get("MAIL_FROM", DEFAULT_FROM)

    msg = EmailMessage()
    msg["Message-ID"] = make_msgid()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    for path in paths:
        data = path.read_bytes()
        content_type = imaging.sniff_content_type(data, path.name)
        maintype, _, subtype = content_type.partition("/")
        msg.add_attachment(
            data,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=path.name,
        )

    with smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT) as smtp:
        smtp.login(sender, password)
        mail_code, mail_resp = smtp.mail(sender)
        if mail_code >= 400:
            raise RuntimeError(f"mail server refused sender {sender!r}: {mail_code} {mail_resp!r}")
        rcpt_code, rcpt_resp = smtp.rcpt(to)
        if rcpt_code >= 400:
            raise RuntimeError(f"mail server refused recipient {to!r}: {rcpt_code} {rcpt_resp!r}")
        data_code, data_resp = smtp.data(msg.as_bytes())
        if data_code >= 400:
            raise RuntimeError(f"mail server refused the message: {data_code} {data_resp!r}")

    response_text = (
        data_resp.decode("utf-8", errors="replace")
        if isinstance(data_resp, bytes)
        else str(data_resp)
    )
    # Stalwart (and most Postfix-alikes) echo something like
    # "2.0.0 Ok: queued as 1a2b3c4d" on a successful DATA command - that
    # queue id is the closest thing to a server-assigned message id this
    # protocol exposes. Fall back to the raw response text (still reported,
    # not hidden) when a server phrases it differently.
    match = re.search(r"queued as (\S+)", response_text)
    message_id = match.group(1) if match else response_text

    return {
        "sent": True,
        "message_id": message_id,
        "smtp_response": response_text,
        "to": to,
        "subject": subject,
        "attachments": [p.name for p in paths],
    }
