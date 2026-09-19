# Homelab Agent — Design Spec

**Date:** 2026-09-19
**Owner:** Tech
**Target:** Proxmox node `tech` (10.0.0.2)

## Goal

One always-running agent that manages the homelab and acts as an AI assistant for
Tech and his family, reachable from Slack.

## Decisions (from scoping)

| Decision | Choice |
|---|---|
| Autonomy | Full. Agent acts, logs reasoning. No approval prompts. |
| Interface | Slack only |
| Home Assistant | Not used, not planned |
| Scope | Everything: infra health, media, photos, calendar, household |
| Model provider | MiniMax Token Plan Plus ($20/mo), M3 |
| Extra spend | None |

## Model budget

Token Plan Plus gives ~1.7B M3 tokens/month, 1M context, 3–4 concurrent agents,
native image and video input, speech in the same quota.

Estimated usage: a 60-second tick carrying 12k of context costs ~520M tokens/month,
under a third of quota. **Tokens are not a constraint.** No local triage model, no
context trimming, no retrieval layer. The agent loads full homelab state into one
prompt each tick.

**Concurrency is the constraint.** Four slots, allocated:

```
slot 1  daemon tick        (preemptible — yields to family)
slot 2  Slack request
slot 3  Slack request
slot 4  burst / escalation
```

Enforced by a semaphore in the agent process, not by prompt instruction.

## Architecture

```
┌─ Proxmox host tech (10.0.0.2) ──────────────────────────┐
│                                                          │
│  hostctl  (systemd, FastAPI, 127.0.0.1:8710 + vmbr0)     │
│    └─ the privileged boundary. Allowlisted verbs only.   │
│                                                          │
│  ┌─ LXC 104 "agent" (10.0.0.168) ─────────────┐          │
│  │  homelab-agent  (systemd, Python 3.11)     │          │
│  │    ├─ Slack Socket Mode listener           │          │
│  │    ├─ APScheduler tick                     │          │
│  │    ├─ tool dispatcher + allowlist          │          │
│  │    ├─ MiniMax M3 client                    │          │
│  │    └─ SQLite state + event log             │          │
│  │  bind mount: /tank/dev/agent               │          │
│  └────────────────────────────────────────────┘          │
│                                                          │
│  LXC 101 docker (10.0.0.165)  ← managed over HTTP APIs   │
│  LXC 102 ai     (10.0.0.166)  ← managed over HTTP APIs   │
│  VM  200 mt5    (10.0.0.171)  ← INVISIBLE to the agent   │
└──────────────────────────────────────────────────────────┘
```

### Why `hostctl` exists

An LXC cannot run `pct` or `qm`. The agent needs host-level operations. Two options
were available: give the agent SSH root on the host, or expose a narrow API.

The narrow API is chosen because it doubles as the safety boundary. Full autonomy
inside the agent is safe precisely because the set of reachable verbs is finite and
defined in code the model never sees.

### Permanently absent from `hostctl`

Not blocked by a prompt. Not gated by approval. **Not implemented.**

- `zfs destroy` on any dataset or snapshot
- `pct destroy`, `qm destroy`
- any operation naming VM 200 or `mt5`
- arbitrary shell on the host

`hostctl` rejects a request naming VM 200 with HTTP 403 before routing. Requests are
logged with full body regardless of outcome.

## Components

### `hostctl` (Proxmox host)

FastAPI, single file, systemd unit, bearer token from `/etc/hostctl/token`.
Binds `127.0.0.1:8710` and `10.0.0.2:8710`, firewalled to the LXC subnet.

| Route | Method | Effect |
|---|---|---|
| `/guests` | GET | `pct list` + `qm list`, VM 200 stripped |
| `/guest/{id}/status` | GET | resource usage for one guest |
| `/guest/{id}/action` | POST | start / stop / reboot. 403 for 200 |
| `/guest/{id}/exec` | POST | `pct exec` with an allowlisted argv |
| `/zfs/status` | GET | `zpool status -v tank` + `zfs list` |
| `/zfs/snapshot` | POST | create a snapshot. Create only |
| `/zfs/scrub` | POST | start a scrub |
| `/disks` | GET | `smartctl` summary, by-id paths only |
| `/host/metrics` | GET | load, memory, ARC, uptime |

