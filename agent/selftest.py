"""A fast, safe-to-run-any-time reachability check across every tool that
talks to something outside this process.

Three capabilities were silently broken during earlier work on this agent
and were only found by hand: the audit trail posting to a channel that
didn't exist, `monitors_status` reading "everything healthy" off an empty
Uptime Kuma status page, a Proxmox tool calling a flag this version doesn't
have, a missing API key surfacing as a cryptic error deep in a stack trace.
Every one of those looked fine until someone went and checked. This module
is that check, made repeatable.

Each `Probe` exercises one external dependency (hostctl, ZFS, Uptime Kuma,
AdGuard, Jellyfin/Jellyseerr, Immich, mail, Slack, the MiniMax API, or a
local filesystem path) using the narrowest read-only call available, and is
tagged with every tool that depends on it. A tool with no read-only form of
its own (`guest_action`, `zfs_snapshot`, `send_email`, ...) is not called
directly - its *dependency* is probed instead (hostctl reachability for the
former two, SMTP auth without sending for the latter), per the design
brief: nothing here snapshots, restarts, sends mail, or places anything.

A probe's outcome is one of four states, not two - conflating "answered
empty" with "healthy" is the exact Uptime Kuma failure above:

- `ok`        - answered, and the data looks like data.
- `empty`     - answered, but with nothing in it. Suspicious, not a pass:
                surfaced the same as a failure so a human decides whether
                that's expected.
- `failed`    - the call raised: a timeout, an auth error, a malformed
                response, a 500.
- `unavailable` - the probe didn't run because required configuration (an
                API key, a password) isn't set. A configuration state, not
                a fault - reported separately so it doesn't read as broken.

Probes run concurrently (each is a blocking call with its own short
timeout already) so the whole suite's wall time is close to the single
slowest probe, not their sum - the "keep it fast enough to run often"
requirement.
"""

from __future__ import annotations

import datetime as dt
import enum
import os
import smtplib
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent import clients
from agent.brain import Brain
from agent.tools import infra, mail, media, photos
from agent.tools import outbox as outbox_mod

# Cap on how long a single detail string can be before it reaches the
# model/Slack - a probe's failure message (an HTTP body, an exception repr)
# can be long; the overall report must stay small regardless of how chatty
# any one dependency's error text is.
_DETAIL_CAP = 400

_MAX_WORKERS = 8


class ProbeStatus(enum.StrEnum):
    OK = "ok"
    EMPTY = "empty"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class Unavailable(Exception):
    """Raise from inside a probe body when the reason it can't run is
    missing configuration - an unset env var, a missing API key - rather
    than a real fault. Caught by `run_probe` and reported as
    `ProbeStatus.UNAVAILABLE`, kept out of the failure count."""


@dataclass(frozen=True)
class Probe:
    name: str
    tools: tuple[str, ...]
    fn: Callable[[], tuple[ProbeStatus, str]]


@dataclass(frozen=True)
class ProbeResult:
    probe: str
    tools: tuple[str, ...]
    status: ProbeStatus
    detail: str
    latency_ms: int


def _cap(text: str) -> str:
    text = text.strip()
    if len(text) <= _DETAIL_CAP:
        return text
    return text[:_DETAIL_CAP] + "... (truncated)"


def run_probe(probe: Probe) -> ProbeResult:
    start = time.monotonic()
    try:
        status, detail = probe.fn()
    except Unavailable as exc:
        status, detail = ProbeStatus.UNAVAILABLE, str(exc)
    except Exception as exc:  # a probe's own failure must never crash the suite
        status, detail = ProbeStatus.FAILED, f"{type(exc).__name__}: {exc}"
    latency_ms = int((time.monotonic() - start) * 1000)
    return ProbeResult(
        probe=probe.name,
        tools=probe.tools,
        status=status,
        detail=_cap(detail),
        latency_ms=latency_ms,
    )


def _require_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise Unavailable(f"{', '.join(missing)} not set")


# --- individual probes -----------------------------------------------------


def _probe_hostctl_guests() -> tuple[ProbeStatus, str]:
    data = infra.guests_list()
    guests = data.get("guests")
    if not guests:
        return ProbeStatus.EMPTY, "hostctl answered but listed no guests"
    return ProbeStatus.OK, f"{len(guests)} guests reported"


