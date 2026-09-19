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
