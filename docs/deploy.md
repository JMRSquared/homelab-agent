# Deployment runbook

Every command needed to take this repo from "reviewed on a laptop" to "running on the
homelab and answering Slack." Run these in order, on a machine with SSH access to
`root@10.0.0.2` (the Proxmox host), confirming each step's expected output before
moving to the next. Nothing here runs itself - the owner runs every command by hand,
the same as every other deployment step in this project.

`root@10.0.0.2` holds the `tank` ZFS pool (692GB of family photos) and the MT5 trading
VM (guest 200). Steps 3 and 8.3 deliberately try to reach guest 200 through the agent
and expect that to fail - that is the point of those steps, not a mistake.

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

Copy the token for later - it becomes the agent's `HOSTCTL_TOKEN` in step 5:

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

## 3. Create LXC 103 for the agent

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 'bash -s' < deploy/create-lxc-103.sh
```

This creates the container at `10.0.0.167`, mounts `/tank/dev/agent` into it (with the
uid remap already handled - see the comment in the script), starts it, and installs
`python3`/`python3-venv`/`python3-pip` inside it. It does not install the agent's own
Python dependencies or start the agent - that's steps 4-6.

## 4. Bootstrap the agent's Python environment on LXC 103

One-time setup; `make deploy` (step 6) only pushes `agent/` and `pyproject.toml` and
restarts the service, it does not create the virtualenv or install dependencies.

Installs from `pyproject.toml`'s own `[project.dependencies]` - the package's
declaration is the single source of truth for what the agent needs at runtime, so
this step does not hand-list packages that could drift from it. `hostctl/` is included
only because `[tool.hatch.build.targets.wheel]` names it alongside `agent` as a
package `pip install` must find; nothing in the running agent process imports it.

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 103 -- mkdir -p /opt/homelab-agent'
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 103 -- python3 -m venv /opt/homelab-agent/.venv'
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 103 -- /opt/homelab-agent/.venv/bin/pip install --upgrade pip'
tar czf /tmp/agent-bootstrap.tgz agent hostctl pyproject.toml
scp /tmp/agent-bootstrap.tgz root@10.0.0.2:/tmp/
ssh -n -o BatchMode=yes root@10.0.0.2 'pct push 103 /tmp/agent-bootstrap.tgz /tmp/agent-bootstrap.tgz'
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 103 -- sh -c "mkdir -p /tmp/agent-bootstrap && tar xzf /tmp/agent-bootstrap.tgz -C /tmp/agent-bootstrap"'
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 103 -- /opt/homelab-agent/.venv/bin/pip install /tmp/agent-bootstrap'
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 103 -- rm -rf /tmp/agent-bootstrap /tmp/agent-bootstrap.tgz'
```

## 5. Deploy the Radicale calendar stack (LXC 101)

Full detail, including per-family-member account creation and phone setup, is in
`docs/radicale-setup.md`. The commands:

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 101 -- mkdir -p /opt/stacks/radicale'
scp deploy/stacks/radicale/compose.yaml root@10.0.0.2:/tmp/radicale-compose.yaml
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct push 101 /tmp/radicale-compose.yaml /opt/stacks/radicale/compose.yaml'
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 101 -- docker compose -f /opt/stacks/radicale/compose.yaml up -d'
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 101 -- docker exec -it radicale htpasswd -B -c /data/htpasswd family'
```

The last command prompts for a password interactively - set one and keep it for
`CALDAV_PASSWORD` in step 6. Radicale creates the shared calendar the first time a
client connects to `http://10.0.0.165:5232/family/home/` as user `family`; that first
connection can be the agent itself once its env file is in place, or a family phone
(`docs/radicale-setup.md` section 4).

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

## 7. Populate the agent's env file on LXC 103

Copy `env.example` as a starting point and fill in every value collected above -
`HOSTCTL_TOKEN` (step 1), `CALDAV_PASSWORD` (step 5), `SLACK_BOT_TOKEN` /
`SLACK_APP_TOKEN` (step 6), plus `MINIMAX_API_KEY`, `JELLYFIN_KEY`, `JELLYSEERR_KEY`,
`IMMICH_KEY`, and `ADGUARD_BASIC_AUTH` from their respective services. Write it
locally, then push it in - never type secrets directly into an SSH session that gets
logged in shell history on the host.

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 103 -- mkdir -p /etc/homelab-agent'
scp /path/to/your/filled-in-env root@10.0.0.2:/tmp/agent.env
ssh -n -o BatchMode=yes root@10.0.0.2 'pct push 103 /tmp/agent.env /etc/homelab-agent/env'
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 103 -- chmod 600 /etc/homelab-agent/env'
ssh -n -o BatchMode=yes root@10.0.0.2 'rm /tmp/agent.env'
```

## 8. Install the systemd unit and deploy the code

```bash
scp deploy/homelab-agent.service root@10.0.0.2:/tmp/homelab-agent.service
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct push 103 /tmp/homelab-agent.service /etc/systemd/system/homelab-agent.service'
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 103 -- systemctl daemon-reload'
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 103 -- systemctl enable homelab-agent'
```

Then, from a checkout of this repo with the tests passing locally:

```bash
make deploy
```

This runs `pytest -q && ruff check . && mypy --strict agent hostctl` first and stops
if any of those fail. It then tars `agent/` and `pyproject.toml`, pushes the tarball
into LXC 103, extracts it over `/opt/homelab-agent`, restarts `homelab-agent`, and
prints its status.

Expected: `active`.

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