### `homelab-agent` (LXC 104)

Single Python process, one systemd unit. Long-running because Slack Socket Mode
needs a persistent websocket; the scheduler lives in the same process.

Modules:

| File | Responsibility |
|---|---|
| `config.py` | env loading, typed settings |
| `store.py` | SQLite: events, state snapshots, Slack thread map |
| `brain.py` | markdown long-term memory at `/tank/dev/agent/brain.md` |
| `model.py` | MiniMax M3 client, tool-call loop, semaphore |
| `tools/base.py` | tool registry, JSON schema, arg validation, dispatcher |
| `tools/infra.py` | hostctl calls, Docker, AdGuard, Uptime Kuma, Beszel |
| `tools/media.py` | Jellyfin, Jellyseerr, Zurg |
| `tools/photos.py` | Immich |
| `tools/household.py` | Shared household lists |
| `tools/comms.py` | Slack post, thread reply, file upload |
| `slack_app.py` | Socket Mode handlers |
| `tick.py` | scheduled sweep, state diff, escalation |
| `main.py` | wiring, startup, graceful shutdown |

### Calendar backend

Dropped. Radicale was deployed and then removed at the owner's request, along with
the `calendar_list` and `calendar_add` tools and the `caldav` dependency. Shared
lists survive as plain markdown files under `/tank/dev/agent/lists/`, which need no
server. If a calendar is wanted later, the tools were generic CalDAV rather than
Radicale-specific, so any CalDAV server would do.

## Tool surface

Deliberately small and flat. Open-weight and mid-tier models degrade fast past ~15
tools and on nested argument objects.

```
guests_list()
guest_action(guest, action)            # start|stop|reboot
zfs_report()
zfs_snapshot(dataset, label)
docker_stacks()
docker_action(stack, action)           # up|down|restart|pull
monitors_status()
host_metrics()
adguard_report()
media_search(query)
media_request(query, kind)             # movie|show
media_library_status()
photos_search(query)
photos_stats()
notes_append(list_name, item)
brain_read(topic)
brain_write(topic, content)
slack_say(channel, text)
```

Every tool validates against its JSON schema before execution. A malformed call is
rejected and returned to the model as a typed error, never passed through.

## Slack layout

Socket Mode, so no public ingress. The Vodafone router needs no port forward.

| Channel | Purpose |
|---|---|
| `#homelab` | agent status, actions taken, incidents |
| `#family` | family assistant, everyone in here |
| `#agent-log` | every tool call with reasoning, machine-readable |

Family members DM the bot or talk in `#family`. Threads keep context; the thread id
maps to a conversation row in SQLite.

## The tick

Every 60 seconds:

1. Collect state: guests, ZFS, disks, Docker, monitors, host metrics.
2. Diff against the last snapshot in SQLite.
3. No change and no pending work → write snapshot, stop. No model call.
4. Change → full state plus the diff plus recent brain context into one M3 call.
5. Model reasons, calls tools, acts.
6. Everything logged to `#agent-log` and SQLite.

Step 3 is not a cost optimization. It stops the agent narrating an idle system into
Slack every minute.

## Escalation to Slack

The agent posts to `#homelab` when it acts, and when something needs Tech. It does
not ask permission. Post format:

```
:wrench: restarted jellyfin
  why: container unhealthy 3 checks running, /health 502
  result: healthy after 12s
  tick: 2026-09-19T14:03:11Z
```

## Failure behaviour

MiniMax unreachable or quota exhausted:

- Tick keeps collecting state and writing snapshots.
- Diffs queue in SQLite.
- A plain non-model alert goes to `#homelab` stating the agent is degraded.
- Queue drains when the provider returns.

Nothing is lost during an outage. The agent never silently stops.

## Non-goals

- No web UI. Slack is the interface.
- No voice in v1.
- No local model. Ollama in LXC 102 stays untouched.
- No off-site backup automation. The agent will nag about the missing off-site copy
  of `/tank/media`; fixing it is a separate project.

## Open risk

`/tank` holds 692G of photos that exist nowhere else. The agent cannot destroy them
by construction, but an unrelated disk failure still can. This spec does not solve
that.