def _probe_zfs_report() -> tuple[ProbeStatus, str]:
    data = infra.zfs_report()
    if not data.get("datasets") and not data.get("pool_status"):
        return ProbeStatus.EMPTY, "zfs status answered but reported no pool or datasets"
    return ProbeStatus.OK, "pool status and dataset usage reported"


def _probe_host_metrics() -> tuple[ProbeStatus, str]:
    data = infra.host_metrics()
    if not data:
        return ProbeStatus.EMPTY, "host metrics answered with an empty body"
    return ProbeStatus.OK, "host metrics reported"


def _probe_mt5_status() -> tuple[ProbeStatus, str]:
    data = infra.mt5_status()
    if not data:
        return ProbeStatus.EMPTY, "mt5 status answered with an empty body"
    return ProbeStatus.OK, "mt5 account state reported"


def _probe_guest_200() -> tuple[ProbeStatus, str]:
    data = infra.guests_list()
    guests = data.get("guests") or []
    guest200 = next((g for g in guests if g.get("id") == 200), None)
    if guest200 is None:
        return ProbeStatus.EMPTY, "guest 200 (mt5) not present in the guest list"
    status = guest200.get("status")
    if status != "running":
        return ProbeStatus.EMPTY, f"guest 200 (mt5) is not running (status={status!r})"
    return ProbeStatus.OK, "guest 200 (mt5) is running"


def _probe_docker_stacks() -> tuple[ProbeStatus, str]:
    data = infra.docker_stacks()
    stdout = str(data.get("stdout") or "").strip()
    if not stdout:
        return ProbeStatus.EMPTY, "docker compose ls answered with no stacks listed"
    return ProbeStatus.OK, "docker stacks listed"


def _probe_monitors() -> tuple[ProbeStatus, str]:
    data = infra.monitors_status()
    if data.get("empty"):
        return ProbeStatus.EMPTY, str(
            data.get("note") or "Uptime Kuma status page has no monitors"
        )
    return ProbeStatus.OK, "Uptime Kuma monitor states reported"


def _probe_adguard() -> tuple[ProbeStatus, str]:
    _require_env("ADGUARD_BASIC_AUTH")
    data = infra.adguard_report()
    if not data:
        return ProbeStatus.EMPTY, "AdGuard answered with an empty body"
    return ProbeStatus.OK, "AdGuard DNS stats reported"


def _probe_media() -> tuple[ProbeStatus, str]:
    _require_env("JELLYFIN_KEY", "JELLYSEERR_KEY")
    data = media.media_library_status()
    counts = data.get("jellyfin") if isinstance(data, dict) else None
    total = sum(v for v in (counts or {}).values() if isinstance(v, int))
    if not data or total == 0:
        return ProbeStatus.EMPTY, "Jellyfin/Jellyseerr answered but reported zero library items"
    return ProbeStatus.OK, "Jellyfin/Jellyseerr library status reported"


def _probe_photos() -> tuple[ProbeStatus, str]:
    _require_env("IMMICH_KEY")
    data = photos.photos_stats()
    if not data:
        return ProbeStatus.EMPTY, "Immich answered with an empty body"
    return ProbeStatus.OK, "Immich photo/video stats reported"


def _probe_mail_imap() -> tuple[ProbeStatus, str]:
    _require_env("MAIL_PASSWORD")
    data = mail.mail_list_messages(limit=1)
    if not data.get("total"):
        return ProbeStatus.EMPTY, "inbox reachable but reports zero messages"
    return ProbeStatus.OK, f"inbox reachable, {data['total']} messages"


def _probe_mail_smtp() -> tuple[ProbeStatus, str]:
    _require_env("MAIL_PASSWORD")
    password = os.environ["MAIL_PASSWORD"]
    host = os.environ.get("MAIL_SMTP_HOST", mail.DEFAULT_SMTP_HOST)
    port = int(os.environ.get("MAIL_SMTP_PORT", str(mail.DEFAULT_SMTP_PORT)))
    sender = os.environ.get("MAIL_FROM", mail.DEFAULT_FROM)
    with smtplib.SMTP_SSL(host, port, timeout=mail.SMTP_TIMEOUT) as smtp:
        smtp.login(sender, password)
    return ProbeStatus.OK, f"authenticated to {host}:{port} without sending anything"


