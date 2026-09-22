import email
import email.message
import imaplib
import smtplib

import pytest

from agent.tools import base, mail  # noqa: F401


class _FakeSMTPSSL:
    instances: list["_FakeSMTPSSL"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.logged_in = None
        self.mail_from = None
        self.rcpt_to = None
        self.data_sent = None
        self.mail_code = 250
        self.rcpt_code = 250
        self.data_code = 250
        self.data_response = b"2.0.0 Ok: queued as 4f1a2b3c"
        _FakeSMTPSSL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.logged_in = (user, password)

    def mail(self, sender):
        self.mail_from = sender
        return (self.mail_code, b"ok")

    def rcpt(self, to):
        self.rcpt_to = to
        return (self.rcpt_code, b"ok")

    def data(self, msg_bytes):
        self.data_sent = msg_bytes
        return (self.data_code, self.data_response)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("MAIL_PASSWORD", "secret-pass")
    _FakeSMTPSSL.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTPSSL)


def _make_attachment(tmp_path, monkeypatch, name="photo.jpg", content=b"\xff\xd8\xffjpegdata"):
    from agent.tools import outbox

    outbox_dir = outbox.outbox_dir()
    path = outbox_dir / name
    path.write_bytes(content)
    return str(path)


def test_send_email_happy_path_returns_server_message_id(tmp_path, monkeypatch):
    attachment = _make_attachment(tmp_path, monkeypatch)
    out = base.dispatch(
        "send_email",
        {
            "to": "mulavhe@gmail.com",
            "subject": "Audi + Mutiny",
            "body": "See attached.",
            "attachments": [attachment],
        },
    )
    assert out["ok"] is True
    result = out["result"]
    assert result["message_id"] == "4f1a2b3c"
    assert result["to"] == "mulavhe@gmail.com"
    assert result["attachments"] == ["photo.jpg"]

    sent = _FakeSMTPSSL.instances[0]
    assert sent.logged_in == ("admin@mail.jmrsquared.com", "secret-pass")
    assert sent.mail_from == "admin@mail.jmrsquared.com"
    assert sent.rcpt_to == "mulavhe@gmail.com"
    assert b"photo.jpg" in sent.data_sent


def test_send_email_uses_configured_host_and_port(tmp_path, monkeypatch):
    monkeypatch.setenv("MAIL_SMTP_HOST", "mail.example.internal")
    monkeypatch.setenv("MAIL_SMTP_PORT", "465")
    attachment = _make_attachment(tmp_path, monkeypatch)
    base.dispatch(
        "send_email",
        {"to": "a@b.com", "subject": "s", "body": "b", "attachments": [attachment]},
    )
    sent = _FakeSMTPSSL.instances[0]
    assert sent.host == "mail.example.internal"
    assert sent.port == 465


def test_send_email_without_password_fails_with_clear_error(monkeypatch):
    monkeypatch.delenv("MAIL_PASSWORD", raising=False)
    out = base.dispatch(
        "send_email", {"to": "a@b.com", "subject": "s", "body": "b", "attachments": []}
    )
    assert out["ok"] is False
    assert "MAIL_PASSWORD" in out["error"]


def test_send_email_rejects_attachment_outside_outbox(tmp_path, monkeypatch):
    outside = tmp_path / "not_the_outbox.jpg"
    outside.write_bytes(b"data")
    out = base.dispatch(
        "send_email",
        {"to": "a@b.com", "subject": "s", "body": "b", "attachments": [str(outside)]},
    )
    assert out["ok"] is False
    assert "outbox" in out["error"]
    assert _FakeSMTPSSL.instances == []


def test_send_email_rejects_missing_attachment(monkeypatch):
    missing = "/tank/dev/agent/outbox/nope.jpg"
    out = base.dispatch(
        "send_email",
        {"to": "a@b.com", "subject": "s", "body": "b", "attachments": [missing]},
    )
    assert out["ok"] is False


def test_send_email_rejects_bad_address(tmp_path, monkeypatch):
    attachment = _make_attachment(tmp_path, monkeypatch)
    out = base.dispatch(
        "send_email",
        {"to": "not-an-email", "subject": "s", "body": "b", "attachments": [attachment]},
    )
    assert out["ok"] is False


def test_send_email_rejects_attachments_over_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(mail, "MAX_ATTACHMENTS_BYTES", 10)
    attachment = _make_attachment(tmp_path, monkeypatch, content=b"x" * 100)
    out = base.dispatch(
        "send_email",
        {"to": "a@b.com", "subject": "s", "body": "b", "attachments": [attachment]},
    )
    assert out["ok"] is False
    assert "cap" in out["error"]


