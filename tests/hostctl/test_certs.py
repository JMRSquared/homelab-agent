import datetime as dt
import json
import socket
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from hostctl import certs
from hostctl.app import app

AUTH = {"Authorization": "Bearer testtoken"}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("HOSTCTL_TOKEN", "testtoken")


def _self_signed_pem(*, not_after: dt.datetime) -> tuple[bytes, bytes]:
    """Build a throwaway self-signed cert/key pair with a chosen expiry, so
    tests can assert on `days_remaining` without depending on any real
    certificate's lifetime."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test.internal")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.UTC) - dt.timedelta(days=1))
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


@pytest.fixture()
def tls_server(tmp_path):
    """A real, minimal TLS server on 127.0.0.1 presenting a cert that
    expires in 30 days - so `certs.check_target` is exercised against an
    actual handshake, not a mocked socket."""
    cert_pem, key_pem = _self_signed_pem(
        not_after=dt.datetime.now(dt.UTC) + dt.timedelta(days=30)
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(key_pem)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def _serve() -> None:
        listener.settimeout(0.5)
        while not stop.is_set():
            try:
                raw, _ = listener.accept()
            except TimeoutError:
                continue
            try:
                with ctx.wrap_socket(raw, server_side=True) as tls:
                    tls.recv(1)
            except (ssl.SSLError, OSError):
                pass

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", port
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=2)


def test_check_target_reports_subject_issuer_and_days_remaining(tls_server):
    host, port = tls_server
    result = certs.check_target({"name": "test-svc", "host": host, "port": port})
    assert result["ok"] is True
    assert result["name"] == "test-svc"
    assert "test.internal" in result["subject"]
    assert "test.internal" in result["issuer"]
    # Built with a 30 day lifetime above; allow a day of slack for test runtime.
    assert 28 <= result["days_remaining"] <= 30


def test_check_target_unreachable_port_reports_ok_false():
    result = certs.check_target({"name": "down", "host": "127.0.0.1", "port": 1})
    assert result["ok"] is False
    assert "error" in result
    assert result["name"] == "down"


def test_load_targets_missing_config_is_empty_list(monkeypatch, tmp_path):
    monkeypatch.setenv("HOSTCTL_CERTS_CONFIG", str(tmp_path / "does-not-exist.json"))
    assert certs.load_targets() == []


def test_load_targets_malformed_json_is_empty_list(monkeypatch, tmp_path):
    path = tmp_path / "certs.json"
    path.write_text("{not json")
    monkeypatch.setenv("HOSTCTL_CERTS_CONFIG", str(path))
    assert certs.load_targets() == []


def test_load_targets_reads_and_validates_entries(monkeypatch, tmp_path):
    path = tmp_path / "certs.json"
    path.write_text(
        json.dumps(
            {
                "targets": [
                    {"name": "mail-smtp", "host": "10.0.0.167", "port": 465},
                    {"name": "missing-port", "host": "10.0.0.167"},
                    "not-a-dict",
                ]
            }
        )
    )
    monkeypatch.setenv("HOSTCTL_CERTS_CONFIG", str(path))
    assert certs.load_targets() == [{"name": "mail-smtp", "host": "10.0.0.167", "port": 465}]


def test_certs_status_route_is_configurable_and_authenticated(monkeypatch, tls_server, tmp_path):
    host, port = tls_server
    path = tmp_path / "certs.json"
    path.write_text(json.dumps({"targets": [{"name": "test-svc", "host": host, "port": port}]}))
    monkeypatch.setenv("HOSTCTL_CERTS_CONFIG", str(path))

    client = TestClient(app)
    assert client.get("/certs/status").status_code == 401

    r = client.get("/certs/status", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["certs"][0]["name"] == "test-svc"
    assert body["certs"][0]["ok"] is True
