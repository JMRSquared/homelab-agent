---
name: docker-stacks
description: The Dockge-managed Docker Compose stacks in LXC 101 - stack-to-container mapping, logs, restarts, updates. Read before restarting or debugging any app container.
---

# Docker stacks (LXC 101)

Dockge (http://10.0.0.165:5001) manages every stack under /opt/stacks/<stack>/compose.yaml inside LXC 101. `docker_action` works on stack names. Container names differ, so map first.

## Stack to container

| Stack | Containers |
|---|---|
| immich | immich_server, immich_machine_learning, immich_redis, immich_postgres |
| debrid | zurg, rclone, jellyfin |
| jellyseerr | jellyseerr |
| adguard | adguardhome |
| monitoring | uptime-kuma, beszel, beszel-agent |
| grafana | grafana |
| leadgen | leadgen |
| faceswap | faceswap |

Jellyfin lives in the `debrid` stack. Restarting `debrid` restarts Zurg, rclone and Jellyfin together, and anyone mid-film loses playback.

## Look before you restart

1. `docker_stacks` for stack state.
2. `guest_exec` 101: `docker ps -a --format '{{.Names}} {{.Status}}'` to find exited or restarting containers.
3. `guest_exec` 101: `docker logs --tail 80 <container>` for the error.
4. `guest_exec` 101: `docker inspect -f '{{.State.OOMKilled}} {{.RestartCount}}' <container>` when a container keeps dying.

## Fix

- One stuck service: `docker_action` restart on its stack.
- Stale image or known upstream bug fixed: `docker_action` pull, then up. Immich pins `IMMICH_VERSION` in /opt/stacks/immich/.env; read the Immich release notes before a major bump, since those can need a DB migration.
- Disk full inside LXC 101 (`df -h /` in the guest): `docker image prune -f` is safe. Never `docker system prune -a --volumes`; volumes hold Immich's database and Jellyfin's config.
- Never `docker compose down -v` on any stack.

## Editing a compose file

Copy it first (`cp compose.yaml compose.yaml.bak-$(date +%s)`), edit, then `docker_action` up. Nested quoting through tools mangles YAML, so write the full new file with a heredoc instead of a sed one-liner.

## Resource limits

LXC 101 has 4 cores and 12GB RAM. Immich machine learning and the faceswap worker are the heavy users. The host has no GPU, so jobs run on CPU; slow ML jobs are normal, not a fault.
