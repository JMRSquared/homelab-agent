# Deployment runbook

Every command needed to take this repo from "reviewed on a laptop" to "running on the
homelab and answering Slack." Run these in order, on a machine with SSH access to
`root@10.0.0.2` (the Proxmox host), confirming each step's expected output before
moving to the next. Nothing here runs itself - the owner runs every command by hand,
the same as every other deployment step in this project.

`root@10.0.0.2` holds the `tank` ZFS pool (692GB of family photos) and the MT5 trading
VM (guest 200). Steps 2 and 10.4 deliberately try to reach guest 200 - once directly
against `hostctl`, once through the agent - and expect that to fail. That's the point
of those steps, not a mistake.

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

Copy the token for later - it becomes the agent's `HOSTCTL_TOKEN` in step 8:

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
`python3`/`python3-venv`/`python3-pip`/`git`/`openssh-client` inside it. It also
generates an ed25519 keypair at `/root/.ssh/id_ed25519` inside the container for
cloning the agent's private GitHub repo, seeds `known_hosts` with GitHub's host key,
and prints the new public key at the end. **Copy that public key** - it's needed in
the next step. This script does not clone the agent's code, install its Python
dependencies, or start it - that's steps 5 and 9.

## 4. Add the deploy key to GitHub

The container can only clone `git@github.com:JMRSquared/homelab-agent.git` once
GitHub trusts the public key step 3 just printed. This is a manual step in GitHub's
UI, done once:

1. Open the repo on GitHub -> **Settings** -> **Deploy keys** -> **Add deploy key**.
2. Title it something identifiable, e.g. `lxc-103-agent`.
3. Paste the public key from step 3's output.
4. Leave **Allow write access** unchecked - the container only ever needs to pull.
5. Click **Add key**.

The clone in step 5 fails with a permission-denied error until this key is added.

## 5. Clone the agent repo and set up its virtualenv on LXC 103

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 103 -- git clone git@github.com:JMRSquared/homelab-agent.git /opt/homelab-agent'
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 103 -- python3 -m venv /opt/homelab-agent/.venv'
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 103 -- /opt/homelab-agent/.venv/bin/pip install --upgrade pip'
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 103 -- /opt/homelab-agent/.venv/bin/pip install /opt/homelab-agent'
```

The last command installs from `/opt/homelab-agent/pyproject.toml`'s own
`[project.dependencies]` - the package's declaration is the single source of truth
for what the agent needs at runtime, so this step does not hand-list packages that
could drift from it. `hostctl/` comes along in the clone (the whole repo is there
now, including `deploy/` and `docs/`, unlike the old tarball approach) because
`[tool.hatch.build.targets.wheel]` names it alongside `agent` as a package `pip
install` must find to build the wheel; nothing in the running agent process imports
it.

## 6. Deploy the Radicale calendar stack (LXC 101)

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
`CALDAV_PASSWORD` in step 8. Radicale creates the shared calendar the first time a
client connects to `http://10.0.0.165:5232/family/home/` as user `family`; that first
connection can be the agent itself once its env file is in place, or a family phone
(`docs/radicale-setup.md` section 4).

## 7. Create the Slack app and collect its tokens

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

## 8. Set the agent's secrets on LXC 103

`deploy/set-secrets.sh` is part of the clone from step 5, so it's already on the
container. Run it interactively, as root, inside LXC 103:

```bash
ssh -t -o BatchMode=no root@10.0.0.2 'pct enter 103'
```

Then, at the container's own root shell:

```bash
bash /opt/homelab-agent/deploy/set-secrets.sh
```

It prompts for each of the twelve values by name - `MINIMAX_API_KEY`, `MINIMAX_MODEL`,
`HOSTCTL_TOKEN` (from step 1), `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` (step 7),
`JELLYFIN_KEY`, `JELLYSEERR_KEY`, `IMMICH_KEY`, `ADGUARD_BASIC_AUTH`, and
`CALDAV_URL`/`CALDAV_USER`/`CALDAV_PASSWORD` (step 6) - without echoing secret values
back to the terminal, and writes `/etc/homelab-agent/env` at mode `0600` through a
`0600` temp file, so no world-readable copy of any token exists even briefly. Nothing
is typed over SSH into a command that gets logged in shell history, because the whole
exchange happens in the interactive session from `pct enter`. `env.example` at the
repo root documents what each variable is for; `set-secrets.sh`'s prompts are the
source of truth for how to actually set them.

Re-running this script later - to rotate a single leaked token, for example - keeps
every existing value unless a new one is typed, so it doubles as the rotation path.
Exit the container shell (`exit` or Ctrl-D) once it reports success.

## 9. Install the systemd unit and do the first deploy

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
if any of those fail. It then refuses to continue if the working tree is dirty or if
local `HEAD` doesn't match what was just pushed to `origin` - the container can only
fetch what GitHub has, so deploying from uncommitted or unpushed work would silently
leave the previous commit running while `make deploy` reports success. Once past
those checks it pushes the current branch, then drives LXC 103 through `pct exec`:
`git -C /opt/homelab-agent pull`, `pip install /opt/homelab-agent` (this is what
makes a dependency change in `pyproject.toml` actually take effect - its absence in
the old tar-based deploy was a real bug: a new dependency would extract fine and then
crash the service with an `ImportError` on restart), `systemctl restart
homelab-agent`, and `systemctl is-active homelab-agent`.

Expected: `active`. Confirm which commit is actually running - this is the only
place that answers that question:

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 103 -- git -C /opt/homelab-agent rev-parse --short HEAD'
```

`make deploy` prints this itself as its last line; compare it against `git rev-parse
--short HEAD` on your own checkout if you ever need to double-check by hand.

## 10. Live smoke test

Do this in the live Slack workspace, watching `#homelab`, `#family`, and `#agent-log`.

1. **Startup announcement.** `#homelab` should show `:satellite: homelab agent online`
   within a few seconds of the service becoming active in step 9.

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