def test_send_email_direct_call_raises_on_bad_address(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        mail.send_email("nope", "s", "b", [])


# --- mail_list_messages / mail_read_message -------------------------------


class _FakeIMAPMessage:
    def __init__(self, uid: bytes, flags: bytes, header: bytes, full: bytes):
        self.uid = uid
        self.flags = flags
        self.header = header
        self.full = full


class _FakeIMAP4SSL:
    instances: list["_FakeIMAP4SSL"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.logged_in = None
        self.logged_out = False
        self.selected = None
        self.messages: list[_FakeIMAPMessage] = []
        self.login_error: Exception | None = None
        _FakeIMAP4SSL.instances.append(self)

    def login(self, user, password):
        if self.login_error:
            raise self.login_error
        self.logged_in = (user, password)

    def select(self, mailbox, readonly=False):
        self.selected = (mailbox, readonly)
        return ("OK", [b"1"])

    def search(self, charset, criterion):
        return ("OK", [b" ".join(m.uid for m in self.messages)])

    def fetch(self, uid, parts):
        # Real imaplib.IMAP4.fetch takes a str message set; search() above
        # still hands back bytes uids (matching real imaplib), so compare
        # decoded.
        for m in self.messages:
            if m.uid.decode() == uid:
                if "BODY.PEEK[HEADER" in parts:
                    return ("OK", [(m.flags, m.header)])
                return ("OK", [(m.flags, m.full)])
        return ("OK", [None])

    def logout(self):
        self.logged_out = True


class _FakeIMAP4Error(Exception):
    pass


@pytest.fixture(autouse=True)
def _imap_env(monkeypatch):
    monkeypatch.setenv("MAIL_PASSWORD", "secret-pass")
    _FakeIMAP4SSL.instances.clear()
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _FakeIMAP4SSL)
    monkeypatch.setattr(imaplib.IMAP4, "error", _FakeIMAP4Error, raising=False)


def _add_message(imap, uid, *, seen=True, sender="a@b.com", subject="Hi", body="hello there"):
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Date"] = "Mon, 1 Jan 2024 00:00:00 +0000"
    msg.set_content(body)
    full = msg.as_bytes()
    header = email.message_from_bytes(full)
    header_only = email.message.Message()
    for k in ("From", "Subject", "Date"):
        header_only[k] = header[k]
    flags = b"1 (FLAGS (\\Seen))" if seen else b"1 (FLAGS ())"
    imap.messages.append(
        _FakeIMAPMessage(str(uid).encode(), flags, header_only.as_bytes(), full)
    )


def test_mail_list_messages_returns_newest_first_with_read_state(monkeypatch):
    imap = _FakeIMAP4SSL("h", 993)

    def fake_ssl(host, port, timeout=None):
        return imap

    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake_ssl)
    _add_message(imap, 1, seen=True, subject="first")
    _add_message(imap, 2, seen=False, subject="second")
    out = base.dispatch("mail_list_messages", {})
    assert out["ok"] is True
    result = out["result"]
    assert [m["subject"] for m in result["messages"]] == ["second", "first"]
    assert result["messages"][0]["unread"] is True
    assert result["messages"][1]["unread"] is False
    assert imap.selected == ("INBOX", True)
    assert imap.logged_out is True


def test_mail_list_messages_empty_mailbox_returns_empty_list(monkeypatch):
    imap = _FakeIMAP4SSL("h", 993)

    def fake_ssl(host, port, timeout=None):
        return imap

    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake_ssl)
    out = base.dispatch("mail_list_messages", {})
    assert out["ok"] is True
    assert out["result"] == {"messages": [], "total": 0}


def test_mail_list_messages_bad_credentials_fails_cleanly(monkeypatch):
    imap = _FakeIMAP4SSL("h", 993)
    imap.login_error = _FakeIMAP4Error("AUTHENTICATIONFAILED")

    def fake_ssl(host, port, timeout=None):
        return imap

    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake_ssl)
    out = base.dispatch("mail_list_messages", {})
    assert out["ok"] is False
    assert "IMAP login" in out["error"]


def test_mail_list_messages_without_password_fails_with_clear_error(monkeypatch):
    monkeypatch.delenv("MAIL_PASSWORD", raising=False)
    out = base.dispatch("mail_list_messages", {})
    assert out["ok"] is False
    assert "MAIL_PASSWORD" in out["error"]


def test_mail_read_message_returns_body(monkeypatch):
    imap = _FakeIMAP4SSL("h", 993)

    def fake_ssl(host, port, timeout=None):
        return imap

    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake_ssl)
    _add_message(imap, 42, body="the full body text")
    out = base.dispatch("mail_read_message", {"uid": "42"})
    assert out["ok"] is True
    result = out["result"]
    assert result["body"].strip() == "the full body text"
    assert result["truncated"] is False
    assert result["from"] == "a@b.com"


def test_mail_read_message_caps_oversized_body(monkeypatch):
    imap = _FakeIMAP4SSL("h", 993)

    def fake_ssl(host, port, timeout=None):
        return imap

    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake_ssl)
    _add_message(imap, 7, body="x" * (mail._MAX_BODY_CHARS + 500))
    out = base.dispatch("mail_read_message", {"uid": "7"})
    assert out["ok"] is True
    result = out["result"]
    assert len(result["body"]) == mail._MAX_BODY_CHARS
    assert result["truncated"] is True


def test_mail_read_message_unknown_uid_fails_cleanly(monkeypatch):
    imap = _FakeIMAP4SSL("h", 993)

    def fake_ssl(host, port, timeout=None):
        return imap

    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake_ssl)
    out = base.dispatch("mail_read_message", {"uid": "999"})
    assert out["ok"] is False
    assert "999" in out["error"]