def _probe_slack() -> tuple[ProbeStatus, str]:
    _require_env("SLACK_BOT_TOKEN")
    from agent.tools import comms

    channel = os.environ.get("SLACK_CHANNEL_LOG", "#homelab-agent-log")
    data = comms.slack_history(channel, limit=1)
    if not data.get("messages"):
        return ProbeStatus.EMPTY, f"{channel} reachable but has no recent messages"
    return ProbeStatus.OK, f"{channel} reachable"


def _probe_minimax() -> tuple[ProbeStatus, str]:
    client = clients.minimax_client()
    models = client.models.list()
    data = list(getattr(models, "data", None) or [])
    if not data:
        return ProbeStatus.EMPTY, "MiniMax API answered but listed no models"
    return ProbeStatus.OK, f"{len(data)} models listed"


def _probe_brain() -> tuple[ProbeStatus, str]:
    path = os.environ.get("AGENT_BRAIN", "/tank/dev/agent/brain.md")
    topics = Brain(path).topics()
    if not topics:
        return ProbeStatus.EMPTY, "brain file readable but has no topics yet"
    return ProbeStatus.OK, f"{len(topics)} topics on file"


def _probe_outbox() -> tuple[ProbeStatus, str]:
    directory = outbox_mod.outbox_dir()
    if not os.access(directory, os.W_OK):
        return ProbeStatus.FAILED, f"{directory} exists but is not writable"
    return ProbeStatus.OK, f"{directory} exists and is writable"


def _probe_household_lists() -> tuple[ProbeStatus, str]:
    directory = Path(os.environ.get("AGENT_LISTS", "/tank/dev/agent/lists"))
    directory.mkdir(parents=True, exist_ok=True)
    if not os.access(directory, os.W_OK):
        return ProbeStatus.FAILED, f"{directory} exists but is not writable"
    return ProbeStatus.OK, f"{directory} exists and is writable"


def default_probes() -> list[Probe]:
    return [
        Probe(
            "hostctl",
            (
                "guests_list", "guest_action", "guest_exec", "host_exec",
                "self_deploy", "hostctl_restart_verified",
            ),
            _probe_hostctl_guests,
        ),
        Probe("zfs", ("zfs_report", "zfs_snapshot"), _probe_zfs_report),
        Probe("host_metrics", ("host_metrics",), _probe_host_metrics),
        Probe("mt5_status", ("mt5_status",), _probe_mt5_status),
        Probe("guest_200", ("mt5_screenshot",), _probe_guest_200),
        Probe("docker", ("docker_stacks", "docker_action"), _probe_docker_stacks),
        Probe("uptime_kuma", ("monitors_status",), _probe_monitors),
        Probe("adguard", ("adguard_report",), _probe_adguard),
        Probe(
            "jellyfin_jellyseerr",
            ("media_search", "media_request", "media_last_watched", "media_library_status"),
            _probe_media,
        ),
        Probe("immich", ("photos_search", "photos_download", "photos_stats"), _probe_photos),
        Probe("mail_imap", ("mail_list_messages", "mail_read_message"), _probe_mail_imap),
        Probe("mail_smtp", ("send_email",), _probe_mail_smtp),
        Probe("slack", ("slack_say", "slack_history", "slack_thread_replies"), _probe_slack),
        Probe("minimax", ("image_inspect",), _probe_minimax),
        Probe(
            "brain",
            ("brain_read", "brain_write", "brain_list", "brain_consolidate"),
            _probe_brain,
        ),
        Probe("outbox", (), _probe_outbox),
        Probe("household_lists", ("notes_append",), _probe_household_lists),
    ]


def run_self_test(probes: Sequence[Probe] | None = None) -> dict[str, Any]:
    probe_list = list(probes) if probes is not None else default_probes()
    if not probe_list:
        results: list[ProbeResult] = []
    else:
        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(probe_list))) as pool:
            results = list(pool.map(run_probe, probe_list))
    results.sort(key=lambda r: r.probe)

    summary = {status.value: 0 for status in ProbeStatus}
    for r in results:
        summary[r.status.value] += 1

    return {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "summary": summary,
        "results": [
            {
                "probe": r.probe,
                "tools": list(r.tools),
                "status": r.status.value,
                "detail": r.detail,
                "latency_ms": r.latency_ms,
            }
            for r in results
        ],
    }
