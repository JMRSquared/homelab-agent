---
name: self-agent
description: How you yourself run - LXC 104, hostctl on the host, your logs, env, brain, database, timers, and how to check or repair your own tools. Read when a tool fails or someone asks how you work.
---

# You: homelab-agent

## Where you run

- Process: `homelab-agent.service` in LXC 104 (10.0.0.168), checkout /opt/homelab-agent, venv /opt/homelab-agent/.venv.
- Config: /etc/homelab-agent/env (mode 0600). Never print it or post any value from it.
- Model: MiniMax through its OpenAI-compatible API.
- Brain: /tank/dev/agent/brain.md, backups in /tank/dev/agent/brain-backups/.
- Events, conversation history and usage: SQLite at /tank/dev/agent/agent.db.
- Outbox for attachments: /tank/dev/agent/outbox/.
- Extra skills added on the box: /tank/dev/agent/skills/*.md (same format as the bundled ones; a local file replaces a bundled skill of the same name).
- Improvement cycle: `homelab-improve.timer` in LXC 104, every 10 minutes.

## hostctl

Every host and guest action goes through hostctl, a FastAPI service on the host (10.0.0.2:8710, `hostctl.service`). If every infra tool fails at once, hostctl is down or unreachable, not the services behind it.
- `self_test` probes each dependency and says which one broke.
- `host_exec` needs hostctl too, so it cannot fix hostctl. The improvement cycle has `hostctl_restart_verified`; from chat, ask the owner to run `systemctl restart hostctl` on the host.

## Reading your own logs

- `guest_exec` 104 `journalctl -u homelab-agent -n 100 --no-pager`
- `guest_exec` 104 `journalctl -u homelab-improve -n 50 --no-pager`
- `usage_report` for token spend by loop.

## Changing yourself

Code and skill changes ship only through `self_deploy`, which only the improvement cycle holds. It commits, runs the tests, restarts, verifies and rolls back on failure. Never commit and restart by hand. A skill is a markdown file in agent/skills/ with `name` and `description` frontmatter; fix a wrong fact there the same way you fix code. For a quick fix without a deploy, write the corrected skill to /tank/dev/agent/skills/ instead.

## Round limit

Each reply has a cap on tool-call rounds. When the answer depends on a human action (re-attaching the EA, swapping a disk, entering a password), stop after one confirming check and say what the person needs to do. Digging further burns the budget and the reply ends in "exceeded the tool-call round limit" with nothing useful.
