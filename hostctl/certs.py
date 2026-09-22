"""TLS certificate expiry reporting.

Nothing on the network watches certificate expiry today - the mail server's
`mx.crt` in LXC 103 expires 2026-12-16 and an expired cert on a mail server
fails in a way that looks like "mail is a bit broken" rather than an obvious
outage. This module answers "what's the expiry situation" for a configurable
set of endpoints.

Deliberately connect-based, not a walk of known cert file paths on disk: the
task that motivated this module asked for whichever approach generalises
better, and a live TLS handshake checks what a client actually gets served -
including a mismatch between the file on disk and what the process actually
has loaded (a stale reload, a wrong SNI cert, a proxy in front) - rather than
what's merely present in a directory. The host running hostctl shares a LAN
with every guest, so connecting to `guest_ip:port` needs nothing beyond a
plain TCP+TLS handshake; no `pct exec` indirection required.

The set of targets is config, not code: `HOSTCTL_CERTS_CONFIG` (default
`/etc/hostctl/certs.json`) names a JSON file of `{"targets": [{"name",
"host", "port"}, ...]}`. Adding a new service to watch is an edit to that
file and a restart of hostctl - not a code change and redeploy.
"""

import datetime as dt
import json
import os
import socket
import ssl
from pathlib import Path
from typing import TypedDict

from cryptography import x509

DEFAULT_CONFIG_PATH = "/etc/hostctl/certs.json"
CONNECT_TIMEOUT_S = 5.0


class CertTarget(TypedDict):
    name: str
    host: str
    port: int


def _config_path() -> str:
    return os.environ.get("HOSTCTL_CERTS_CONFIG", DEFAULT_CONFIG_PATH)


def load_targets() -> list[CertTarget]:
    """Read and validate the configured target list.

    Missing file, unreadable file, or malformed JSON all resolve to an empty
    list rather than raising - a misconfigured or not-yet-created config
    file should make `/certs/status` report nothing to check, not 500 the
    route that's supposed to be raising alarms about other things being
    broken.
    """
    path = Path(_config_path())
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []

    entries = raw.get("targets") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []

    targets: list[CertTarget] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name, host, port = entry.get("name"), entry.get("host"), entry.get("port")
        if not isinstance(name, str) or not isinstance(host, str) or not isinstance(port, int):
            continue
        targets.append({"name": name, "host": host, "port": port})
    return targets


def _fetch_der_cert(host: str, port: int) -> bytes:
    """Open a TLS connection and return the peer certificate, DER-encoded.

    `CERT_NONE` is deliberate: this reports on whatever certificate a
    service presents, including self-signed or internally-issued certs that
    would otherwise fail chain validation on a homelab LAN with no public
    CA involved. Trust is not the question this module answers - expiry is.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls_sock:
            der = tls_sock.getpeercert(binary_form=True)
    if der is None:
        raise ValueError(f"{host}:{port} presented no certificate")
    return der


def _describe(der: bytes) -> dict[str, object]:
    cert = x509.load_der_x509_certificate(der)
    not_after = cert.not_valid_after_utc
    days_remaining = (not_after - dt.datetime.now(dt.UTC)).days
    return {
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "not_after": not_after.isoformat(),
        "days_remaining": days_remaining,
    }


def check_target(target: CertTarget) -> dict[str, object]:
    """Check one target. Never raises - a target that can't be reached or
    doesn't speak TLS is reported with `ok: false`, not dropped or allowed
    to fail the whole route."""
    result: dict[str, object] = {
        "name": target["name"],
        "host": target["host"],
        "port": target["port"],
    }
    try:
        der = _fetch_der_cert(target["host"], target["port"])
        result.update(ok=True, **_describe(der))
    except Exception as exc:  # noqa: BLE001 - any failure here is data, not a route error
        result.update(ok=False, error=str(exc))
    return result


def status() -> dict[str, object]:
    return {"certs": [check_target(t) for t in load_targets()]}
