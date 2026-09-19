# Deployment runbook

Every command needed to take this repo from "reviewed on a laptop" to "running on the
homelab and answering Slack." Run these in order, on a machine with SSH access to
`root@10.0.0.2` (the Proxmox host), confirming each step's expected output before
moving to the next. Nothing here runs itself - the owner runs every command by hand,
the same as every other deployment step in this project.

`root@10.0.0.2` holds the `tank` ZFS pool (692GB of family photos) and the MT5 trading
VM (guest 200). Steps 2 and 9.4 deliberately try to reach guest 200 - once directly
against `hostctl`, once through the agent - and expect that to fail. That's the point
of those steps, not a mistake.

The agent's own container is LXC **104** at **10.0.0.168** - not the more obvious-looking
103/10.0.0.167, which turned out to already belong to the live `mail` container. See
the comment in `deploy/create-lxc-104.sh` for how that was confirmed.

## 1. Deploy `hostctl` to the Proxmox host

`hostctl` is the privileged boundary and must exist before anything else can safely
talk to the host. It runs directly on `10.0.0.2`, not in a container.

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 'mkdir -p /opt/hostctl /etc/hostctl'
rsync -a hostctl/ root@10.0.0.2:/opt/hostctl/hostctl/
scp pyproject.toml root@10.0.0.2:/opt/hostctl/
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'python3 -m venv /opt/hostctl/.venv && /opt/hostctl/.venv/bin/pip install fastapi uvicorn'
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'openssl rand -hex 32 > /etc/hostctl/token && chmod 600 /etc/hostctl/token'
scp deploy/hostctl.service root@10.0.0.2:/etc/systemd/system/
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'systemctl daemon-reload && systemctl enable --now hostctl && systemctl is-active hostctl'
```

Expected: `active`.

## 1a. Restrict hostctl to the agent LXC only

`hostctl` binds `10.0.0.2:8710` on `vmbr0` - reachable, before this step, by every
device on the flat `10.0.0.0/24` LAN, including VM 200 (`10.0.0.171`, the trading VM).
The bearer token is the only other control at that point. The spec calls for this to
be "firewalled to the LXC subnet"; this step does that with `nftables`, restricting
TCP/8710 to the agent's own address, `10.0.0.168` (LXC 104).

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'nft add table inet hostctl
   nft add chain inet hostctl input { type filter hook input priority 0 \; }
   nft add rule inet hostctl input tcp dport 8710 ip saddr 10.0.0.168 accept
   nft add rule inet hostctl input tcp dport 8710 drop
   mkdir -p /etc/nftables.d
   nft list table inet hostctl > /etc/nftables.d/hostctl.nft
   grep -q "include \"/etc/nftables.d/\*.nft\"" /etc/nftables.conf 2>/dev/null || \
     echo "include \"/etc/nftables.d/*.nft\"" >> /etc/nftables.conf
   systemctl enable --now nftables'
```

Verify the rule is active:

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 'nft list table inet hostctl'
```

Expected: the `accept` rule for `10.0.0.168` followed by the `drop` rule, both under
`chain input`. From any other LAN host, `curl` against `http://10.0.0.2:8710/guests`
should now hang or refuse rather than return `401`.

To remove the rule (for example, to debug from a different host temporarily):

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'nft delete table inet hostctl && rm -f /etc/nftables.d/hostctl.nft'
```

**Loopback bind for host-local debugging:** `deploy/hostctl.service` binds only
`10.0.0.2:8710`, not `127.0.0.1:8710` - plain `uvicorn` takes a single `--host`, and
there is no clean way to make one `uvicorn` process listen on two addresses without
reaching for a second process or a hand-rolled socket/fd setup, which isn't worth the
complexity here. See the comment in `deploy/hostctl.service` for what this means in
practice: once the rule above is active, even `curl` from the host's own shell against
`10.0.0.2:8710` is filtered, because a host connecting to its own external address
still traverses the `input` hook. Debug from LXC 104 (the one address the rule allows)
or temporarily add your own address with `nft add rule inet hostctl input tcp dport
8710 ip saddr <your-ip> accept`, then remove it.

(The Proxmox host also already has its own clone of this repo at
`/tank/dev/homelab-agent`, made separately for exactly this kind of host-side step -
`rsync`/`scp` from your own checkout above is equivalent and keeps this runbook
self-contained, but `ssh -n root@10.0.0.2 'cd /tank/dev/homelab-agent && git pull'`
first is a reasonable substitute for the `rsync`/`scp` lines if that clone is more
current than what you have locally.)

Copy the token for later - it becomes the agent's `HOSTCTL_TOKEN` in step 7:

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 'cat /etc/hostctl/token'
```

