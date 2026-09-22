---
name: ai-and-income
description: LXC 102 - Ollama local models, Open WebUI, and the money4band bandwidth-sharing containers that earn income. Read for local AI questions or "are we making money from bandwidth".
---

# LXC 102 `ai` (10.0.0.166)

6 cores, 6GB RAM, no GPU. Compose stacks under /opt/stacks inside the guest; reach it with `guest_exec` 102.

## Local AI

- Ollama API http://10.0.0.166:11434, models qwen2.5:7b, qwen2.5:3b, llama3.2:3b. List with `curl -s localhost:11434/api/tags`.
- Open WebUI http://10.0.0.166:3000, the family's chat page for those models.
- CPU only: a 7B model answers at a few tokens per second. Slow is normal.
- Backups skip /opt/stacks/ai/ollama. After a restore, re-pull the three models.
- No GPU means no local image or video generation worth the wait. Renting a cloud GPU is the answer when someone asks.

## Bandwidth income (money4band)

Containers named `tech-server_<app>`: earnapp, honeygain, packetstream, traffmonetizer, earnfm, proxylite, bitping, gradient, proxyrack, mystnode. Plus tech-server_m4b_dashboard (static page at :8081, no API) and tech-server_watchtower (auto-updates them).

Health comes from `guest_exec` 102 `docker ps` and `docker logs --tail 50 tech-server_<app>`. The legacy hourly checker has reported 0/10 while all ten were up; trust `docker ps` over it.

You cannot read balances. They sit on each provider's website behind the owner's login.

Brain topic `channel-homelab-income` holds each app's healthy and broken log patterns and the reply shape the owner wants. Read it before diagnosing. In short:
- honeygain DNS errors and a gradient log frozen at login: restart that container.
- proxyrack "Device not found yet" looping: bad key in its config, the owner must fix it.
- mystnode "Could not get consumer channel": normal noise, leave it.
- earnfm and the proxyrack data plane cannot reach their servers from this network. Restarts do not help.

When the owner says "fix it", restart the broken ones in the same turn, then report a short per-app table: working, broken, fixed.
