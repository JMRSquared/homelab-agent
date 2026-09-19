import os
import re
from typing import Any

import httpx

from agent.clients import EXEC_TIMEOUT, hostctl_get, hostctl_post, service_get
from agent.tools.base import tool

DOCKER_HOST = "http://10.0.0.165"
# The agent is LXC 104; LXC 101 runs the docker stacks and is the exec path
# the agent uses to recover a stuck service. Stopping or rebooting either
# strands the agent with no way back - `pct start 104` is only reachable
# through the agent itself. `start` stays allowed for both.
# hostctl/pve.py enforces the same restriction independently, since
# agent/tick.py calls tool functions directly and bypasses hostctl's schema
# validation for some paths.
#
# There is no equivalent set for VM 200 (mt5) any more - the owner decided
# the agent oversees the whole homelab, including the trading VM, with full
# administrative control and no approval prompts. The operational
# guardrails for it now live in the system prompts (agent/slack_app.py,
# agent/tick.py), not as a tool-level block: verify the result after any
# stop/reboot given what a forced MetaTrader restart costs, never place or
# modify a trade, no trading advice.
SELF_PROTECTED_GUESTS = {101, 104}
GUEST_ACTIONS = ("start", "stop", "reboot")
PROTECTED_ACTIONS = {"stop", "reboot"}
STACK_ACTIONS = ("up", "down", "restart", "pull")

NO_ARGS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

# A bare path segment: no "/", no leading "-" (which zfs/docker argv would read
# as a flag), no ".." traversal tricks.
_NAME_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$"
_NAME_RE = re.compile(_NAME_PATTERN)

# A ZFS dataset name under the `tank` pool: `tank` itself, or `tank/` plus
# one or more "/"-separated segments shaped like _NAME_PATTERN plus "." and
# ":" (both legal and common in ZFS dataset names). Anchored to `tank` so the
# model cannot snapshot an arbitrary pool - `hostctl/zfs.py` enforces the
# same restriction independently, since `agent/tick.py` calls tool functions
# directly and bypasses this schema.
_DATASET_SEGMENT = r"[a-zA-Z0-9][a-zA-Z0-9_.:-]*"
_DATASET_PATTERN = rf"^tank(/{_DATASET_SEGMENT})*$"
_DATASET_RE = re.compile(_DATASET_PATTERN)


@tool(
    "guests_list",
    "List every Proxmox guest (VMs and LXCs) with its current status. Use this to see "
    "what's running on the homelab before deciding whether to act on a guest.",
    NO_ARGS,
)
def guests_list() -> dict[str, Any]:
    return hostctl_get("/guests")


@tool(
    "guest_action",
    "Start, stop, or reboot a Proxmox guest by its numeric id. Use this to bring a "
    "service back up or cycle a misbehaving container/VM, including guest 200 (the "
    "trading VM, mt5) - you have full administrative control over it. Before "
    "stopping or rebooting 200 specifically, see your system prompt for what that "
    "costs MetaTrader and what to check immediately afterward. Guests 101 and 104 "
    "(the docker host and the agent's own container) can be started but never "
    "stopped or rebooted, to avoid stranding the agent.",
    {
        "type": "object",
        "properties": {
            "guest": {"type": "integer"},
            "action": {"type": "string", "enum": list(GUEST_ACTIONS)},
        },
        "required": ["guest", "action"],
        "additionalProperties": False,
    },
)
def guest_action(guest: int, action: str) -> dict[str, Any]:
    if guest in SELF_PROTECTED_GUESTS and action in PROTECTED_ACTIONS:
        raise PermissionError(
            f"guest {guest} hosts the agent itself or its recovery path "
            f"(docker/exec in LXC 101); {action} on it is blocked so the agent "
            "can't strand itself with no way to come back"
        )
    return hostctl_post(f"/guest/{guest}/action", {"action": action})


@tool(
    "zfs_report",
    "Report ZFS pool health, dataset usage, and free space for the tank pool. Use this "
    "to check storage headroom or diagnose a pool problem.",
    NO_ARGS,
)
def zfs_report() -> dict[str, Any]:
    return hostctl_get("/zfs/status")