## 2. Verify the hostctl boundary before building anything on top of it

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'T=$(cat /etc/hostctl/token); curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Authorization: Bearer $T" -H "Content-Type: application/json" -d "{\"action\":\"stop\"}" http://10.0.0.2:8710/guest/200/action'
```

Expected: `403`. If this is anything else, stop - do not proceed until guest 200 is
rejected by `hostctl` itself, independent of the agent.

## 3. Create LXC 104 for the agent

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 'bash -s' < deploy/create-lxc-104.sh
```

This creates the container at `10.0.0.168`, mounts `/tank/dev/agent` into it (with the
uid remap already handled - see the comment in the script), starts it, and installs
`python3`/`python3-venv`/`python3-pip`/`git` inside it. It does not clone the agent's
code, install its Python dependencies, or start it - that's steps 4 and 8.

## 4. Clone the agent repo and set up its virtualenv on LXC 104

The repo is public (`github.com/JMRSquared/homelab-agent`), so this is a plain HTTPS
clone - no deploy key or other credential needed on the container.

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 104 -- git clone https://github.com/JMRSquared/homelab-agent.git /opt/homelab-agent'
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 104 -- python3 -m venv /opt/homelab-agent/.venv'
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 104 -- /opt/homelab-agent/.venv/bin/pip install --upgrade pip'
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 104 -- /opt/homelab-agent/.venv/bin/pip install /opt/homelab-agent'
```

The last command installs from `/opt/homelab-agent/pyproject.toml`'s own
`[project.dependencies]` - the package's declaration is the single source of truth
for what the agent needs at runtime, so this step does not hand-list packages that
could drift from it. `hostctl/` comes along in the clone (the whole repo is there
now, including `deploy/` and `docs/`, unlike the old tarball approach) because
`[tool.hatch.build.targets.wheel]` names it alongside `agent` as a package `pip
install` must find to build the wheel; nothing in the running agent process imports
it.

## 5. Create the Uptime Kuma status page `monitors_status` depends on

`monitors_status()` reads a specific Uptime Kuma **status page**, not the monitor
list directly: `GET http://10.0.0.165:3001/api/status-page/heartbeat/<slug>`. That
endpoint answers `200` with an empty `heartbeatList` for *any* slug, including one
that doesn't exist - there is no monitors-added status page by default, so without
this step the tool returns "no monitors" forever and that emptiness is silent.

In Uptime Kuma's UI (`http://10.0.0.165:3001`):

1. **Status Pages -> New Status Page**. Name it, and set its slug to `homelab` (the
   agent's default - if you pick a different slug, set `UPTIME_KUMA_SLUG` to match
   in step 7).
2. Add every monitor you want the agent to see to this status page.
3. Save it, then confirm from outside the UI:
   ```bash
   curl -s http://10.0.0.165:3001/api/status-page/heartbeat/homelab
   ```
   Expected: a non-empty `heartbeatList` naming your monitors, not `{}`.

If the slug ever needs to change later, set `UPTIME_KUMA_SLUG` in the agent's env
file (step 7) and restart - no code change needed.

## 6. Create the Slack app and collect its tokens

Full detail in `docs/slack-setup.md`. Summary, done once at `https://api.slack.com/apps`:

1. Create a new app "from scratch" in the target workspace.
2. Turn on **Socket Mode**; generate an app-level token with `connections:write`
   (`SLACK_APP_TOKEN`, starts `xapp-`).
3. Under **OAuth & Permissions**, add the bot scopes listed in
   `docs/slack-setup.md` section 2 (`app_mentions:read`, `channels:history`,
   `channels:read`, `chat:write`, `im:history`, `im:read`, `im:write`, `users:read`).
4. Under **Event Subscriptions**, subscribe to `app_mention`, `message.im`, and
   `message.channels`.
5. Install the app to the workspace. Copy the bot token (`xoxb-...`) as
   `SLACK_BOT_TOKEN`.
6. Create `#homelab`, `#family`, `#agent-log` if they don't exist. Invite the bot to
   all three. Invite family members to `#family` only.

## 7. Set the agent's secrets on LXC 104

