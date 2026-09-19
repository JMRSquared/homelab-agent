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
│  VM  200 mt5    (10.0.0.171)  ← full guest, admin access │
└──────────────────────────────────────────────────────────┘
```

### Why `hostctl` exists

An LXC cannot run `pct` or `qm`. The agent needs host-level operations. Two options
were available: give the agent SSH root on the host, or expose a narrow API.

The narrow API was originally chosen because it doubled as the safety boundary: full
autonomy inside the agent was safe *because* the set of reachable verbs was finite
and defined in code the model never sees. **That property no longer holds.** The
owner asked for total visibility and full administrative access across the homelab,
and `/guest/{id}/shell` (added for it) is unrestricted command execution as root on
any guest, LXC or the Windows trading VM - see "Command execution: no allowlist"
below. `hostctl` still refuses to add a destroy route and still refuses arbitrary
shell *on the host itself*, but a model with a root shell on every guest does not
need a destroy route to cause damage. The "dangerous verbs do not exist" guarantee
is advisory now, not structural - it depends on the model behaving, not on the
verbs being unavailable to it.

### Permanently absent from `hostctl`

Not blocked by a prompt. Not gated by approval. **Not implemented.**

- `zfs destroy` on any dataset or snapshot
- `pct destroy`, `qm destroy`
- arbitrary shell on the *host* (10.0.0.2 itself - not the guests; see above)

Requests are logged with full body regardless of outcome. There is no VM-200-specific
block any more - see "VM 200 (mt5): full parity" below.

### Command execution: no allowlist

`hostctl/pve.py`'s `guest_shell()` runs a free-form command on any guest as root,
dispatched by kind: `sh -c <command>` for an LXC, `cmd.exe /c <command>` for a QEMU
guest. There is no command allowlist (`ALLOWED_EXEC`, the original Linux-only
allowlist, was removed entirely) and no per-guest exclusion - the owner asked for
full administrative access twice and total visibility across the homelab once, and
this is what actually delivers it: mail inside the mail container's filesystem,
MT5's own files on the Windows VM, Jellyfin's internals, anything else, all through
one tool (`guest_exec` on the agent side) instead of a bespoke tool per data source.

`SELF_PROTECTED_GUEST_IDS` (101, 104) still blocks `stop`/`reboot` via
`guest_action` - that protects the agent from stranding itself and is unrelated to
this. It does **not** extend to `guest_shell`: a command run against 101 or 104
(the docker host and the agent's own container) can still strand the agent, and
nothing stops the model from running one. That door is open by the same "no
restrictions not asked for" reasoning as everything else in this section.

Output is capped (4000 characters per stream, in `agent/tools/infra.py`) with the
cut reported explicitly, and a hung command returns a typed timeout error
(`GuestCommandTimeoutError`/`GuestAgentUnavailableError`, HTTP 504/503) rather than
stalling the tool loop, at the same 300s `EXEC_TIMEOUT` every other exec-backed call
uses.

## Components

### `hostctl` (Proxmox host)

FastAPI, single file, systemd unit, bearer token from `/etc/hostctl/token`.
Binds `127.0.0.1:8710` and `10.0.0.2:8710`, firewalled to the LXC subnet.

| Route | Method | Effect |
|---|---|---|
| `/guests` | GET | `pct list` + `qm list`, every guest included |
| `/guest/{id}/status` | GET | resource usage for one guest |
| `/guest/{id}/action` | POST | start / stop / reboot, any guest |
| `/guest/{id}/exec` | POST | run a precise argv (no shell) - `pct exec` (LXC) or `qm guest exec` (QEMU), no allowlist |
| `/guest/{id}/shell` | POST | run a free-form command string - `sh -c` (LXC) or `cmd.exe /c` (QEMU), no allowlist |
| `/zfs/status` | GET | `zpool status -v tank` + `zfs list` |
| `/zfs/snapshot` | POST | create a snapshot. Create only |
| `/zfs/scrub` | POST | start a scrub |
| `/disks` | GET | `smartctl` summary, by-id paths only |
| `/host/metrics` | GET | load, memory, ARC, uptime |
| `/mt5/status` | GET | MT5 account state from the existing on-host TazzieMoney EA export - no VM call |

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

### VM 200 (mt5): full parity

Reversed. This spec originally made VM 200 - the MetaTrader/mt5 trading VM -
permanently invisible: stripped from `/guests`, 403 on any action or exec naming it,
scrubbed out of third-party responses (Uptime Kuma, AdGuard) that happened to mention
it. The owner has since decided, explicitly and in writing, that the agent oversees
the *entire* homelab including VM 200, with full administrative control and no
approval prompts - the same as every other guest.

This was also fixing a live defect, not just a policy change: with VM 200 invisible,
`hostctl` reported four guests where the host actually has five, and the agent
inferred the missing one was down. Asked how things were, it confidently reported
mt5 as down while `qm status 200` said `running`. Hiding the VM produced a false
alarm about it.

What changed:

- `BLOCKED_GUEST_IDS` (`hostctl/pve.py`), the `_guard` 403 (`hostctl/app.py`),
  `BLOCKED_GUESTS` (`agent/tools/infra.py`), and the mt5-scrubbing applied to
  `monitors_status`/`adguard_report` are all removed. VM 200 is a guest like any
  other in every tool and every route.
- `guest_exec`/`guest_shell` now dispatch on guest kind: LXC targets go through
  `pct exec`; VM 200 (a Windows QEMU guest) goes through `qm guest exec`, which
  needs the QEMU guest agent running inside the VM and returns a JSON envelope
  (`out-data`/`err-data`/`exitcode`) normalized to the same shape `pct exec`
  returns. Neither path has a command allowlist any more - see "Command execution:
  no allowlist" above.
- `SELF_PROTECTED_GUEST_IDS`/`SELF_PROTECTED_GUESTS` (101, 104 - the agent's own
  container and its docker/exec recovery path) are unaffected and unrelated: that
  protection exists so the agent can't strand itself, not to gate mt5, and does not
  extend to exec (see above).
- The operational guardrails for VM 200 live in the system prompt
  (`agent/prompts.py`'s `MT5_GUARDRAILS`, shared verbatim by `agent/slack_app.py`'s
  family-chat prompt and `agent/tick.py`'s daemon prompt): stopping or rebooting it
  force-kills MetaTrader (the running profile isn't saved, only the startup-config
  EA reattaches, anything attached by hand is lost), so the agent must verify the
  result immediately afterward and report what it found. **This is the only
  guardrail left.** The prompt originally also forbade placing, modifying, or
  closing a trade, and forbade trading advice; the owner explicitly and repeatedly
  removed both restrictions and asked for a real route to trade. There is no
  trading-specific prohibition anywhere in the system prompts as of this writing.

### MT5 read/write access

**Reads** (open positions, balance, equity): `mt5_status` (`hostctl/mt5.py`) reads
`/tank/dev/tazzie-metrics/tazzie.db`, a SQLite database an existing host-side cron
job (`tazzie-export.py`, part of the owner's own TazzieBot tooling, not this
project) already populates from the TazzieMoney EA's own on-disk heartbeat plus a
screenshot OCR check - read-only, no VM call, no dependency on this project's exec
plumbing at all. Investigated live before building anything else: two independent
sources feed that table and can disagree or go stale independently (confirmed live -
the EA heartbeat source was over 100 hours stale while the OCR-based source reported
fresher numbers minutes old), so `mt5_status` reports both explicitly rather than
picking one, with each row's age, for the model to reason about rather than report
false confidence.

**Writes** (place, modify, or close an order): investigated, not built. The
`MetaTrader5` Python package - the normal way to place orders programmatically - is
**not installed on VM 200**, and no Python interpreter is present at all (confirmed
live via `qm guest exec`: no `python`/`python3` on PATH, no `Program Files\Python*`,
no per-user `AppData\Local\Programs\Python`). Installing either is the owner's own
call on a live trading machine, not something this project does unprompted. No
existing bidirectional control surface was found either: the attached EA
(`TazzieSpreadLogger`, per `tazzie-export.py`'s own docstring) writes a heartbeat
file the host reads; nothing was found that lets the host write commands the EA
reads back. Once a decision is made (install `MetaTrader5` + a small script, or
extend the EA's own MQL5 source with a command channel), the trading tools should
cover list positions / balance & equity / place / modify / close, with every trade
action logged loudly to the audit trail with full parameters, and the tool reading
back the actual resulting state (ticket, fill price, error code) rather than
reporting that a request was merely sent.

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
guest_exec(guest, command)             # root shell, any guest, no allowlist
mt5_status()
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
