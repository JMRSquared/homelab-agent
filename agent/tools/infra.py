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
# administrative control and no approval prompts. The operational knowledge
# for it (what a stop/reboot costs, and to verify the result afterward)
# lives in the system prompt (agent/prompts.py), not as a tool-level block.
#
# guest_exec below reaches every guest, including 101/104, with no command
# allowlist - SELF_PROTECTED_GUESTS only gates guest_action's stop/reboot.
# A command run via guest_exec on 101 or 104 could still strand the agent.
SELF_PROTECTED_GUESTS = {101, 104}
GUEST_ACTIONS = ("start", "stop", "reboot")
PROTECTED_ACTIONS = {"stop", "reboot"}
STACK_ACTIONS = ("up", "down", "restart", "pull")

# Per-stream cap on guest_exec output reaching the model. A command dumping
# megabytes into the context is a real failure mode (a full mail spool, a
# verbose log dump); this caps each stream and reports how much was cut
# rather than silently truncating or blowing the prompt budget.
_EXEC_OUTPUT_LIMIT = 4000

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


def _capped(text: str) -> tuple[str, bool, int]:
    total = len(text)
    if total <= _EXEC_OUTPUT_LIMIT:
        return text, False, total
    return text[:_EXEC_OUTPUT_LIMIT], True, total


@tool(
    "guest_exec",
    "Run a shell command as root on any guest - LXC or the Windows trading VM "
    "(200/mt5) - the general-purpose way to reach anything the narrower tools above "
    "don't cover: read mail inside the mail container, inspect Jellyfin's config, "
    "read MetaTrader's files, check a process, anything. No command allowlist. "
    "`command` is a single shell one-liner in the syntax native to that guest's OS: "
    "on an LXC it runs under `sh -c` (POSIX, e.g. "
    "`grep -ril trading /var/mail/* | head -5` or `cat /var/mail/someone`); on guest "
    "200 it runs under `cmd.exe /c` (Windows, e.g. `dir C:\\Users\\trader\\Desktop` "
    "or `type C:\\path\\to\\a\\file.csv`). Output is capped per stream and says when "
    "it was cut - ask for a narrower command (grep/tail/head, or Select-Object on "
    "Windows) if you need less than the full output.",
    {
        "type": "object",
        "properties": {
            "guest": {"type": "integer"},
            "command": {"type": "string", "minLength": 1},
        },
        "required": ["guest", "command"],
        "additionalProperties": False,
    },
)
def guest_exec(guest: int, command: str) -> dict[str, Any]:
    result = hostctl_post(f"/guest/{guest}/shell", {"command": command}, timeout=EXEC_TIMEOUT)
    stdout, stdout_truncated, stdout_total = _capped(str(result.get("stdout") or ""))
    stderr, stderr_truncated, stderr_total = _capped(str(result.get("stderr") or ""))
    return {
        "guest": guest,
        "exitcode": result.get("exitcode"),
        "stdout": stdout,
        "stdout_truncated": stdout_truncated,
        "stdout_total_chars": stdout_total,
        "stderr": stderr,
        "stderr_truncated": stderr_truncated,
        "stderr_total_chars": stderr_total,
    }


@tool(
    "host_exec",
    "Run a shell command as root directly on the Proxmox host `tech` (10.0.0.2) "
    "itself - not a guest. Use this when what you're after lives on the host, not "
    "inside any LXC or VM: for example /opt/homelab (the legacy TazzieBot Slack "
    "bot and its tazzie-status.py cron job), /etc/cron.d, /etc/homelab/channels.conf, "
    "or anything under /tank directly (see your brain for what's there). Use "
    "guest_exec instead when the target is inside a specific guest (101/104/200/etc) "
    "- 'run a command on the homelab' can mean either, so pick deliberately rather "
    "than guessing. `command` is a POSIX shell one-liner run under `sh -c` on the "
    "host's own Debian shell. No command allowlist - full root, same as guest_exec. "
    "One thing to know before you use it: hostctl (the only path this agent has to "
    "the host or any guest) runs as a process on this same host, so a command here "
    "can restart or kill hostctl itself, and can reach every guest's data on /tank "
    "directly without going through the guest at all. That's not a reason to hold "
    "back - the owner asked for full access - just know what you're holding. Output "
    "is capped per stream and says when it was cut.",
    {
        "type": "object",
        "properties": {"command": {"type": "string", "minLength": 1}},
        "required": ["command"],
        "additionalProperties": False,
    },
)
def host_exec(command: str) -> dict[str, Any]:
    result = hostctl_post("/host/exec", {"command": command}, timeout=EXEC_TIMEOUT)
    stdout, stdout_truncated, stdout_total = _capped(str(result.get("stdout") or ""))
    stderr, stderr_truncated, stderr_total = _capped(str(result.get("stderr") or ""))
    return {
        "exitcode": result.get("exitcode"),
        "stdout": stdout,
        "stdout_truncated": stdout_truncated,
        "stdout_total_chars": stdout_total,
        "stderr": stderr,
        "stderr_truncated": stderr_truncated,
        "stderr_total_chars": stderr_total,
    }


@tool(
    "mt5_status",
    "Report VM 200 (mt5)'s account state - equity, balance, open positions - from "
    "the TazzieMoney EA's own on-disk heartbeat/export on the Proxmox host, without "
    "touching the VM. Use this first for 'how many positions are open' or 'what's "
    "my balance' - it's faster and safer than driving the terminal via guest_exec. "
    "Two sources feed this and can go stale or disagree independently - check "
    "age_s/hb_age_s in the result and say plainly if the data looks stale rather "
    "than reporting a number with false confidence. Fall back to guest_exec on "
    "guest 200 only if this doesn't answer the question.",
    NO_ARGS,
)
def mt5_status() -> dict[str, Any]:
    return hostctl_get("/mt5/status")


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