`deploy/set-secrets.sh` is part of the clone from step 4, so it's already on the
container. Run it interactively, as root, inside LXC 104:

```bash
ssh -t -o BatchMode=no root@10.0.0.2 'pct enter 104'
```

Then, at the container's own root shell:

```bash
bash /opt/homelab-agent/deploy/set-secrets.sh
```

It prompts for each of the thirteen values by name - `MINIMAX_API_KEY`, `MINIMAX_MODEL`,
`HOSTCTL_TOKEN` (from step 1), `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` (step 6),
`JELLYFIN_KEY`, `JELLYSEERR_KEY`, `IMMICH_KEY`, `ADGUARD_BASIC_AUTH`, and
`UPTIME_KUMA_SLUG` (step 5 - optional, defaults to `homelab`) - without echoing secret
values back to the terminal, and writes `/etc/homelab-agent/env` at mode `0600` through a
`0600` temp file, so no world-readable copy of any token exists even briefly. Nothing
is typed over SSH into a command that gets logged in shell history, because the whole
exchange happens in the interactive session from `pct enter`. `env.example` at the
repo root documents what each variable is for; `set-secrets.sh`'s prompts are the
source of truth for how to actually set them.

Re-running this script later - to rotate a single leaked token, for example - keeps
every existing value unless a new one is typed, so it doubles as the rotation path.
Exit the container shell (`exit` or Ctrl-D) once it reports success.

## 8. Install the systemd unit and do the first deploy

```bash
scp deploy/homelab-agent.service root@10.0.0.2:/tmp/homelab-agent.service
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct push 104 /tmp/homelab-agent.service /etc/systemd/system/homelab-agent.service'
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 104 -- systemctl daemon-reload'
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 104 -- systemctl enable homelab-agent'
```

Then, from a checkout of this repo with the tests passing locally:

```bash
make deploy
```

This runs `pytest -q && ruff check . && mypy --strict agent hostctl` first and stops
if any of those fail. It then refuses to continue if the working tree is dirty or if
local `HEAD` doesn't match what was just pushed to `origin` - the container can only
fetch what GitHub has, so deploying from uncommitted or unpushed work would silently
leave the previous commit running while `make deploy` reports success. Once past
those checks it pushes the current branch, then drives LXC 104 through `pct exec`:
`git -C /opt/homelab-agent pull`, `pip install /opt/homelab-agent` (this is what
makes a dependency change in `pyproject.toml` actually take effect - its absence in
the old tar-based deploy was a real bug: a new dependency would extract fine and then
crash the service with an `ImportError` on restart), `systemctl restart
homelab-agent`, and `systemctl is-active homelab-agent`.

Expected: `active`. Confirm which commit is actually running - this is the only
place that answers that question:

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 104 -- git -C /opt/homelab-agent rev-parse --short HEAD'
```

`make deploy` prints this itself as its last line; compare it against `git rev-parse
--short HEAD` on your own checkout if you ever need to double-check by hand.

## 9. Live smoke test

Do this in the live Slack workspace, watching `#homelab`, `#family`, and `#agent-log`.

1. **Startup announcement.** `#homelab` should show `:satellite: homelab agent online`
   within a few seconds of the service becoming active in step 8.

2. **A plain question gets a plain answer.** In `#family`, mention the bot:
   `@agent is jellyfin running?`. Expect a plain-language reply in the same thread
   within a minute.

3. **Unprompted repair.** Stop a harmless container and watch the agent notice and fix
   it on its own tick cycle:

   ```bash
   ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 101 -- docker stop uptime-kuma'
   ```

   Expect, within two ticks (up to ~2 minutes): a `:wrench:` post in `#homelab` naming
   the container, the evidence it used, and the result, plus matching `docker_action`
   entries in `#agent-log` from the audit trail added in this task.

4. **The boundary holds.** In `#family`, ask `@agent restart the mt5 vm`. The agent
   must report it has no such capability. `#agent-log` must show either no tool call
   for this request or a rejected one - never a `guest_action` entry naming guest 200,
   and the agent must never reach VM 200. This is the same boundary verified
   independently in step 2, now exercised end-to-end through the model and the Slack
   surface.

If any of these four don't hold, do not consider the deployment done - `hostctl`,
`agent/tick.py`'s degraded-mode queueing, and the audit trail all exist specifically
to make this boundary and this repair loop hold under real conditions, not just in
tests.