@tool(
    "zfs_snapshot",
    "Take a ZFS snapshot of a dataset under tank, labelled for later reference. Use "
    "this before a risky change to leave a rollback point. This only creates "
    "snapshots; nothing in this system ever deletes one. Snapshotting the same "
    "dataset again within 6 hours is rejected regardless of label, and each "
    "dataset is capped at a small total number of snapshots - check zfs_report "
    "first if unsure whether one was already taken recently.",
    {
        "type": "object",
        "properties": {
            "dataset": {"type": "string", "pattern": _DATASET_PATTERN},
            "label": {"type": "string", "pattern": _NAME_PATTERN},
        },
        "required": ["dataset", "label"],
        "additionalProperties": False,
    },
)
def zfs_snapshot(dataset: str, label: str) -> dict[str, Any]:
    if not _DATASET_RE.match(dataset):
        raise ValueError(f"invalid dataset name: {dataset!r}")
    if not _NAME_RE.match(label):
        raise ValueError(f"invalid label: {label!r}")
    return hostctl_post("/zfs/snapshot", {"dataset": dataset, "label": label})


@tool(
    "host_metrics",
    "Report the Proxmox host's load average, memory use, ZFS ARC size, and uptime. Use "
    "this to check whether the host itself is under pressure.",
    NO_ARGS,
)
def host_metrics() -> dict[str, Any]:
    return hostctl_get("/host/metrics")


@tool(
    "docker_stacks",
    "List the Dockge-managed Docker Compose stacks running in LXC 101 and their real "
    "stack names. Stack names are not the same as container names - a container can "
    "run inside a differently-named stack (for example Jellyfin runs inside the "
    "'debrid' stack, not a 'jellyfin' stack). Always call this first to learn the "
    "real stack name before calling docker_action.",
    NO_ARGS,
)
def docker_stacks() -> dict[str, Any]:
    return hostctl_post(
        "/guest/101/exec",
        {"argv": ["docker", "compose", "ls", "--format", "json"]},
        timeout=EXEC_TIMEOUT,
    )


@tool(
    "docker_action",
    "Bring a Dockge stack in LXC 101 up, take it down, restart it, or pull new images "
    "for it, by stack name. Use this to recover a stuck service or apply an update. "
    "`stack` must be a real stack name as returned by docker_stacks, not a container "
    "or service name - call docker_stacks first if unsure.",
    {
        "type": "object",
        "properties": {
            "stack": {"type": "string", "pattern": _NAME_PATTERN},
            "action": {"type": "string", "enum": list(STACK_ACTIONS)},
        },
        "required": ["stack", "action"],
        "additionalProperties": False,
    },
)
def docker_action(stack: str, action: str) -> dict[str, Any]:
    if not _NAME_RE.match(stack):
        raise ValueError(f"invalid stack name: {stack!r}")
    verb = {"up": ["up", "-d"], "down": ["down"], "restart": ["restart"], "pull": ["pull"]}[action]
    try:
        return hostctl_post(
            "/guest/101/exec",
            {"argv": ["docker", "compose", "-f", f"/opt/stacks/{stack}/compose.yaml", *verb]},
            timeout=EXEC_TIMEOUT,
        )
    except httpx.HTTPStatusError as exc:
        raise ValueError(
            f"stack {stack!r} failed ({exc.response.status_code}): stack names come "
            "from docker_stacks and are not the same as container names - call "
            f"docker_stacks to confirm {stack!r} is a real stack. hostctl said: "
            f"{exc.response.text}"
        ) from exc


@tool(
    "monitors_status",
    "Report Uptime Kuma's monitor states for the homelab status page. Use this to see "
    "what Uptime Kuma currently considers up or down.",
    NO_ARGS,
)
def monitors_status() -> dict[str, Any]:
    slug = os.environ.get("UPTIME_KUMA_SLUG", "homelab")
    data = service_get(f"{DOCKER_HOST}:3001", f"/api/status-page/heartbeat/{slug}")
    if not data.get("heartbeatList"):
        # An empty heartbeatList reads as "everything is fine" unless it's
        # called out explicitly - it's just as likely to mean the status
        # page doesn't exist or has no monitors added to it, which the model
        # (and the human reading its report) must be able to tell apart from
        # a genuinely quiet homelab.
        return {
            "status_page_slug": slug,
            "empty": True,
            "note": (
                f"Uptime Kuma's status page {slug!r} returned no monitors. This is not "
                "proof of health - it can also mean the status page is missing or has no "
                "monitors added. Set UPTIME_KUMA_SLUG if the slug is wrong."
            ),
        }
    return data


@tool(
    "adguard_report",
    "Report AdGuard Home's DNS query and ad-blocking stats. Use this to check DNS "
    "filtering activity or whether AdGuard is answering queries at all.",
    NO_ARGS,
)
def adguard_report() -> dict[str, Any]:
    token = os.environ["ADGUARD_BASIC_AUTH"]
    return service_get(
        f"{DOCKER_HOST}:8080", "/control/stats", {"Authorization": f"Basic {token}"}
    )
