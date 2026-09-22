---
name: troubleshooting
description: The general triage order for "X is down", "X is slow", a guest that won't start, a full disk, or a fault you don't recognise. Read when no more specific skill fits.
---

# Troubleshooting playbook

## Order of work

1. `incident_find` with the component and symptom. If a past incident matches, start from its fix.
2. Read the skill for that service (`homelab-map` names it).
3. Scope it: one service, one guest, the whole host, or the network. `guests_list`, `monitors_status` and `host_metrics` answer that in three calls.
4. Read the error: container logs, `journalctl`, the service's own log. Quote the exact line.
5. Take the smallest fix that addresses the cause: restart one container before a stack, a stack before a guest, a guest before the host.
6. Verify from the user's side: curl the port, run the search, load the page. "Restart returned ok" is not verified.
7. `incident_record` what you saw, the cause and the fix, if it is worth remembering.
8. Report in plain words: what broke, what you did, whether it works now.

## Common cases

| Symptom | First check | Usual fix |
|---|---|---|
| One web app down | `docker ps -a` in 101, then its logs | `docker_action` restart its stack |
| Every app in 101 down | `guests_list`: is 101 running? | `guest_action` start 101 |
| Every tool errors | `self_test` | hostctl down, see skill `self-agent` |
| Guest won't start | `host_exec` `pct start <id>` or `qm start <id>` and read the error | lock left by backup: `pct unlock <id>`/`qm unlock <id>` once no vzdump runs (`pgrep vzdump`) |
| Disk full in a guest | `df -h` in the guest | prune images or logs; LXC root grows with `pct resize <id> rootfs +4G` only when the owner agrees |
| Host slow | `host_metrics`, then `top` | see skill `monitoring` |
| Pool DEGRADED | `zpool status -v tank` | report, see skill `storage-zfs` |
| Films won't play | `ls /mnt/zurg` in 101 | see skill `media-jellyfin` |
| No internet | scope it | see skill `dns-adguard` |

## Rules

- Snapshot a dataset before changing its contents.
- Never run destructive commands (`rm -rf` on data, `zfs destroy`, `docker system prune --volumes`, `pct destroy`, `qm destroy`, wiping disks). Ask the owner, and name exactly what would be lost.
- Never paste passwords, API keys or tokens into Slack, email or the brain.
- Nested quotes through tool, hostctl, pct or qm, and a shell mangle. Write a script file with a heredoc and run the file.
- If you cannot fix it from here, say so after one confirming check, name the human step, and stop.
