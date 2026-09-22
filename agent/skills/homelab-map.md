---
name: homelab-map
description: Start here. Every machine, IP, port and service on the homelab, and which tool reaches each one. Read before any question about where something runs.
---

# Homelab map

One Proxmox VE 9.2 node, hostname `tech`, Intel i7-4790, 28GB RAM. Everything lives in this house on one flat LAN (10.0.0.0/24). Facts checked live on 2026-09-23.

## Machines

| Id | Name | IP | Role | Reach it with |
|---|---|---|---|---|
| host | tech | 10.0.0.2 | Proxmox, ZFS pool `tank`, cron jobs, legacy bot in /opt/homelab, hostctl on :8710 | `host_exec` |
| LXC 101 | docker | 10.0.0.165 | every app container, managed by Dockge | `docker_stacks`, `docker_action`, `guest_exec` guest 101 |
| LXC 102 | ai | 10.0.0.166 | Ollama, Open WebUI, money4band bandwidth-earning containers | `guest_exec` guest 102 |
| LXC 103 | mail | 10.0.0.167 | Stalwart mail server, admin@mail.jmrsquared.com | `guest_exec` guest 103, `send_email`, `mail_list_messages` |
| LXC 104 | agent | 10.0.0.168 | you: homelab-agent, checkout at /opt/homelab-agent | `guest_exec` guest 104 |
| VM 200 | mt5 | 10.0.0.171 | Windows 11 running MetaTrader 5 and the TazzieMoney EA | `mt5_status`, `mt5_screenshot`, `guest_exec` guest 200 (cmd.exe syntax) |
| router | Vodafone H-500-s | 10.0.0.254 | gateway; DNS settings locked by firmware | nothing, you cannot manage it |

`guest_action` refuses stop/reboot on 101 and 104, because stopping either one strands you. It can start them.

## Services in LXC 101 (http://10.0.0.165:PORT)

| Service | Port | Dockge stack | Skill |
|---|---|---|---|
| Immich (photos) | 2283 | immich | photos-immich |
| Jellyfin (films and shows) | 8096 | debrid | media-jellyfin |
| Zurg (Real-Debrid WebDAV) | 9999 | debrid | media-jellyfin |
| Jellyseerr (requests) | 5055 | jellyseerr | media-jellyfin |
| AdGuard Home (DNS on :53) | 8080 | adguard | dns-adguard |
| Uptime Kuma | 3001 | monitoring | monitoring |
| Beszel (metrics) | 8090 | monitoring | monitoring |
| Grafana | 3002 | grafana | monitoring |
| Dockge (stack manager) | 5001 | (runs outside /opt/stacks) | docker-stacks |
| Lead generator | 8099 | leadgen | docker-stacks |
| Faceswap worker container | none | faceswap | legacy-host-bot |

## Services in LXC 102 (http://10.0.0.166:PORT)

Ollama API :11434 (models qwen2.5:7b, qwen2.5:3b, llama3.2:3b), Open WebUI :3000. See skill `ai-and-income`.

## Picking the right tool

- The thing lives on the host (cron, /opt/homelab, /tank directly, Proxmox config, `qm`/`pct`/`zpool` commands): `host_exec`.
- The thing lives inside a guest: `guest_exec` with that guest id. Syntax is POSIX `sh` in LXCs and `cmd.exe` on 200.
- A narrow tool exists (`zfs_report`, `host_metrics`, `monitors_status`, `adguard_report`, `media_*`, `photos_*`): use it first. It is faster and its output is shaped for you.
- Anything that may take over five minutes (image pulls, copies, restores, scrubs): `job_start`, then poll `job_status`.

## Remote access

Tailscale runs on the host (100.102.145.0) and advertises 10.0.0.0/24, so family phones reach every service from outside the house. There is no port forwarding. Never expose RDP (3389) or the Proxmox UI (8006) to the internet.

## Where to read more

- Live runbook, regenerated nightly at 03:30 by /opt/homelab/gen-runbook.sh: /tank/dev/HOMELAB.md on the host.
- Nightly change log: /var/log/homelab-changes.log on the host (track-changes.sh, 03:45).
