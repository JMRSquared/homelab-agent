import os
from typing import Any

from agent.clients import hostctl_get, hostctl_post, service_get
from agent.tools.base import tool

DOCKER_HOST = "http://10.0.0.165"
BLOCKED_GUESTS = {200}
GUEST_ACTIONS = ("start", "stop", "reboot")
STACK_ACTIONS = ("up", "down", "restart", "pull")

NO_ARGS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}


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
    "service back up or cycle a misbehaving container/VM. Guest 200 (the trading VM) "
    "is permanently off limits and will always be rejected.",
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
    if guest in BLOCKED_GUESTS:
        raise PermissionError(f"guest {guest} is out of scope")
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
    "Take a ZFS snapshot of a dataset, labelled for later reference. Use this before a "
    "risky change to leave a rollback point. This only creates snapshots; nothing in "
    "this system ever deletes one.",
    {
        "type": "object",
        "properties": {"dataset": {"type": "string"}, "label": {"type": "string"}},
        "required": ["dataset", "label"],
        "additionalProperties": False,
    },
)
def zfs_snapshot(dataset: str, label: str) -> dict[str, Any]:
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
    "List the Dockge-managed Docker Compose stacks running in LXC 101 and their state. "
    "Use this to see which services are up before restarting or pulling one.",
    NO_ARGS,
)
def docker_stacks() -> dict[str, Any]:
    return hostctl_post(
        "/guest/101/exec", {"argv": ["docker", "compose", "ls", "--format", "json"]}
    )


@tool(
    "docker_action",
    "Bring a Dockge stack in LXC 101 up, take it down, restart it, or pull new images "
    "for it, by stack name. Use this to recover a stuck service or apply an update.",
    {
        "type": "object",
        "properties": {
            "stack": {"type": "string"},
            "action": {"type": "string", "enum": list(STACK_ACTIONS)},
        },
        "required": ["stack", "action"],
        "additionalProperties": False,
    },
)
def docker_action(stack: str, action: str) -> dict[str, Any]:
    verb = {"up": ["up", "-d"], "down": ["down"], "restart": ["restart"], "pull": ["pull"]}[action]
    return hostctl_post(
        "/guest/101/exec",
        {"argv": ["docker", "compose", "-f", f"/opt/stacks/{stack}/compose.yaml", *verb]},
    )


@tool(
    "monitors_status",
    "Report Uptime Kuma's monitor states for the homelab status page. Use this to see "
    "what Uptime Kuma currently considers up or down.",
    NO_ARGS,
)
def monitors_status() -> dict[str, Any]:
    return service_get(
        f"{DOCKER_HOST}:3001",
        "/api/status-page/heartbeat/homelab",
    )


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
