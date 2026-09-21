#!/usr/bin/env python3
"""Seed the agent's brain with facts about the homelab it lives in.

Run this once, inside LXC 104, after deploying:

    pct enter 104
    cd /opt/homelab-agent
    python deploy/seed_brain.py

It writes one Brain topic per fact below via the same `Brain.write` the
`brain_write` tool uses, so a redeploy that re-runs this script simply
overwrites each topic with the same content - safe to run more than once.

Why this exists as a script rather than the model writing these itself: the
model can only learn what host_exec and slack_history show it *after* it
already knows to look - these entries are what tell it to look there in the
first place. Without them, "update that report" has no path from the
words in a Slack message to /opt/homelab/tazzie-status.py.
"""

from __future__ import annotations

import os

from agent.brain import Brain

TOPICS: dict[str, str] = {
    "legacy homelab bot opt-homelab": (
        "The Proxmox host `tech` (10.0.0.2) runs a separate, older Slack bot at "
        "/opt/homelab/ - a different Slack app from this agent, not this agent, and "
        "this agent does not control it by default. It runs as two systemd services "
        "on the host: slackbot.service (/opt/homelab/slackbot.py) and "
        "faceswap-worker.service. It answers regex commands in Slack: search, watch, "
        "chart, digest, faceswap, help, media, panic, scan, silence, space, status, "
        "tazzie, unpanic, voice, voices. Because it is a separate Slack app, this "
        "agent cannot see or change its behavior through Slack at all - only "
        "host_exec on 10.0.0.2 can read or edit its files, or restart/stop its "
        "systemd units (systemctl restart slackbot / faceswap-worker). Never edit "
        "anything under /opt/homelab as part of routine work - that is the owner's "
        "legacy bot, and any change there is deliberate, not incidental to another task."
    ),
    "tazzie hourly status report": (
        "The hourly MT5 status card posted to #homelab-mt5 comes from "
        "/opt/homelab/tazzie-status.py on the Proxmox host (10.0.0.2), NOT from this "
        "agent - this agent has never posted that card. It is scheduled by cron, not "
        "a systemd timer: /etc/cron.d/homelab-tazzie-status runs "
        "`0 * * * * root flock -n /run/tazzie-status.lock "
        "/opt/homelab/venv/bin/python /opt/homelab/tazzie-status.py "
        ">>/tank/dev/tazzie-metrics/status.log 2>&1` - hourly, on the hour. Changing "
        "how often it posts, or switching it to only post on change, means editing "
        "that cron line (host_exec: cat/edit /etc/cron.d/homelab-tazzie-status) or "
        "adding change-detection logic inside tazzie-status.py itself - there is no "
        "config flag for this today. Read both files with host_exec before proposing "
        "a change, since this file has not been inspected as part of writing this note."
    ),
    "legacy bot channels conf": (
        "/etc/homelab/channels.conf on the Proxmox host (10.0.0.2) maps each legacy "
        "/opt/homelab service to the Slack channel it posts to - for example "
        "mt5=#homelab-mt5. Read or edit it with host_exec "
        "(cat /etc/homelab/channels.conf) when a question is about which service "
        "posts where in the legacy bot, or when redirecting one of its reports to a "
        "different channel."
    ),
    "tazzie data on tank": (
        "TazzieBot's own exports live under /tank/dev/ on the Proxmox host: "
        "tazzie-daily/ (daily summaries), tazzie-metrics/ (status.log is where the "
        "hourly tazzie-status.py cron job (see the 'tazzie hourly status report' "
        "topic) appends its own run log - check this "
        "first to see exactly when it last ran and what it posted), and "
        "tazzie-alerts/. All three are host_exec territory (`ls`/`cat` on "
        "10.0.0.2), not reachable through any guest."
    ),
    "where this agent runs vs where hostctl runs": (
        "This agent runs in LXC 104 (10.0.0.168). hostctl - the only interface this "
        "agent has to the Proxmox host and every guest - runs as its own process on "
        "the host itself (10.0.0.2), not inside any guest. So 'run a command on the "
        "homelab' is ambiguous by default and needs a deliberate choice: host_exec "
        "reaches the host directly (where /opt/homelab, cron, and /tank live); "
        "guest_exec reaches a specific guest by id (101 docker, 104 this agent "
        "itself, 200 mt5, etc). Picking the wrong one is a common way to answer a "
        "homelab question wrong - for example the hourly MT5 report lives on the "
        "host, not inside VM 200, even though it reports on VM 200's account."
    ),
}


def _load_env() -> None:
    env_path = "/etc/homelab-agent/env"
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            if line.startswith("#") or "=" not in line:
                continue
            key, value = line.rstrip("\n").split("=", 1)
            os.environ.setdefault(key, value)


def main() -> int:
    _load_env()
    brain_path = os.environ.get("AGENT_BRAIN", "/tank/dev/agent/brain.md")
    brain = Brain(brain_path)
    for topic, content in TOPICS.items():
        brain.write(topic, content)
        print(f"wrote: {topic}")
    print(f"\n{len(TOPICS)} topics written to {brain_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
